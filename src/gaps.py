"""Franchise gap detection: films in your collections that are not in the index.

Movies only. An earlier build also reported TV series that shared a keyword
with an indexed film; that cluttered the report and is gone. Any
"connected_tv" data still in details.json, and "connected_tv" / "shown_tv"
in an older gaps.json, is ignored.
"""

import logging
from typing import Any

from config.config import GAPS_FILE
from src.atomic_io import atomic_write_json
from src.index import load_details, load_index, now_iso
from src.ui.reports import _is_upcoming

logger = logging.getLogger(__name__)


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


def _coerce_id_set(raw: Any) -> set[int]:
    """Return a set of ints from a JSON list, skipping unusable values."""
    ids: set[int] = set()
    for value in raw or []:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def find_gaps(*, persist: bool = True) -> dict:
    """Find the films of your movies' collections that are not yet in the index.

    Reads only the local index and details files; zero API calls.

    Every run is recomputed from the current index -- the previous report is
    never substituted for a fresh one. It is consulted only to flag each
    entry with ``is_new`` (never shown in an earlier report) or previously
    shown, so repeat runs highlight just what changed. When ``persist`` is
    true the report is written to ``GAPS_FILE`` and the shown-sets are
    updated, marking everything in this report as seen for the next run.
    """
    index = load_index()
    details = load_details()
    indexed_ids = {int(k) for k in index.get("movies", {})}

    seen_ids: set[int] = set()
    missing_films = _find_missing_collection_parts(index, details, indexed_ids, seen_ids)

    # Oldest first; undated entries (unannounced sequels) go last, not first.
    missing_films.sort(key=lambda x: (not x.get("release_date"), x.get("release_date") or "", x.get("title", "")))

    previous = load_gaps()
    prev_film_ids = _coerce_id_set(previous.get("shown_films"))
    for film in missing_films:
        film["is_new"] = film["id"] not in prev_film_ids

    result = {
        "missing_films": missing_films,
        # The shown-set snapshots exactly what this report contains, so the
        # next run treats these ids as previously shown. Films that later get
        # indexed simply vanish from it.
        "shown_films": sorted(film["id"] for film in missing_films),
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
