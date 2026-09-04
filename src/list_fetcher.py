"""Fetch and cache a TMDB custom list with pagination."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import config.config as _config
from config.config import DATA_DIR
from src.atomic_io import atomic_write_json
from src.tmdb_api import _TMDBClientLike

logger = logging.getLogger(__name__)


class ListFetchError(Exception):
    """Raised when the list cannot be fetched or assembled."""


def _list_cache_path(list_id: str | int, data_dir: Path) -> Path:
    return data_dir / f"list_{list_id}_fast.json"


def _fetch_page(client: _TMDBClientLike, list_id: str | int, page: int, *, auth: bool = False) -> dict:
    """Fetch a single page of the list."""
    resp = client.get(f"/list/{list_id}", params={"page": page}, auth=auth)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ListFetchError(f"List {list_id} page {page} returned non-object body")
    return data


def fetch_list(
    client: _TMDBClientLike,
    list_id: str | int | None = None,
    *,
    cache_path: Path | None = None,
    use_session_on_private: bool = True,
) -> tuple[list[dict], bool]:
    """Fetch all pages of a TMDB list.

    Returns (items, incomplete). Incomplete is True if any page failed.
    The raw last-page payload is cached for inspection.
    """
    list_id = list_id if list_id is not None else _config.TMDB_LIST_ID
    if not list_id or (isinstance(list_id, str) and not list_id.strip()):
        raise ListFetchError("TMDB_LIST_ID is not configured")
    list_id = str(list_id)

    def _try_fetch(auth: bool) -> tuple[list[dict], bool]:
        items: list[dict] = []
        page = 1
        total_pages = 1
        incomplete = False
        # How many items the list itself says it holds. Used after the loop to
        # prove the fetch actually finished: `incomplete` is what gates every
        # removal downstream, so a short read must never reach detect_changes
        # looking like a clean one.
        expected_items: int | None = None

        while page <= total_pages:
            try:
                data = _fetch_page(client, list_id, page, auth=auth)
            except Exception as exc:
                logger.error("Failed to fetch list page %d: %s", page, exc)
                logger.warning("List fetch truncated at %d items after page %d", len(items), page)
                incomplete = True
                break

            page_items = data.get("items", []) or []
            if not page_items:
                # An empty page *before* the last expected one is a truncated
                # response, not the end of the list. Ending the loop quietly
                # here reported a partial fetch as complete, and every item on
                # the pages never read was then proposed for removal.
                if page < total_pages:
                    logger.error(
                        "List page %d of %d came back empty; treating the fetch as incomplete",
                        page,
                        total_pages,
                    )
                    incomplete = True
                break

            items.extend(page_items)
            if expected_items is None:
                expected_items = data.get("item_count")
                total_pages = data.get("total_pages") or total_pages
            page += 1

        # If the list reports a total item count, double-check that we really
        # got that many. A broken stream might never deliver a blank page but
        # still under-fill every page.
        if expected_items is not None and len(items) < expected_items:
            logger.warning(
                "List fetch short read: expected %d items, got %d",
                expected_items,
                len(items),
            )
            incomplete = True

        return items, incomplete

    items, incomplete = _try_fetch(auth=False)
    if not items and incomplete and use_session_on_private:
        logger.info("List fetch looks private; attempting session upgrade")
        session_id = client.ensure_session()
        if session_id:
            items, incomplete = _try_fetch(auth=True)

    if cache_path is None:
        cache_path = _list_cache_path(list_id, DATA_DIR)

    try:
        cache_payload = {
            "list_id": list_id,
            "cached_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
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
