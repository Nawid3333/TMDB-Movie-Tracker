"""Local index management: load, validate, merge, and save."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from config.config import DETAILS_FILE, INDEX_FILE, TMDB_LIST_ID
from src.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

CURRENT_INDEX_SCHEMA = 1
CURRENT_DETAILS_SCHEMA = 1


def _try_load_with_backup(filepath: Path) -> dict:
    """Load JSON, falling back through .bak1-3 on corruption."""
    candidates = [str(filepath)] + [f"{filepath}.bak{i}" for i in range(1, 4)]
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            if candidate != str(filepath):
                logger.warning("Loaded %s from backup %s", filepath.name, candidate)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load %s: %s", candidate, exc)
            continue
    return {}


def _validate_schema(data: dict, current: int, kind: str) -> None:
    version = data.get("schema_version")
    if version is None and data:
        # Backwards-compatible default: assume v1 if missing but data exists.
        return
    if version is not None and version != current:
        raise ValueError(f"Unsupported {kind} schema_version: {version} (expected {current})")


def _ensure_index_structure(data: dict) -> dict:
    """Return a valid index dict with all required keys."""
    return {
        "schema_version": CURRENT_INDEX_SCHEMA,
        "list_id": data.get("list_id") if data.get("list_id") else int(TMDB_LIST_ID) if TMDB_LIST_ID else 0,
        "list_name": data.get("list_name", ""),
        "last_fast_scan": data.get("last_fast_scan", ""),
        "last_full_scan": data.get("last_full_scan", ""),
        "movies": data.get("movies", {}),
        "ignored": data.get("ignored", {}),
        "meta": {
            "incomplete_last_scan": data.get("meta", {}).get("incomplete_last_scan", False),
        },
    }


def _ensure_details_structure(data: dict) -> dict:
    return {
        "schema_version": CURRENT_DETAILS_SCHEMA,
        "last_full_scan": data.get("last_full_scan", ""),
        "movies": data.get("movies", {}),
    }


def load_index() -> dict:
    """Load the membership index, creating defaults if missing."""
    data = _try_load_with_backup(INDEX_FILE)
    _validate_schema(data, CURRENT_INDEX_SCHEMA, "index")
    return _ensure_index_structure(data)


def save_index(data: dict) -> None:
    """Atomically save the membership index."""
    payload = _ensure_index_structure(data)
    atomic_write_json(INDEX_FILE, payload)


def load_details() -> dict:
    """Load the enrichment details file."""
    data = _try_load_with_backup(DETAILS_FILE)
    _validate_schema(data, CURRENT_DETAILS_SCHEMA, "details")
    return _ensure_details_structure(data)


def save_details(data: dict) -> None:
    """Atomically save the enrichment details file."""
    payload = _ensure_details_structure(data)
    atomic_write_json(DETAILS_FILE, payload)


def validate_movie_id(movie_id: int | str) -> int:
    """Ensure a movie id is a positive integer."""
    try:
        value = int(movie_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Movie id must be an integer, got {movie_id!r}") from exc
    if value <= 0:
        raise ValueError(f"Movie id must be positive, got {value}")
    return value


def validate_membership_record(record: dict) -> None:
    """Validate a movie record before it enters the index."""
    if not isinstance(record, dict):
        raise ValueError("Movie record must be a dict")
    validate_movie_id(record.get("id", 0))
    title = record.get("title")
    if not title or not isinstance(title, str):
        raise ValueError(f"Movie {record.get('id')} has no title")
    release_date = record.get("release_date", "")
    if release_date and not isinstance(release_date, str):
        raise ValueError(f"Movie {record.get('id')} has invalid release_date")


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_record_exists(index: dict, details: dict, movie_id: int) -> tuple[dict, dict]:
    """Return (membership, enrichment) for a movie, creating empty dicts if needed."""
    key = str(movie_id)
    if key not in index["movies"]:
        index["movies"][key] = {"id": movie_id}
    if key not in details["movies"]:
        details["movies"][key] = {"id": movie_id}
    return index["movies"][key], details["movies"][key]
