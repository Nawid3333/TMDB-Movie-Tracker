"""Fetch and cache a TMDB custom list with pagination."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import config.config as _config
from config.config import DATA_DIR
from src.atomic_io import atomic_write_json
from src.tmdb_api import _TMDBClientLike, check_status

logger = logging.getLogger(__name__)

# TMDB serves list pages 20 items at a time, and a list that claims more pages
# than this is a broken response rather than a real list -- without the cap a
# bad total_pages is an unbounded fetch loop.
_PER_PAGE = 20
_MAX_PAGES = 500

# TMDB keeps counting a list entry it no longer serves -- typically a movie it
# deleted or merged after the entry was added. Such a list comes back short by
# that many items on every fetch, on v3 and v4 alike, signed in or not, so the
# short-read check below marked every fetch incomplete forever (and with it
# the startup table said FAILED and every removal stayed blocked). A shortfall
# this small that also survives a re-fetch of the short pages unchanged is
# taken as entries TMDB hides, not as a broken read. Anything larger is still
# a short read, however steady.
_HIDDEN_MIN_TOLERANCE = 5
_HIDDEN_TOLERANCE_RATIO = 0.01


def _hidden_tolerance(expected_items: int) -> int:
    """How many missing items may still count as entries TMDB hides."""
    return max(_HIDDEN_MIN_TOLERANCE, int(expected_items * _HIDDEN_TOLERANCE_RATIO))


class ListFetchError(Exception):
    """Raised when the list cannot be fetched or assembled."""


@dataclass(frozen=True)
class ListFetchResult:
    """A list fetch: its items, whether it is incomplete, and how many entries TMDB hid.

    *hidden* counts entries the list's item_count includes but TMDB did not
    return, on pages that came back the same when asked twice. The fetch is
    complete despite them: nothing more can be read.
    """

    items: list[dict]
    incomplete: bool
    hidden: int = 0
    expected: int | None = None


def _list_cache_path(list_id: str | int, data_dir: Path) -> Path:
    return data_dir / f"list_{list_id}_fast.json"


def _fetch_page(client: _TMDBClientLike, list_id: str | int, page: int, *, auth: bool = False) -> dict:
    """Fetch a single page of the list."""
    resp = client.get(f"/list/{list_id}", params={"page": page}, auth=auth)
    # Not resp.raise_for_status(): its message quotes the URL, api_key and
    # all, and the caller logs it -- on every fetch of a private list.
    check_status(resp)
    data = resp.json()
    if not isinstance(data, dict):
        raise ListFetchError(f"List {list_id} page {page} returned non-object body")
    return data


def _entry_keys(page_items: list) -> list[tuple]:
    """What identifies a page's entries, in order, for comparing two reads of it."""
    return [
        (item.get("media_type"), item.get("id")) if isinstance(item, dict) else (None, repr(item))
        for item in page_items
    ]


def _short_pages(page_sizes: dict[int, int], expected_items: int) -> list[int]:
    """Pages holding fewer items than an item_count of *expected_items* puts on them."""
    return [
        page
        for page, size in sorted(page_sizes.items())
        if size < min(_PER_PAGE, expected_items - (page - 1) * _PER_PAGE)
    ]


def fetch_list(
    client: _TMDBClientLike,
    list_id: str | int | None = None,
    *,
    cache_path: Path | None = None,
    use_session_on_private: bool = True,
) -> tuple[list[dict], bool]:
    """Fetch all pages of a TMDB list.

    Returns (items, incomplete). Incomplete is True if any page failed.
    See fetch_list_result for the full story, entries TMDB hides included.
    """
    result = fetch_list_result(
        client,
        list_id,
        cache_path=cache_path,
        use_session_on_private=use_session_on_private,
    )
    return result.items, result.incomplete


def fetch_list_result(
    client: _TMDBClientLike,
    list_id: str | int | None = None,
    *,
    cache_path: Path | None = None,
    use_session_on_private: bool = True,
) -> ListFetchResult:
    """Fetch all pages of a TMDB list.

    Incomplete is True if any page failed or the list came back short. A
    shortfall of a few entries that TMDB leaves out the same way on a second
    read is reported as *hidden* instead (see _HIDDEN_MIN_TOLERANCE). The
    fetched items are cached for inspection.
    """
    list_id = list_id if list_id is not None else _config.TMDB_LIST_ID
    if not list_id or (isinstance(list_id, str) and not list_id.strip()):
        raise ListFetchError("TMDB_LIST_ID is not configured")
    list_id = str(list_id)

    def _pages_read_alike(pages: list[int], first_read: dict[int, list[tuple]], auth: bool) -> bool:
        """Read *pages* again; True when each comes back exactly as the first time."""
        for page in pages:
            try:
                again = _fetch_page(client, list_id, page, auth=auth).get("items", []) or []
            except Exception as exc:
                logger.warning("Re-reading short list page %d failed: %s", page, exc)
                return False
            if _entry_keys(again) != first_read[page]:
                logger.warning("Short list page %d changed when read again", page)
                return False
        return True

    def _try_fetch(auth: bool) -> ListFetchResult:
        items: list[dict] = []
        page = 1
        total_pages = 1
        incomplete = False
        # How many items the list itself says it holds. Used after the loop to
        # prove the fetch actually finished: `incomplete` is what gates every
        # removal downstream, so a short read must never reach detect_changes
        # looking like a clean one.
        expected_items: int | None = None
        # Each page's entries as read, to re-check the short ones afterwards.
        page_keys: dict[int, list[tuple]] = {}

        while page <= total_pages:
            try:
                data = _fetch_page(client, list_id, page, auth=auth)
            except Exception as exc:
                logger.error("Failed to fetch list page %d: %s", page, exc)
                logger.warning("List fetch truncated at %d items after page %d", len(items), page)
                incomplete = True
                break

            page_items = data.get("items", []) or []
            page_keys[page] = _entry_keys(page_items)
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

            # Both counters come off the wire and neither is trusted to be an
            # int: a string total_pages would blow up the `page <= total_pages`
            # comparison above, and a string item_count the short-read check
            # below -- neither is caught by the handler around the fetch, so
            # either one takes the whole scan down with it.
            item_count = data.get("item_count")
            if isinstance(item_count, int) and item_count > 0:
                expected_items = item_count
                # total_pages for this endpoint has been seen to under-report.
                # item_count is the number the list itself stands behind, so
                # derive a page count from it and keep whichever is larger:
                # stopping on the server's word instead leaves a complete list
                # looking like a short read, and `incomplete` gates every
                # removal downstream, so it would never resolve on its own.
                total_pages = max(total_pages, (item_count + _PER_PAGE - 1) // _PER_PAGE)

            reported_total = data.get("total_pages")
            if isinstance(reported_total, int) and reported_total > 0:
                if page == 1 and reported_total != total_pages:
                    logger.debug(
                        "total_pages (%d) differs from the item_count-derived %d; using the larger",
                        reported_total,
                        total_pages,
                    )
                total_pages = max(total_pages, reported_total)

            page += 1
            if page > _MAX_PAGES:
                logger.warning(
                    "List pagination capped at %d pages / %d items",
                    _MAX_PAGES,
                    _MAX_PAGES * _PER_PAGE,
                )
                incomplete = True
                break

        # If the list reports a total item count, double-check that we really
        # got that many. A broken stream might never deliver a blank page but
        # still under-fill every page.
        hidden = 0
        if expected_items is not None and len(items) < expected_items:
            shortfall = expected_items - len(items)
            # Only a fetch that otherwise ran clean to its last page can be
            # short by hidden entries; any other short read is just short.
            if (
                not incomplete
                and shortfall <= _hidden_tolerance(expected_items)
                and _pages_read_alike(
                    _short_pages({p: len(keys) for p, keys in page_keys.items()}, expected_items),
                    page_keys,
                    auth,
                )
            ):
                hidden = shortfall
                # Debug: the log file keeps it; the console already gets a
                # plain-words note under the startup table.
                logger.debug(
                    "List counts %d items but TMDB returns %d, the same way twice; "
                    "taking %d as entries TMDB hides (deleted or merged movies)",
                    expected_items,
                    len(items),
                    hidden,
                )
            else:
                logger.warning(
                    "List fetch short read: expected %d items, got %d",
                    expected_items,
                    len(items),
                )
                incomplete = True

        return ListFetchResult(items, incomplete, hidden, expected_items)

    result = _try_fetch(auth=False)
    if not result.items and result.incomplete and use_session_on_private:
        logger.info("List fetch looks private; attempting session upgrade")
        session_id = client.ensure_session()
        if session_id:
            result = _try_fetch(auth=True)
        else:
            logger.warning("No session available; private list reads will fail")

    if cache_path is None:
        cache_path = _list_cache_path(list_id, DATA_DIR)

    try:
        cache_payload = {
            "list_id": list_id,
            "cached_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "item_count": len(result.items),
            "expected_count": result.expected,
            "hidden": result.hidden,
            "incomplete": result.incomplete,
            "items": result.items,
        }
        atomic_write_json(cache_path, cache_payload)
    except Exception as exc:
        logger.warning("Could not cache list payload: %s", exc)

    return result


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
