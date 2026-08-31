"""Franchise gap detection: connected movies and TV not in the index."""

import logging
from datetime import datetime, timezone

from src.index import load_details, load_index

logger = logging.getLogger(__name__)


def _is_upcoming(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d").date()
        return date > datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _title_line(record: dict) -> str:
    title = record.get("title") or record.get("name") or "(untitled)"
    year = ""
    date = record.get("release_date") or record.get("first_air_date") or ""
    if date and len(date) >= 4:
        year = f" ({date[:4]})"
    upcoming = " (upcoming)" if _is_upcoming(date) else ""
    return f"{title}{year}{upcoming}"


def find_gaps() -> dict:
    """Find connected films and TV series not yet in the index.

    Reads only the local index and details files; zero API calls.
    Applies the keyword franchise heuristic from the design doc:
    a keyword only qualifies if it appears on at least
    FRANCHISE_KEYWORD_MIN_MOVIES films in the index, or its name shares
    a significant token with a collection name of one of the indexed films.
    """
    from config.config import FRANCHISE_KEYWORD_MIN_MOVIES

    index = load_index()
    details = load_details()
    indexed_ids = {int(k) for k in index.get("movies", {})}

    missing_films: list[dict] = []
    connected_tv: list[dict] = []

    # Compute keyword frequency across the whole index.
    keyword_movie_counts: dict[str, set[int]] = {}
    collection_names: set[str] = set()
    for key, membership in index.get("movies", {}).items():
        movie_id = int(key)
        detail = details.get("movies", {}).get(key, {})
        collection = detail.get("collection") or membership.get("collection") or {}
        if collection and collection.get("name"):
            collection_names.add(collection["name"].lower())
        for kw in detail.get("keywords", []):
            if isinstance(kw, str):
                keyword_movie_counts.setdefault(kw, set()).add(movie_id)

    def _keyword_qualifies(kw_name: str) -> bool:
        lower = kw_name.lower()
        if len(keyword_movie_counts.get(kw_name, set())) >= FRANCHISE_KEYWORD_MIN_MOVIES:
            return True
        kw_tokens = {t for t in lower.split() if len(t) > 2}
        for name in collection_names:
            name_tokens = {t for t in name.split() if len(t) > 2}
            if kw_tokens & name_tokens:
                return True
        return False

    for key, membership in index.get("movies", {}).items():
        detail = details.get("movies", {}).get(key, {})
        collection = detail.get("collection") or membership.get("collection") or {}

        if collection and collection.get("id"):
            for part in collection.get("parts", []):
                if not isinstance(part, dict):
                    continue
                part_id = part.get("id")
                if part_id and int(part_id) not in indexed_ids and part_id not in {m["id"] for m in missing_films}:
                    release = part.get("release_date", "")
                    missing_films.append(
                        {
                            "id": part_id,
                            "title": part.get("title", ""),
                            "release_date": release,
                            "upcoming": _is_upcoming(release),
                            "source": "collection",
                            "collection_id": collection.get("id"),
                            "collection_name": collection.get("name", ""),
                        }
                    )

    seen_tv: set[int] = set()
    for _key, detail in details.get("movies", {}).items():
        for item in detail.get("connected_tv", []):
            if not isinstance(item, dict):
                continue
            tv_id = item.get("id")
            via = item.get("via_keyword", "")
            if not tv_id:
                continue
            if not _keyword_qualifies(via):
                continue
            if int(tv_id) in seen_tv or int(tv_id) in indexed_ids:
                continue
            seen_tv.add(int(tv_id))
            first_air = item.get("first_air_date", "")
            connected_tv.append(
                {
                    "id": tv_id,
                    "name": item.get("name", ""),
                    "first_air_date": first_air,
                    "upcoming": _is_upcoming(first_air),
                    "via_keyword": via,
                    "source": "keyword_tv",
                }
            )

    missing_films.sort(key=lambda x: (x.get("release_date") or "", x.get("title", "")))
    connected_tv.sort(key=lambda x: (x.get("first_air_date") or "", x.get("name", "")))

    return {
        "missing_films": missing_films,
        "connected_tv": connected_tv,
        "indexed_count": len(indexed_ids),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
