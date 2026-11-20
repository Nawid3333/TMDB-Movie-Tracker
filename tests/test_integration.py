"""Integration tests that exercise multiple modules with mocked TMDB."""

from pathlib import Path

import httpx
import respx

from src.changes import apply_changes, detect_changes
from src.index import load_index, save_index
from src.list_fetcher import fetch_list
from src.tmdb_api import TMDBClient


class TestFastScanWorkflow:
    @respx.mock
    def test_full_fast_scan_and_apply(self, fixtures: dict, tmp_project: Path, client: TMDBClient) -> None:
        list_payload = {**fixtures["list"], "item_count": len(fixtures["list"]["items"]), "total_pages": 1}
        list_id = list_payload["id"]
        respx.get(f"https://api.themoviedb.org/3/list/{list_id}").mock(
            return_value=httpx.Response(200, json=list_payload)
        )

        items, incomplete = fetch_list(client, list_id)
        assert incomplete is False
        assert items == list_payload["items"]

        index = load_index()
        change_set = detect_changes(index["movies"], items)
        index["movies"] = apply_changes(index["movies"], change_set, approve_additions=True)
        save_index(index)

        reloaded = load_index()
        assert len(reloaded["movies"]) == len(list_payload["items"])
