"""Fetch and cache a TMDB custom list with pagination."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config.config import DATA_DIR, TMDB_LIST_ID
from src.atomic_io import atomic_write_json
from src.tmdb_api import TMDBClient

logger = logging.getLogger(__name__)


class ListFetchError(Exception):
    """Raised when the list cannot be fetched or assembled."""


def _list_cache_path(list_id: str | int, data_dir: Path) -> Path:
    return data_dir / f"list_{list_id}_fast.json"


def _fetch_page(client: TMDBClient, list_id: str | int, page: int, *, auth: bool = False) -> dict:
    """Fetch a single page of the list."""
    resp = client.get(f"/list/{list_id}", params={"page": page}, auth=auth)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ListFetchError(f"List {list_id} page {page} returned non-object body")
    return data


def fetch_list(
    client: TMDBClient,
    list_id: str | int | None = None,
    *,
    cache_path: Path | None = None,
    use_session_on_private: bool = True,
) -> tuple[list[dict], bool]:
    """Fetch all pages of a TMDB list.

    Returns (items, incomplete). Incomplete is True if any page failed.
    The raw last-page payload is cached for inspection.
    """
    list_id = list_id or TMDB_LIST_ID
    if not list_id:
        raise ListFetchError("TMDB_LIST_ID is not configured")
    list_id = str(list_id)

    def _try_fetch(auth: bool) -> tuple[list[dict], bool]:
        items: list[dict] = []
        page = 1
        total_pages = 1
        incomplete = False

        while page <= total_pages:
            try:
                data = _fetch_page(client, list_id, page, auth=auth)
            except Exception as exc:
                logger.error("Failed to fetch list page %d: %s", page, exc)
                incomplete = True
                break

            items.extend(data.get("items", []))

            # Derive page count from item_count when total_pages is unreliable.
            item_count = data.get("item_count")
            if isinstance(item_count, int) and item_count > 0:
                derived_total = (item_count + 19) // 20  # 20 per page
                total_pages = max(total_pages, derived_total)

            reported_total = data.get("total_pages")
            if isinstance(reported_total, int) and reported_total > 0:
                if reported_total != total_pages and page == 1:
                    logger.debug(
                        "total_pages (%d) differs from item_count derived (%d); using derived",
                        reported_total,
                        total_pages,
                    )
                total_pages = max(total_pages, reported_total)

            page += 1
            if page > 500:
                logger.warning("List pagination capped at 500 pages / 10,000 items")
                incomplete = True
                break

        return items, incomplete

    items, incomplete = _try_fetch(auth=False)

    # If API-key-only failed entirely, try with a session (private list).
    if not items and use_session_on_private:
        logger.info("No items fetched without session; attempting session upgrade")
        session_id = client.ensure_session()
        if session_id:
            items, incomplete = _try_fetch(auth=True)
        else:
            logger.warning("No session available; private list reads will fail")

    if cache_path is None:
        cache_path = _list_cache_path(list_id, DATA_DIR)

    try:
        cache_payload = {
            "list_id": list_id,
            "cached_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "item_count": len(items),
            "incomplete": incomplete,
            "items": items,
        }
        atomic_write_json(cache_path, cache_payload)
    except Exception as exc:
        logger.warning("Could not cache list payload: %s", exc)

    return items, incomplete


def load_cached_list(list_id: str | int, data_dir: Path = DATA_DIR) -> dict | None:
    """Load the most recent cached list payload if it exists."""
    path = _list_cache_path(list_id, data_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not load cached list %s: %s", path, exc)
        return None
