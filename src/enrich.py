"""Full-scan enrichment: fetch detailed movie records and related data."""

import concurrent.futures
import contextlib
import json
import logging
import threading
import time
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
from src.tmdb_api import TMDBClient, pick_certification

logger = logging.getLogger(__name__)

_APPEND_TO_RESPONSE = "credits,keywords,external_ids,release_dates,videos,watch/providers,recommendations,similar"


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
    tier = _volatility_tier(record)
    if tier in ("hot", "warm"):
        return True
    enriched_at = details.get("enriched_at", "")
    if not enriched_at:
        return True
    try:
        enriched = datetime.fromisoformat(enriched_at.replace("Z", "+00:00"))
        days = (datetime.now(UTC) - enriched).days
    except ValueError:
        return True
    if tier == "cool":
        return days >= 7
    if tier == "cold":
        return days >= COLD_REENRICH_DAYS
    return True


def _fetch_collection(client: TMDBClient, collection_id: int) -> dict | None:
    try:
        resp = client.get(f"/collection/{collection_id}")
        resp.raise_for_status()
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


def _fetch_keyword_tv(client: TMDBClient, keyword_id: int) -> list[dict]:
    """Fetch TV series for a keyword via discover/tv."""
    try:
        resp = client.get("/discover/tv", params={"with_keywords": keyword_id})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        return [
            {"id": r.get("id"), "name": r.get("name"), "first_air_date": r.get("first_air_date")}
            for r in results
            if isinstance(r, dict) and r.get("id")
        ]
    except Exception as exc:
        logger.warning("Keyword TV fetch failed for %s: %s", keyword_id, exc)
        return []


class _LockedCache:
    """Small thread-safe cache wrapper used by the worker pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # None is a cached value, not a miss: a fetch that came back empty is
        # remembered so the worker pool does not ask for it again.
        self._data: dict[int, dict | None] = {}

    def get(self, key: int) -> dict | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: int, value: dict | None) -> None:
        with self._lock:
            self._data[key] = value


class _LockedListCache:
    """Thread-safe cache for list-of-dict values."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[int, list[dict]] = {}

    def get(self, key: int) -> list[dict] | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: int, value: list[dict]) -> None:
        with self._lock:
            self._data[key] = value


def _resolve_collection(
    client: TMDBClient,
    membership: dict,
    details: dict,
    collection_cache: _LockedCache,
) -> None:
    collection_id = (details.get("collection") or {}).get("id")
    if not collection_id:
        return
    cached = collection_cache.get(collection_id)
    if cached is None:
        collection_cache.set(collection_id, _fetch_collection(client, collection_id))
        cached = collection_cache.get(collection_id)
    if cached:
        details["collection"] = cached


def _resolve_connected_tv(
    client: TMDBClient,
    membership: dict,
    details: dict,
    keyword_tv_cache: _LockedListCache,
) -> None:
    keywords = details.get("keywords", [])
    if not keywords:
        return
    connected: list[dict] = []
    for kw in keywords:
        if not isinstance(kw, dict):
            continue
        kw_id = kw.get("id")
        kw_name = kw.get("name", "")
        if not kw_id:
            continue
        cached = keyword_tv_cache.get(kw_id)
        if cached is None:
            keyword_tv_cache.set(kw_id, _fetch_keyword_tv(client, kw_id))
            cached = keyword_tv_cache.get(kw_id)
        for series in cached or []:
            connected.append(
                {
                    "id": series["id"],
                    "name": series["name"],
                    "first_air_date": series.get("first_air_date"),
                    "via_keyword": kw_name,
                }
            )
    # Deduplicate by series id, preserving first via_keyword.
    seen = set()
    deduped = []
    for item in connected:
        key = item["id"]
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    details["connected_tv"] = deduped


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


def _enrich_one(
    client: TMDBClient,
    membership: dict,
    details: dict,
    collection_cache: _LockedCache,
    keyword_tv_cache: _LockedListCache,
    image_client: Any,
) -> None:
    """Fetch and merge full details for a single movie."""
    movie_id = membership["id"]
    params = {"language": TMDB_LANGUAGE, "append_to_response": _APPEND_TO_RESPONSE}
    resp = client.get(f"/movie/{movie_id}", params=params)
    if resp.status_code == 404:
        # TMDB merged or deleted the record. Mark gone but keep data.
        membership["gone"] = True
        membership["gone_since"] = membership.get("gone_since") or now_iso()
        logger.info("Movie %s no longer resolves on TMDB; marked gone", movie_id)
        return
    resp.raise_for_status()
    movie = resp.json()
    if not isinstance(movie, dict):
        raise ValueError(f"Movie {movie_id} returned non-object body")

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

    details["keywords"] = [k.get("name") for k in movie.get("keywords", {}).get("keywords", []) if isinstance(k, dict)]

    _resolve_collection(client, membership, details, collection_cache)
    _resolve_connected_tv(client, membership, details, keyword_tv_cache)

    # Poster cache.
    if membership.get("poster_path"):
        poster_file = download_poster(image_client, movie_id, membership["poster_path"])
        membership["poster_file"] = str(poster_file) if poster_file else None

    details["enriched_at"] = now_iso()


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


def run_full_scan(
    client: TMDBClient,
    *,
    force: bool = False,
    resume: bool = True,
) -> None:
    """Enrich every movie in the local index that is due."""
    logger.info("Starting full scan...")
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
        if movie_id in done or membership.get("gone"):
            continue
        detail = details.get("movies", {}).get(key, {})
        if force or _should_enrich(membership, detail, force=force):
            todo.append((movie_id, key))

    if not todo:
        print("All movies are up to date. Nothing to enrich.")
        index["last_full_scan"] = now_iso()
        save_index(index)
        return

    collection_cache = _LockedCache()
    keyword_tv_cache = _LockedListCache()

    image_client: Any | None = None
    try:
        image_client = httpx.Client(timeout=30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=TMDB_DETAIL_WORKERS) as executor:
            futures = {}
            for movie_id, _key in todo:
                membership, detail = ensure_record_exists(index, details, movie_id)
                future = executor.submit(
                    _enrich_one,
                    client,
                    membership,
                    detail,
                    collection_cache,
                    keyword_tv_cache,
                    image_client,
                )
                futures[future] = movie_id

            checkpoint_interval = 60.0  # seconds
            last_checkpoint = time.monotonic()
            for future in concurrent.futures.as_completed(futures):
                movie_id = futures[future]
                try:
                    future.result()
                    done.add(movie_id)
                    now = time.monotonic()
                    if now - last_checkpoint >= checkpoint_interval:
                        _save_checkpoint(done)
                        last_checkpoint = now
                    logger.debug("Enriched %s", movie_id)
                except Exception as exc:
                    logger.error("Failed to enrich %s: %s", movie_id, exc)
                    # Do not add to checkpoint so resume can retry this movie.
    finally:
        if image_client is not None:
            image_client.close()

    index["last_full_scan"] = now_iso()
    details["last_full_scan"] = now_iso()
    save_index(index)
    save_details(details)
    _save_checkpoint(set())
    word = "movie" if len(todo) == 1 else "movies"
    print(f"Full scan complete. Enriched {len(todo)} {word}.")
