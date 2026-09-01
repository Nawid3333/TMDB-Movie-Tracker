"""Integration tests that exercise multiple modules with mocked TMDB."""

from pathlib import Path

import httpx
import respx

from src.changes import apply_changes, detect_changes
from src.index import load_details, load_index, save_index
from src.list_fetcher import fetch_list
from src.search import add_movie_locally, fetch_full_movie
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


class TestAddAndEnrichWorkflow:
    @respx.mock
    def test_add_movie_and_save(self, fixtures: dict, tmp_project: Path, fake_image_client, client: TMDBClient) -> None:
        movie = fixtures["movie_9013"]
        movie_id = movie["id"]
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(return_value=httpx.Response(200, json=movie))
        coll = movie.get("belongs_to_collection")
        if coll:
            respx.get(f"https://api.themoviedb.org/3/collection/{coll['id']}").mock(
                return_value=httpx.Response(200, json=fixtures["collection_987044"])
            )

        pair = fetch_full_movie(client, movie_id, fake_image_client)
        membership, detail = pair
        add_movie_locally(membership, detail)

        reloaded_index = load_index()
        reloaded_details = load_details()
        assert str(movie_id) in reloaded_index["movies"]
        assert str(movie_id) in reloaded_details["movies"]
        assert reloaded_index["movies"][str(movie_id)]["title"] == movie["title"]
