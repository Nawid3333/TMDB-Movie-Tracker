"""Full-scan enrichment: fetch detailed movie records and related data."""

import concurrent.futures
import contextlib
import functools
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from config.config import (
    COLD_REENRICH_DAYS,
    COOL_DAYS,
    TMDB_DETAIL_WORKERS,
    TMDB_LANGUAGE,
    WARM_DAYS,
)
from src.atomic_io import atomic_write_json
from src.index import ensure_record_exists, load_details, load_index, now_iso, save_details, save_index
from src.posters import download_poster
from src.tmdb_api import TMDBClient, check_status, is_auth_rejected, pick_certification
from src.ui.reports import title_line, title_link
from src.ui.term import alert, bold, dim, err, ok, success, warn
from src.ui.term import cprint as print

logger = logging.getLogger(__name__)

# No "keywords": they only ever fed the connected-TV lookup, which is gone --
# this tracker is for movies. Keywords already in details.json stay as they are.
_APPEND_TO_RESPONSE = "credits,external_ids,release_dates,videos,watch/providers,recommendations,similar"

# How often a full scan saves its progress (index, details, then checkpoint).
_CHECKPOINT_SECONDS = 60.0


def _release_year(release_date: str) -> int | None:
    if release_date and len(release_date) >= 4:
        try:
            return int(release_date[:4])
        except ValueError:
            pass
    return None


def _days_since_release(release_date: str) -> int | None:
    if not release_date:
        return None
    today = datetime.now(UTC).date()
    try:
        release = datetime.fromisoformat(release_date).replace(tzinfo=UTC).date()
        return (today - release).days
    except ValueError:
        return None


def _volatility_tier(record: dict) -> str:
    """Classify how stale a record can be before re-enrichment.

    Returns one of: hot, warm, cool, cold.
    """
    status = (record.get("status") or "").strip()
    if status and status != "Released":
        return "hot"
    release_date = record.get("release_date", "")
    if not release_date:
        return "hot"
    days = _days_since_release(release_date)
    if days is None:
        return "hot"
    if days <= WARM_DAYS:
        return "warm"
    if days <= COOL_DAYS:
        return "cool"
    return "cold"


def _should_enrich(record: dict, details: dict, force: bool = False) -> bool:
    if force:
        return True
    if "collection" in (details.get("enrich_incomplete") or []):
        # The collection lookup failed last time. What is on record for it is
        # the older data kept as a stand-in, so the movie is due again
        # whatever its tier -- a cold film would otherwise carry the gap for
        # COLD_REENRICH_DAYS. (A leftover "connected_tv" marker from the
        # removed TV lookup does not count; the next enrich clears it.)
        return True
    tier = _volatility_tier(record)
    if tier in ("hot", "warm"):
        return True
    enriched_at = details.get("enriched_at", "")
    if not enriched_at or not isinstance(enriched_at, str):
        return True
    try:
        enriched = datetime.fromisoformat(enriched_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if enriched.tzinfo is None:
        # Everything this program writes carries a zone, but one stamp without
        # it -- hand-edited, or from an older build -- made the subtraction
        # below raise TypeError, which ValueError does not catch, and that
        # aborted the whole enrichment run over one record.
        enriched = enriched.replace(tzinfo=UTC)
    days = (datetime.now(UTC) - enriched).days
    if tier == "cool":
        return days >= 7
    if tier == "cold":
        return days >= COLD_REENRICH_DAYS
    return True


def _fetch_collection(client: TMDBClient, collection_id: int) -> dict | None:
    """The collection's parts, or None when the lookup failed."""
    try:
        resp = client.get(f"/collection/{collection_id}")
        check_status(resp)
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "parts": [
                {"id": p.get("id"), "title": p.get("title"), "release_date": p.get("release_date")}
                for p in data.get("parts", [])
                if isinstance(p, dict) and p.get("id")
            ],
        }
    except Exception as exc:
        logger.warning("Collection fetch failed for %s: %s", collection_id, exc)
        return None


class _LockedCache:
    """Thread-safe per-run cache that asks TMDB for each collection at most once.

    Many movies share a collection, and the worker pool used to
    check-then-set: two workers missing the same key at the same moment both
    fetched it. And because a stored None read back exactly like a miss, a
    collection whose lookup failed was fetched again -- three attempts and a
    backoff each time -- by every movie in it. get_or_fetch() holds a per-key
    lock across the fetch, so the second worker waits for the first one's
    answer, and remembers every answer for the run, a failure (None)
    included: an outage costs one failed lookup per key, not one per movie.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[int, Any] = {}
        self._key_locks: dict[int, threading.Lock] = {}

    def get_or_fetch(self, key: int, fetch: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                if key in self._data:
                    return self._data[key]
            value = fetch()
            with self._lock:
                self._data[key] = value
            return value


def _resolve_collection(
    client: TMDBClient,
    details: dict,
    collection_cache: _LockedCache,
    previous: Any,
) -> bool:
    """Fill in the movie's collection parts; False when the lookup failed.

    A failed lookup used to leave the fresh, empty "parts" in place -- and the
    movie was still stamped enriched, so a cold film went 90 days with its
    whole franchise missing from the gaps report. Now the parts already on
    record for the same collection are kept, and the caller marks the
    enrichment incomplete so the next scan asks again.
    """
    collection_id = (details.get("collection") or {}).get("id")
    if not collection_id:
        return True
    fetched = collection_cache.get_or_fetch(collection_id, functools.partial(_fetch_collection, client, collection_id))
    if fetched:
        details["collection"] = fetched
        return True
    if isinstance(previous, dict) and previous.get("id") == collection_id and previous.get("parts"):
        details["collection"] = previous
    return False


def _extract_titles(movie: dict, language: str = TMDB_LANGUAGE) -> dict[str, str]:
    """Extract local/original/english/german title variants from a TMDB movie response."""
    local = movie.get("title") or ""
    original = movie.get("original_title") or ""
    english = ""
    german = ""
    original_lang = (movie.get("original_language") or "").lower()

    german = local if language.lower().startswith("de") else ""

    if language.lower().startswith("en"):
        english = local
    elif original_lang.startswith("en"):
        english = original or local

    return {
        "title": local,
        "title_original": original,
        "title_english": english,
        "title_german": german,
    }


def _enrichment_snapshot(membership: dict, details: dict) -> dict[str, str]:
    """A comparable, display-ready snapshot of the fields worth telling the user about.

    Deliberately narrow: cast/crew/overview churn on TMDB constantly
    and would swamp a change report in noise. These are the fields a re-enrich
    can change that actually matter to someone tracking whether a film is
    watchable yet (status, release date) or worth re-checking (runtime,
    certification, a finalized title).
    """
    cert = details.get("certification")
    rating = cert.get("rating") if isinstance(cert, dict) else None
    runtime = details.get("runtime")
    return {
        "Title": membership.get("title") or "(none)",
        "Release date": membership.get("release_date") or "(none)",
        "Status": membership.get("status") or "(none)",
        "Runtime": f"{runtime} min" if runtime else "(none)",
        "Certification": rating or "(none)",
    }


def _enrich_one(
    client: TMDBClient,
    membership: dict,
    details: dict,
    collection_cache: _LockedCache,
    image_client: Any,
) -> list[tuple[str, str, str]]:
    """Fetch and merge full details for a single movie.

    Returns a list of (field, old_value, new_value) for fields that changed
    since the last time this movie was enriched -- empty on a movie's first
    enrichment (nothing to compare against yet) or when nothing changed.

    Movies only: nothing here asks TMDB about TV. A "connected_tv" or
    "keywords" entry left in details.json by an older build is neither read
    nor rewritten.
    """
    was_enriched_before = bool(details.get("enriched_at"))
    before = _enrichment_snapshot(membership, details) if was_enriched_before else None
    # Kept before the fresh response overwrites it, so a failed collection
    # lookup below can fall back to what an earlier enrich found.
    previous_collection = details.get("collection")

    movie_id = membership["id"]
    params = {"language": TMDB_LANGUAGE, "append_to_response": _APPEND_TO_RESPONSE}
    resp = client.get(f"/movie/{movie_id}", params=params)
    if resp.status_code == 404:
        # TMDB merged or deleted the record. Mark gone but keep data.
        membership["gone"] = True
        membership["gone_since"] = membership.get("gone_since") or now_iso()
        logger.debug("Movie %s no longer resolves on TMDB; marked gone", movie_id)
        return []
    check_status(resp)
    movie = resp.json()
    if not isinstance(movie, dict):
        raise ValueError(f"Movie {movie_id} returned non-object body")

    if membership.get("gone"):
        # Only Force re-enrich asks TMDB about a movie marked gone, and it
        # answered: the 404 that marked it was not the last word. Track it
        # normally again rather than leave it frozen for good.
        logger.info("Movie %s resolves on TMDB again; no longer marked gone", movie_id)
        membership["gone"] = False
        membership.pop("gone_since", None)

    titles = _extract_titles(movie)
    membership["title"] = titles["title"] or membership.get("title", "")
    membership["title_original"] = titles["title_original"] or membership.get("title_original", "")
    membership["title_english"] = titles["title_english"] or membership.get("title_english", "")
    membership["title_german"] = titles["title_german"] or membership.get("title_german", "")
    membership["release_date"] = movie.get("release_date") or membership.get("release_date", "")
    membership["poster_path"] = movie.get("poster_path")
    membership["status"] = movie.get("status") or membership.get("status", "")
    membership["imdb_id"] = (movie.get("external_ids") or {}).get("imdb_id") or membership.get("imdb_id", "")

    # German title fallback if not already set.
    if not membership.get("title_german") and TMDB_LANGUAGE.lower().startswith("de"):
        membership["title_german"] = membership["title"]

    origin_country = None
    countries = movie.get("production_countries") or []
    if countries and isinstance(countries, list):
        origin_country = countries[0].get("iso_3166_1")

    details["id"] = movie_id
    details["runtime"] = movie.get("runtime")
    details["overview"] = movie.get("overview")
    details["tagline"] = movie.get("tagline")
    details["genres"] = [g.get("name") for g in movie.get("genres", []) if isinstance(g, dict)]
    details["original_language"] = movie.get("original_language")
    details["production_companies"] = [
        c.get("name") for c in movie.get("production_companies", []) if isinstance(c, dict)
    ]
    details["production_countries"] = [c.get("iso_3166_1") for c in countries if isinstance(c, dict)]
    details["certification"] = pick_certification(movie.get("release_dates", {}).get("results", []), origin_country)

    credits = movie.get("credits", {})
    crew = credits.get("crew", []) if isinstance(credits, dict) else []
    cast = credits.get("cast", []) if isinstance(credits, dict) else []
    details["directors"] = [
        p.get("name") for p in crew if isinstance(p, dict) and p.get("job") == "Director" and p.get("name")
    ]
    details["writers"] = [
        p.get("name") for p in crew if isinstance(p, dict) and p.get("job") == "Writer" and p.get("name")
    ]
    details["cast"] = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "character": p.get("character"),
            "order": p.get("order"),
        }
        for p in cast
        if isinstance(p, dict) and p.get("id")
    ][:20]
    details["crew"] = [
        {"id": p.get("id"), "name": p.get("name"), "job": p.get("job"), "department": p.get("department")}
        for p in crew
        if isinstance(p, dict) and p.get("id")
    ]

    raw_collection = movie.get("belongs_to_collection")
    if raw_collection and isinstance(raw_collection, dict):
        details["collection"] = {
            "id": raw_collection.get("id"),
            "name": raw_collection.get("name"),
            "parts": [],
        }
    elif "collection" not in details:
        details["collection"] = {"id": None, "name": None, "parts": []}

    if _resolve_collection(client, details, collection_cache, previous_collection):
        details.pop("enrich_incomplete", None)
    else:
        # Read by _should_enrich: due again on the next scan whatever the tier.
        details["enrich_incomplete"] = ["collection"]

    # Poster cache.
    if membership.get("poster_path"):
        poster_file = download_poster(image_client, movie_id, membership["poster_path"])
        membership["poster_file"] = str(poster_file) if poster_file else None

    details["enriched_at"] = now_iso()

    if before is None:
        return []
    after = _enrichment_snapshot(membership, details)
    return [(field, before[field], after[field]) for field in before if before[field] != after[field]]


def _load_checkpoint() -> set[int]:
    from config.config import ENRICH_CHECKPOINT_FILE

    try:
        with open(ENRICH_CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        done = data.get("done", []) if isinstance(data, dict) else []
        return {int(x) for x in done}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def _save_checkpoint(done: set[int]) -> None:
    from config.config import ENRICH_CHECKPOINT_FILE

    if done:
        atomic_write_json(ENRICH_CHECKPOINT_FILE, {"done": sorted(done)}, backup=False)
    else:
        with contextlib.suppress(OSError):
            ENRICH_CHECKPOINT_FILE.unlink(missing_ok=True)


def _print_changed(label: str, changes: list[tuple[str, str, str]]) -> None:
    """Print a changed movie so it stands out from the dimmed unchanged rows.

    The "●" marker (instead of "✓") keeps changed rows findable even when
    colour is off; with colour, the old value is red and the new one green.
    """
    print(f"  {warn('●')} {bold(label)}")
    for field, old_value, new_value in changes:
        print(f"      {field}: {err(old_value)} → {ok(new_value)}")


def run_full_scan(
    client: TMDBClient,
    *,
    force: bool = False,
    resume: bool = True,
) -> None:
    """Enrich every movie in the local index that is due.

    Progress is saved as it goes: every checkpoint writes the index and
    details first and the checkpoint after, so an id in the checkpoint always
    has its enrichment on disk. The checkpoint used to be written alone --
    after an interrupt or a crash, the next run skipped those movies although
    nothing of theirs had been saved, called itself complete, and deleted
    the checkpoint.
    """
    logger.debug("Starting full scan...")
    index = load_index()
    details = load_details()

    movies = index.get("movies", {})
    if not movies:
        print("No movies in index. Run a fast scan or add films first.")
        return

    done: set[int] = _load_checkpoint() if resume else set()
    todo = []
    for key, membership in movies.items():
        movie_id = membership.get("id")
        if not movie_id:
            continue
        if movie_id in done:
            continue
        # A movie marked gone is skipped by an ordinary scan, but Force
        # re-enrich asks again: nothing else ever cleared the flag, so one
        # wrong 404 used to freeze a movie for good.
        if membership.get("gone") and not force:
            continue
        detail = details.get("movies", {}).get(key, {})
        if force or _should_enrich(membership, detail, force=force):
            todo.append((movie_id, key))

    if not todo:
        print("All movies are up to date. Nothing to enrich.")
        index["last_full_scan"] = now_iso()
        save_index(index)
        return

    word = "movie" if len(todo) == 1 else "movies"
    print(f"Enriching {len(todo)} {word}...")

    collection_cache = _LockedCache()

    image_client: Any | None = None
    enriched: list[str] = []
    gone: list[str] = []
    failed: list[str] = []
    partial: list[str] = []
    # (sort key, label, changes) -- repeated in a recap after the scan so the
    # changes don't have to be fished out of hundreds of progress lines.
    changed: list[tuple[str, str, list[tuple[str, str, str]]]] = []
    # Each worker enriches its own *copies* of a movie's two records, and only
    # a finished enrichment is put into the index, from this thread. That is
    # what makes saving mid-scan safe -- no worker is writing into the dicts
    # being serialized -- and it keeps a movie whose enrichment raised half way
    # out of the saved data entirely.
    futures: dict[concurrent.futures.Future, tuple[int, dict, dict]] = {}
    recorded: set[concurrent.futures.Future] = set()

    def _save_progress() -> None:
        # Data first, checkpoint second: the checkpoint vouches for what is on disk.
        save_index(index)
        save_details(details)
        _save_checkpoint(done)

    def _record(future: concurrent.futures.Future) -> BaseException | None:
        """Report one finished movie and merge it in; return its exception, if any."""
        recorded.add(future)
        movie_id, membership, detail = futures[future]
        label = title_link(membership) if membership.get("title") else f"#{movie_id}"
        try:
            changes = future.result()
        except Exception as exc:
            logger.error("Failed to enrich %s: %s", movie_id, exc)
            failed.append(label)
            print(f"  {err(f'✗ {label} — {exc}')}")
            # Not merged and not checkpointed, so the next scan retries it.
            return exc
        key = str(movie_id)
        index["movies"][key] = membership
        details["movies"][key] = detail
        done.add(movie_id)
        # The label carries OSC 8 link codes, which make cprint skip
        # its marker colouring -- so every row is styled explicitly.
        if membership.get("gone"):
            gone.append(label)
            print(f"  {warn(f'⚠ {label} — no longer on TMDB, marked gone')}")
        else:
            enriched.append(label)
            if "collection" in (detail.get("enrich_incomplete") or []):
                partial.append(label)
            if changes:
                changed.append((title_line(membership).casefold(), label, changes))
                _print_changed(label, changes)
            else:
                print(f"  {dim(f'✓ {label}')}")
        logger.debug("Enriched %s", movie_id)
        return None

    def _stop(executor: concurrent.futures.ThreadPoolExecutor) -> None:
        # Drop every queued movie and wait only for the ones already running.
        # Leaving the `with` block on its own waits for the whole queue: one
        # Ctrl+C on a large index still fetched every movie before it took
        # effect, and then threw all of it away.
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        finally:
            for future in futures:
                if future not in recorded and future.done() and not future.cancelled():
                    _record(future)
            _save_progress()

    try:
        image_client = httpx.Client(timeout=30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=TMDB_DETAIL_WORKERS) as executor:
            for movie_id, _key in todo:
                membership, detail = ensure_record_exists(index, details, movie_id)
                work_membership, work_detail = dict(membership), dict(detail)
                future = executor.submit(
                    _enrich_one,
                    client,
                    work_membership,
                    work_detail,
                    collection_cache,
                    image_client,
                )
                futures[future] = (movie_id, work_membership, work_detail)

            last_checkpoint = time.monotonic()
            auth_rejected = False
            try:
                for future in concurrent.futures.as_completed(futures):
                    exc = _record(future)
                    if exc is not None and is_auth_rejected(exc):
                        # A 401 means TMDB refused the API key or the session:
                        # every remaining movie would fail the same way.
                        auth_rejected = True
                        break
                    now = time.monotonic()
                    if now - last_checkpoint >= _CHECKPOINT_SECONDS:
                        _save_progress()
                        last_checkpoint = now
            except BaseException:
                # Ctrl+C, or anything unexpected: keep what finished, then stop.
                _stop(executor)
                print(warn(f"  ⚠ Stopped. {len(done)} enriched movie(s) are saved; the next scan resumes."))
                raise
            if auth_rejected:
                _stop(executor)
                print()
                print(err("✗ TMDB rejected the API key or session (HTTP 401); the scan was stopped."))
                print("  Check TMDB_API_KEY and TMDB_SESSION_ID in .env. Movies enriched before this are saved.")
                return
    finally:
        if image_client is not None:
            image_client.close()

    index["last_full_scan"] = now_iso()
    details["last_full_scan"] = now_iso()
    save_index(index)
    save_details(details)
    _save_checkpoint(set())

    if changed:
        print()
        print(alert(f"Changes since the last scan: {len(changed)}"))
        for _key, label, changes in sorted(changed, key=lambda entry: entry[0]):
            _print_changed(label, changes)
        print()

    print(success(f"Full scan complete. Enriched {len(enriched)} {'movie' if len(enriched) == 1 else 'movies'}."))
    if changed:
        print(f"  {len(changed)} had field changes (listed above).")
    if gone:
        print(f"  {warn(f'{len(gone)} marked gone (no longer on TMDB).')}")
    if failed:
        print(f"  {err(f'{len(failed)} failed and will be retried on the next scan.')}")
    if partial:
        print(
            f"  {warn(f'{len(partial)} had their collection lookup fail; the earlier collection data is kept')}"
            " and they are fetched again on the next scan."
        )
