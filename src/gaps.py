"""Franchise gap detection: connected movies and TV not in the index."""

import logging
from collections import Counter
from typing import Any

from config.config import FRANCHISE_KEYWORD_MIN_MOVIES, GAPS_FILE
from src.atomic_io import atomic_write_json
from src.index import load_details, load_index, now_iso
from src.ui.reports import _is_upcoming

logger = logging.getLogger(__name__)


def _keyword_qualifies(
    kw_name: str,
    keyword_counts: Counter,
    collection_tokens: set[frozenset[str]],
    min_movies: int = FRANCHISE_KEYWORD_MIN_MOVIES,
) -> bool:
    """Return True when a keyword looks like a franchise anchor.

    A keyword qualifies when it appears on at least ``min_movies`` indexed
    movies, or when any significant token (>2 chars) overlaps with a
    collection name in the index.
    """
    if keyword_counts.get(kw_name, 0) >= min_movies:
        return True
    lower = kw_name.lower()
    kw_tokens = {t for t in lower.split() if len(t) > 2}
    return any(kw_tokens & name_tokens for name_tokens in collection_tokens)


def _normalized_keyword_counts(index: dict, details: dict) -> Counter:
    """Count how many indexed movies carry each keyword name."""
    counts: Counter = Counter()
    for key in index.get("movies", {}):
        detail = details.get("movies", {}).get(key, {})
        for kw in detail.get("keywords", []):
            if isinstance(kw, str):
                counts[kw] += 1
    return counts


def _collection_name_tokens(index: dict, details: dict) -> set[frozenset[str]]:
    """Return significant token sets for every collection name in the index."""
    tokens: set[frozenset[str]] = set()
    for key, membership in index.get("movies", {}).items():
        detail = details.get("movies", {}).get(key, {})
        collection = detail.get("collection") or membership.get("collection") or {}
        name = (collection.get("name") or "").lower()
        if name:
            tokens.add(frozenset(t for t in name.split() if len(t) > 2))
    return tokens


def _find_missing_collection_parts(
    index: dict,
    details: dict,
    indexed_ids: set[int],
    seen_ids: set[int],
) -> list[dict]:
    """Return collection parts that are not already in the index."""
    missing: list[dict] = []
    for key, membership in index.get("movies", {}).items():
        detail = details.get("movies", {}).get(key, {})
        collection = detail.get("collection") or membership.get("collection") or {}

        collection_id = collection.get("id")
        if not collection_id:
            continue

        collection_name = collection.get("name", "")
        for part in collection.get("parts", []):
            if not isinstance(part, dict):
                continue
            part_id = part.get("id")
            if not part_id:
                continue
            part_id_int = int(part_id)
            if part_id_int in indexed_ids or part_id_int in seen_ids:
                continue
            seen_ids.add(part_id_int)
            release = part.get("release_date", "")
            missing.append(
                {
                    "id": part_id_int,
                    "title": part.get("title", ""),
                    "release_date": release,
                    "upcoming": _is_upcoming(release),
                    "source": "collection",
                    "collection_id": collection_id,
                    "collection_name": collection_name,
                }
            )
    return missing


def _find_connected_tv(
    details: dict,
    indexed_ids: set[int],
    keyword_counts: Counter,
    collection_tokens: set[frozenset[str]],
) -> list[dict]:
    """Return connected TV series that pass the keyword franchise filter."""
    tv: list[dict] = []
    seen_tv: set[int] = set()
    for detail in details.get("movies", {}).values():
        for item in detail.get("connected_tv", []):
            if not isinstance(item, dict):
                continue
            tv_id = item.get("id")
            via = item.get("via_keyword", "")
            if not tv_id or not via:
                continue
            tv_id_int = int(tv_id)
            if tv_id_int in seen_tv or tv_id_int in indexed_ids:
                continue
            if not _keyword_qualifies(via, keyword_counts, collection_tokens):
                continue
            seen_tv.add(tv_id_int)
            first_air = item.get("first_air_date", "")
            tv.append(
                {
                    "id": tv_id_int,
                    "name": item.get("name", ""),
                    "first_air_date": first_air,
                    "upcoming": _is_upcoming(first_air),
                    "via_keyword": via,
                    "source": "keyword_tv",
                }
            )
    return tv


def find_gaps(*, persist: bool = True) -> dict:
    """Find connected films and TV series not yet in the index.

    Reads only the local index and details files; zero API calls.
    Applies the keyword franchise heuristic from the design doc:
    a keyword only qualifies if it appears on at least
    FRANCHISE_KEYWORD_MIN_MOVIES films in the index, or its name shares
    a significant token with a collection name of one of the indexed films.

    The result is optionally written to ``GAPS_FILE``.
    """
    index = load_index()
    details = load_details()
    indexed_ids = {int(k) for k in index.get("movies", {})}

    keyword_counts = _normalized_keyword_counts(index, details)
    collection_tokens = _collection_name_tokens(index, details)

    seen_ids: set[int] = set()
    missing_films = _find_missing_collection_parts(index, details, indexed_ids, seen_ids)
    connected_tv = _find_connected_tv(details, indexed_ids, keyword_counts, collection_tokens)

    missing_films.sort(key=lambda x: (x.get("release_date") or "", x.get("title", "")))
    connected_tv.sort(key=lambda x: (x.get("first_air_date") or "", x.get("name", "")))

    result = {
        "missing_films": missing_films,
        "connected_tv": connected_tv,
        "indexed_count": len(indexed_ids),
        "generated_at": now_iso(),
    }

    if persist:
        try:
            atomic_write_json(GAPS_FILE, result, backup=True)
        except Exception as exc:
            logger.warning("Could not persist gaps report: %s", exc)

    return result


def load_gaps() -> dict[str, Any]:
    """Load the most recently persisted gaps report, if any."""
    try:
        with open(GAPS_FILE, encoding="utf-8") as f:
            data: dict[str, Any] = __import__("json").load(f)
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not load gaps report: %s", exc)
        return {}
