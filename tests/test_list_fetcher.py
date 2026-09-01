"""Tests for src.list_fetcher."""

from pathlib import Path

import httpx
import pytest
import respx

from src.list_fetcher import ListFetchError, fetch_list, load_cached_list
from src.tmdb_api import TMDBClient


class TestFetchList:
    @respx.mock
    def test_fetch_first_page(self, fixtures: dict, tmp_project: Path, client: TMDBClient) -> None:
        list_payload = {**fixtures["list"], "item_count": len(fixtures["list"]["items"]), "total_pages": 1}
        list_id = list_payload["id"]
        respx.get(f"https://api.themoviedb.org/3/list/{list_id}").mock(
            return_value=httpx.Response(200, json=list_payload)
        )
        items, incomplete = fetch_list(client, list_id)
        assert items == list_payload["items"]
        assert incomplete is False

    @respx.mock
    def test_missing_list_id_raises(self, client: TMDBClient) -> None:
        with pytest.raises(ListFetchError, match="TMDB_LIST_ID"):
            fetch_list(client, "")

    @respx.mock
    def test_private_list_session_fallback(self, fixtures: dict, tmp_project: Path, client: TMDBClient) -> None:
        list_payload = {**fixtures["list"], "item_count": len(fixtures["list"]["items"]), "total_pages": 1}
        list_id = list_payload["id"]

        def _list_response(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("session_id"):
                return httpx.Response(200, json=list_payload)
            # Simulate a private list that rejects unauthenticated requests.
            return httpx.Response(401, json={"status_message": "Authentication failed"})

        list_route = respx.get(f"https://api.themoviedb.org/3/list/{list_id}").mock(side_effect=_list_response)
        session_route = respx.get("https://api.themoviedb.org/3/account").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )
        client.session_id = "fake_session"
        items, incomplete = fetch_list(client, list_id, use_session_on_private=True)
        assert len(items) == len(list_payload["items"])
        assert list_route.called
        assert session_route.called
        assert incomplete is False


class TestLoadCachedList:
    def test_returns_none_when_missing(self, tmp_project: Path) -> None:
        assert load_cached_list(8678795, tmp_project / "data") is None

    def test_loads_existing_cache(self, tmp_project: Path) -> None:
        cache = tmp_project / "data" / "list_8678795_fast.json"
        payload = {"list_id": "8678795", "items": [{"id": 1}]}
        cache.write_text('{"list_id": "8678795", "items": [{"id": 1}]}', encoding="utf-8")
        loaded = load_cached_list(8678795, tmp_project / "data")
        assert loaded == payload


class TestTruncatedFetchIsReportedIncomplete:
    """A short read must never reach detect_changes looking like a clean one.

    `incomplete` is the flag every removal proposal is gated on. The loop used
    to stop at an empty page and return incomplete=False, so a list truncated
    mid-way was indistinguishable from a list that had genuinely shrunk, and
    every item on the pages never read was offered for deletion.
    """

    def _client(self, blank_page: int, total: int = 100, per: int = 20):
        class FakeResp:
            def __init__(self, payload):
                self._p = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._p

        class FakeClient:
            def get(self, path, params=None, auth=False):
                page = params["page"]
                if page == blank_page:
                    items = []
                else:
                    start = (page - 1) * per + 1
                    items = [
                        {"media_type": "movie", "id": i, "title": f"Movie {i}", "release_date": "2020-01-01"}
                        for i in range(start, min(start + per, total + 1))
                    ]
                return FakeResp({"items": items, "item_count": total, "total_pages": (total + per - 1) // per})

            def ensure_session(self):
                return None

        return FakeClient()

    def test_a_blank_middle_page_marks_the_fetch_incomplete(self, tmp_path: Path) -> None:
        items, incomplete = fetch_list(self._client(blank_page=4), 123, cache_path=tmp_path / "c.json")
        assert incomplete is True
        assert len(items) < 100

    def test_a_complete_fetch_is_not_marked_incomplete(self, tmp_path: Path) -> None:
        items, incomplete = fetch_list(self._client(blank_page=0), 123, cache_path=tmp_path / "c.json")
        assert incomplete is False
        assert len(items) == 100

    def test_a_short_read_is_caught_even_without_a_blank_page(self, tmp_path: Path) -> None:
        """A server under-filling every page still owes item_count items."""

        class FakeResp:
            def __init__(self, payload):
                self._p = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._p

        class Underfilling:
            def get(self, path, params=None, auth=False):
                page = params["page"]
                start = (page - 1) * 10 + 1
                items = [
                    {"media_type": "movie", "id": i, "title": f"M{i}", "release_date": "2020-01-01"}
                    for i in range(start, min(start + 10, 101))
                ]
                return FakeResp({"items": items, "item_count": 100, "total_pages": 5})

            def ensure_session(self):
                return None

        items, incomplete = fetch_list(Underfilling(), 123, cache_path=tmp_path / "c.json")
        assert incomplete is True
        assert len(items) == 50
