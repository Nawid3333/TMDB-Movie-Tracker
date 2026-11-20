"""Movies kept in the index although they are no longer on the TMDB list.

A scan that finds index movies missing from the live list asks whether to
delete them. Each one answered "keep" is written to KEPT_OFF_LIST_FILE, so it
can be found again without searching the index -- to check on TMDB whether it
left the list by mistake, was renamed, or was merged into another movie.

An entry leaves the file by itself once its movie is back on the list or no
longer in the index. That is only decided from a complete list fetch: a
movie on a page that failed to load looks exactly like one that left.
"""

import json
import logging
from collections.abc import Iterable
from pathlib import Path

import config.config as _config
from src.atomic_io import atomic_write_json
from src.index import now_iso
from src.ui.reports import movie_url, title_line

logger = logging.getLogger(__name__)

_NOTE = (
    "Movies in your index that are not on your TMDB list, which you chose to keep. "
    "Check each on TMDB: remove it from the list there, or add it back. "
    "An entry disappears once the movie is back on the list or deleted from the index."
)


def kept_file() -> Path:
    """Where the kept movies are listed."""
    return Path(_config.KEPT_OFF_LIST_FILE)


def load_kept() -> dict[str, dict]:
    """The kept entries by movie id; {} when there is no usable file."""
    path = kept_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    movies = data.get("movies") if isinstance(data, dict) else None
    if not isinstance(movies, list):
        return {}
    return {str(entry["id"]): entry for entry in movies if isinstance(entry, dict) and entry.get("id")}


def _save(entries: dict[str, dict]) -> None:
    path = kept_file()
    try:
        if not entries:
            path.unlink(missing_ok=True)
            return
        atomic_write_json(
            path,
            {
                "note": _NOTE,
                "updated_at": now_iso(),
                "list_id": _config.TMDB_LIST_ID,
                "count": len(entries),
                "movies": sorted(entries.values(), key=lambda entry: str(entry.get("title", "")).lower()),
            },
            backup=False,
        )
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def record_kept(index_movies: dict, movie_ids: Iterable[int | str]) -> int:
    """Add the movies the user chose to keep; returns how many are listed for them.

    A movie listed before keeps its first kept_since date.
    """
    entries = load_kept()
    now = now_iso()
    recorded = 0
    for movie_id in movie_ids:
        key = str(movie_id)
        movie = index_movies.get(key)
        if movie is None:
            continue
        entries[key] = {
            "id": int(key),
            "title": title_line(movie),
            "url": movie_url(key),
            "kept_since": entries.get(key, {}).get("kept_since") or now,
            "last_seen_missing": now,
            "gone_on_tmdb": bool(movie.get("gone")),
        }
        recorded += 1
    if recorded:
        _save(entries)
    return recorded


def reconcile_kept(index_movies: dict, live_ids: set[int]) -> None:
    """Drop entries whose movie is back on the list or no longer in the index.

    Call it only with the ids of a complete list fetch.
    """
    entries = load_kept()
    still = {key: entry for key, entry in entries.items() if key in index_movies and int(key) not in live_ids}
    if len(still) != len(entries):
        logger.debug("Kept-off-list file: %d entr(ies) resolved", len(entries) - len(still))
        _save(still)
