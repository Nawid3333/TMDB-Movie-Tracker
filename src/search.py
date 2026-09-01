"""Search and add logic for finding films and adding them to the index."""

import logging
import re

import httpx

from config.config import TMDB_LANGUAGE
from src.index import (
    build_membership_record,
    ensure_record_exists,
    load_details,
    load_index,
    now_iso,
    save_details,
    save_index,
    validate_membership_record,
)
from src.tmdb_api import TMDBClient

logger = logging.getLogger(__name__)

_TMDB_URL_RE = re.compile(r"(?:www\.)?(?:themoviedb|tmdb)\.org/movie/(\d+)")
_IMDB_URL_RE = re.compile(r"imdb\.com/title/(tt\d+)")
_IMDB_ID_RE = re.compile(r"^tt\d+$")
_TMDB_ID_RE = re.compile(r"^\d+$")

__all__ = [
    "EnrichError",
    "ParseError",
    "SearchError",
    "add_movie_locally",
    "fetch_full_movie",
    "push_to_tmdb_list",
    "resolve_movie_id_from_input",
    "resolve_movie_ids_from_file",
    "search_movies",
]


class SearchError(Exception):
    """Raised when a movie search fails for a reason other than 'no results'."""


class ParseError(Exception):
    """Raised when the user input cannot be parsed as a valid query."""


class EnrichError(Exception):
    """Raised when fetch_full_movie cannot enrich a known or new record."""


def _parse_user_input(query: str) -> dict:
    """Sniff a TMDB id, TMDB URL, IMDb id, or IMDb URL from the input.

    Raises ParseError when the input looks like a malformed id/URL.
    """
    query = query.strip()
    if not query:
        raise ParseError("Empty query")

    tmdb_match = _TMDB_URL_RE.search(query)
    if tmdb_match:
        value = tmdb_match.group(1)
        if not value.isdigit():
            raise ParseError(f"Invalid TMDB movie id in URL: {value!r}")
        return {"type": "tmdb_id", "value": int(value)}

    imdb_match = _IMDB_URL_RE.search(query)
    if imdb_match:
        return {"type": "imdb_id", "value": imdb_match.group(1)}

    if _IMDB_ID_RE.match(query):
        return {"type": "imdb_id", "value": query}

    if _TMDB_ID_RE.match(query):
        return {"type": "tmdb_id", "value": int(query)}

    return {"type": "title", "value": query}


def _extract_movie_basic(movie: dict) -> dict:
    """Build a membership record from a TMDB movie summary."""
    return build_membership_record(movie)


def _lookup(
    client: TMDBClient,
    endpoint: str,
    params: dict[str, str | int],
    extract: dict,
) -> list[dict]:
    """Generic GET helper with consistent error wrapping.

    `extract` should contain the JSON key holding the list of results, e.g.
    {"key": "movie_results"} or {"key": "results"}. For direct item lookups
    use {"direct": True}.
    """
    try:
        resp = client.get(endpoint, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        logger.error("%s failed: %s", endpoint, exc)
        raise SearchError(f"{endpoint} failed: {exc}") from exc
    except Exception as exc:
        logger.error("%s failed: %s", endpoint, exc)
        raise SearchError(f"{endpoint} failed: {exc}") from exc

    data = resp.json()
    if not isinstance(data, dict):
        return []

    if extract.get("direct"):
        movie = data
        return [_extract_movie_basic(movie)] if isinstance(movie, dict) and movie.get("id") else []

    key = extract.get("key", "results")
    results = data.get(key, [])
    return [_extract_movie_basic(m) for m in results if isinstance(m, dict) and m.get("id")]


def search_movies(
    client: TMDBClient,
    query: str,
    *,
    year: str | None = None,
    page: int = 1,
) -> list[dict]:
    """Search TMDB for movies by title or year.

    Raises SearchError when the API call fails or the query is malformed.
    Returns an empty list only when the API responded successfully with no matches.
    """
    try:
        parsed = _parse_user_input(query)
    except ParseError as exc:
        logger.warning("Search parse error: %s", exc)
        raise SearchError(str(exc)) from exc

    if parsed["type"] == "tmdb_id":
        return _lookup(
            client,
            f"/movie/{parsed['value']}",
            {"language": TMDB_LANGUAGE},
            {"direct": True},
        )

    if parsed["type"] == "imdb_id":
        return _lookup(
            client,
            f"/find/{parsed['value']}",
            {"external_source": "imdb_id", "language": TMDB_LANGUAGE},
            {"key": "movie_results"},
        )

    params: dict[str, str | int] = {
        "query": parsed["value"],
        "language": TMDB_LANGUAGE,
        "page": page,
        "include_adult": "false",
    }
    if year:
        params["year"] = year
    return _lookup(client, "/search/movie", params, {"key": "results"})


def fetch_full_movie(client: TMDBClient, movie_id: int, image_client: httpx.Client) -> tuple[dict, dict]:
    """Fetch full details for a movie and return (membership, details) dicts.

    Raises:
        EnrichError: when the record cannot be resolved or enriched.
        httpx.HTTPError, ValueError: passed through for unexpected failures.
    """
    from src.enrich import _enrich_one, _LockedCache, _LockedListCache

    index = load_index()
    details = load_details()
    membership, detail = ensure_record_exists(index, details, movie_id)
    collection_cache = _LockedCache()
    keyword_tv_cache = _LockedListCache()
    try:
        _enrich_one(client, membership, detail, collection_cache, keyword_tv_cache, image_client)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # _enrich_one already marks the membership gone and returns, so
            # this branch only fires if that convention changes in the future.
            membership["gone"] = True
            membership["gone_since"] = membership.get("gone_since") or now_iso()
        raise EnrichError(f"Could not enrich movie {movie_id}: {exc}") from exc
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise EnrichError(f"Network error while enriching movie {movie_id}: {exc}") from exc
    except ValueError as exc:
        raise EnrichError(f"Invalid response for movie {movie_id}: {exc}") from exc
    return membership, detail


def add_movie_locally(membership: dict, detail: dict) -> None:
    """Write a movie to the local index and details files."""
    validate_membership_record(membership)
    index = load_index()
    details = load_details()
    key = str(membership["id"])
    if "added_at" not in membership:
        membership["added_at"] = now_iso()
    index["movies"][key] = membership
    details["movies"][key] = detail
    save_index(index)
    save_details(details)


def _resolve_tmdb_id(
    client: TMDBClient,
    parsed: dict,
    *,
    language: str = TMDB_LANGUAGE,
) -> list[dict]:
    r"""Resolve a parsed query dict to a list of membership records.

    Mirrors the lookup logic in ``search_movies`` without the title path,
    since a batch file should contain direct ids/URLs only.
    """
    if parsed["type"] == "tmdb_id":
        return _lookup(
            client,
            f"/movie/{parsed['value']}",
            {"language": language},
            {"direct": True},
        )

    if parsed["type"] == "imdb_id":
        return _lookup(
            client,
            f"/find/{parsed['value']}",
            {"external_source": "imdb_id", "language": language},
            {"key": "movie_results"},
        )

    return []


def resolve_movie_id_from_input(
    client: TMDBClient,
    raw: str,
    *,
    language: str = TMDB_LANGUAGE,
) -> dict | None:
    """Resolve a single TMDB/IMDb URL or raw id to a membership record.

    Returns ``None`` when the input is blank, a comment, a title search,
    or cannot be resolved to a movie.
    """
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None

    try:
        parsed = _parse_user_input(raw)
    except ParseError:
        return None

    if parsed["type"] == "title":
        return None

    try:
        results = _resolve_tmdb_id(client, parsed, language=language)
    except SearchError:
        return None

    return results[0] if results else None


def resolve_movie_ids_from_file(
    client: TMDBClient,
    file_path: str,
    *,
    language: str = TMDB_LANGUAGE,
) -> tuple[list[dict], list[tuple[int, str, str]]]:
    r"""Read a text file and resolve each line to a TMDB movie record.

    Lines may be:
      - a raw TMDB numeric id
      - a TMDB movie URL (``themoviedb.org/movie/<id>`` or ``tmdb.org/movie/<id>``)
      - an IMDb id (``tt\d+``)
      - an IMDb URL (``imdb.com/title/tt\d+``)

    Empty lines and lines starting with ``#`` are ignored. Lines that cannot
    be parsed are returned in the ``skipped`` list without hitting the API.

    Returns:
        A tuple ``(records, skipped)`` where ``records`` is a list of
        membership dicts and ``skipped`` is a list of
        ``(line_number, raw_line, reason)`` tuples.
    """
    records: list[dict] = []
    skipped: list[tuple[int, str, str]] = []

    try:
        with open(file_path, encoding="utf-8") as fh:
            lines = list(enumerate(fh, 1))
    except OSError as exc:
        logger.error("Could not read %s: %s", file_path, exc)
        raise SearchError(f"Could not read {file_path}: {exc}") from exc

    seen: set[int] = set()
    for line_num, line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        record = resolve_movie_id_from_input(client, raw, language=language)
        if record is None:
            skipped.append((line_num, raw, "could not resolve to a movie id/URL"))
            continue

        movie_id = int(record["id"])
        if movie_id in seen:
            skipped.append((line_num, raw, "duplicate id in file"))
            continue
        seen.add(movie_id)
        records.append(record)

    return records, skipped


def push_to_tmdb_list(client: TMDBClient, list_id: str | int, movie_id: int) -> dict:
    """Add a movie to the remote TMDB list.

    Returns a dict with 'success' bool and 'remote_push' status string.
    """
    if not client.session_id:
        return {"success": False, "reason": "no session", "remote_push": "skipped"}
    try:
        resp = client.post(
            f"/list/{list_id}/add_item",
            json_body={"media_id": movie_id},
        )
        resp.raise_for_status()
        body = resp.json()
        status_code = body.get("status_code") if isinstance(body, dict) else None
        if status_code == 12:
            return {"success": True, "reason": "ok", "remote_push": "ok"}
        reason = body.get("status_message") if isinstance(body, dict) else "unknown"
        return {"success": False, "reason": reason, "remote_push": "failed"}
    except httpx.HTTPError as exc:
        logger.error("Remote push failed: %s", exc)
        return {"success": False, "reason": str(exc), "remote_push": "failed"}
