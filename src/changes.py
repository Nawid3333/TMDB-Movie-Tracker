"""Change detection and approved-only merge for the local index."""

import logging
from dataclasses import dataclass, field

from src.index import validate_membership_record

logger = logging.getLogger(__name__)


@dataclass
class ChangeSet:
    """Proposed changes from a fast scan."""

    additions: dict[str, dict] = field(default_factory=dict)
    removals: dict[str, dict] = field(default_factory=dict)
    gone: dict[str, dict] = field(default_factory=dict)
    unchanged: dict[str, dict] = field(default_factory=dict)
    incomplete: bool = False
    current_count: int = 0
    proposed_count: int = 0


def _extract_list_movie(item: dict) -> dict | None:
    """Normalize a TMDB list item into a membership record."""
    if not isinstance(item, dict):
        return None
    # TMDB /3/list/{id} items wrap the movie under "media_type" == "movie".
    if item.get("media_type") != "movie":
        return None
    movie = item
    movie_id = movie.get("id")
    if not movie_id:
        return None
    record = {
        "id": int(movie_id),
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
    try:
        validate_membership_record(record)
    except ValueError as exc:
        logger.warning("Skipping invalid list item %s: %s", movie_id, exc)
        return None
    return record


def detect_changes(
    current: dict[str, dict],
    fetched: list[dict],
    *,
    incomplete: bool = False,
    min_shrink_ratio: float = 0.5,
) -> ChangeSet:
    """Compare fetched list membership against the current index.

    Returns a ChangeSet with additions, removals, and unchanged ids.
    The shrink sanity gate is enforced here: if the proposed set is too
    small relative to the current index, removals are cleared and the set
    is marked incomplete so the caller can demand explicit confirmation.
    """
    proposed: dict[str, dict] = {}
    for item in fetched:
        record = _extract_list_movie(item)
        if not record:
            continue
        key = str(record["id"])
        proposed[key] = record

    current_ids = set(current.keys())
    proposed_ids = set(proposed.keys())

    additions = {k: proposed[k] for k in proposed_ids - current_ids}
    removals = {k: current[k] for k in current_ids - proposed_ids if not current[k].get("gone")}
    unchanged = {k: current[k] for k in current_ids & proposed_ids}

    change_set = ChangeSet(
        additions=additions,
        removals=removals,
        unchanged=unchanged,
        incomplete=incomplete,
        current_count=len(current),
        proposed_count=len(proposed),
    )

    # Sanity gate: a scan that is incomplete or shrunk too far cannot
    # justify removals. Clearing removals forces the user to review manually.
    if incomplete:
        logger.warning("Scan was incomplete; blocking all removal proposals")
        change_set.removals = {}
        return change_set

    if current and len(proposed) < max(1, int(len(current) * min_shrink_ratio)):
        logger.warning(
            "Shrink gate triggered: proposed count %d is below %0.0f%% of current %d; removals blocked",
            len(proposed),
            min_shrink_ratio * 100,
            len(current),
        )
        change_set.removals = {}
        change_set.incomplete = True
        return change_set

    return change_set


def apply_changes(
    current: dict[str, dict],
    change_set: ChangeSet,
    *,
    approve_additions: bool = False,
    approve_removals: bool = False,
) -> dict[str, dict]:
    """Return a new index dict after applying only approved changes."""
    merged = dict(current)
    if approve_additions:
        for key, record in change_set.additions.items():
            merged[key] = dict(record)
    if approve_removals:
        for key in change_set.removals:
            merged.pop(key, None)
    return merged
