"""Search and add logic for finding films and adding them to the index."""

import logging
import re

import httpx

from config.config import TMDB_LANGUAGE
from src.index import (
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

_TMDB_URL_RE = re.compile(r"tmdb\.org/movie/(\d+)")
_IMDB_URL_RE = re.compile(r"imdb\.com/title/(tt\d+)")
_IMDB_ID_RE = re.compile(r"^tt\d+$")
_TMDB_ID_RE = re.compile(r"^\d+$")


def _parse_user_input(query: str) -> dict | None:
    """Sniff a TMDB id, TMDB URL, IMDb id, or IMDb URL from the input."""
    query = query.strip()
    if not query:
        return None

    tmdb_match = _TMDB_URL_RE.search(query)
    if tmdb_match:
        return {"type": "tmdb_id", "value": int(tmdb_match.group(1))}

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
    return {
        "id": int(movie["id"]),
        "title": (movie.get("title") or "").strip(),
        "title_original": (movie.get("original_title") or "").strip(),
        "title_english": "",
        "title_german": "",
        "release_date": (movie.get("release_date") or "").strip(),
        "poster_path": movie.get("poster_path") or None,
        "poster_file": None,
        "status": "",
        "gone": False,
        "remote_push": "skipped",
    }


def search_movies(
    client: TMDBClient,
    query: str,
    *,
    year: str | None = None,
    page: int = 1,
) -> list[dict]:
    """Search TMDB for movies by title or year."""
    parsed = _parse_user_input(query)
    if parsed is None:
        return []

    if parsed["type"] == "tmdb_id":
        try:
            resp = client.get(
                f"/movie/{parsed['value']}",
                params={"language": TMDB_LANGUAGE},
            )
            resp.raise_for_status()
            movie = resp.json()
            if isinstance(movie, dict) and movie.get("id"):
                return [_extract_movie_basic(movie)]
        except Exception as exc:
            logger.warning("Direct TMDB id lookup failed: %s", exc)
        return []

    if parsed["type"] == "imdb_id":
        try:
            resp = client.get(
                f"/find/{parsed['value']}",
                params={"external_source": "imdb_id", "language": TMDB_LANGUAGE},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("movie_results", []) if isinstance(data, dict) else []
            return [_extract_movie_basic(m) for m in results if isinstance(m, dict) and m.get("id")]
        except Exception as exc:
            logger.warning("IMDb id lookup failed: %s", exc)
        return []

    params: dict[str, str | int] = {
        "query": parsed["value"],
        "language": TMDB_LANGUAGE,
        "page": page,
        "include_adult": "false",
    }
    if year:
        params["year"] = year
    try:
        resp = client.get("/search/movie", params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        return [_extract_movie_basic(m) for m in results if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return []


def fetch_full_movie(client: TMDBClient, movie_id: int, image_client: httpx.Client) -> tuple[dict, dict] | None:
    """Fetch full details for a movie and return (membership, details) dicts."""
    from src.enrich import _enrich_one

    index = load_index()
    details = load_details()
    membership, detail = ensure_record_exists(index, details, movie_id)
    collection_cache: dict[int, dict] = {}
    keyword_tv_cache: dict[int, list[dict]] = {}
    try:
        _enrich_one(client, membership, detail, collection_cache, keyword_tv_cache, image_client)
        return membership, detail
    except Exception as exc:
        logger.error("Could not enrich movie %s: %s", movie_id, exc)
        return None


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


def push_to_tmdb_list(client: TMDBClient, list_id: str | int, movie_id: int) -> dict:
    """Add a movie to the remote TMDB list.

    Returns a dict with 'success' bool and 'remote_push' status string.
    """
    if not client.session_id:
        return {"success": False, "reason": "no session", "remote_push": "skipped"}
    try:
        resp = client.post(
            f"/list/{list_id}/add_item",
            params={"session_id": client.session_id},
            json_body={"media_id": movie_id},
        )
        resp.raise_for_status()
        body = resp.json()
        status_code = body.get("status_code") if isinstance(body, dict) else None
        if status_code == 12:
            return {"success": True, "reason": "ok", "remote_push": "ok"}
        reason = body.get("status_message") if isinstance(body, dict) else "unknown"
        return {"success": False, "reason": reason, "remote_push": "failed"}
    except Exception as exc:
        logger.error("Remote push failed: %s", exc)
        return {"success": False, "reason": str(exc), "remote_push": "failed"}
