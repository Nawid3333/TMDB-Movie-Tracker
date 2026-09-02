"""Tests for src.search with real fixtures as mocked responses."""

import httpx
import pytest
import respx

from src.search import (
    _parse_user_input,
    fetch_full_movie,
    push_to_tmdb_list,
    search_movies,
)
from src.tmdb_api import TMDBClient


class TestParseUserInput:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("550", {"type": "tmdb_id", "value": 550}),
            ("tt1375666", {"type": "imdb_id", "value": "tt1375666"}),
            (
                "https://www.themoviedb.org/movie/550",
                {"type": "tmdb_id", "value": 550},
            ),
            (
                "https://www.imdb.com/title/tt1375666/",
                {"type": "imdb_id", "value": "tt1375666"},
            ),
            ("Inception", {"type": "title", "value": "Inception"}),
        ],
    )
    def test_parses(self, query, expected) -> None:
        assert _parse_user_input(query) == expected

    def test_empty_raises_parse_error(self) -> None:
        from src.search import ParseError

        with pytest.raises(ParseError):
            _parse_user_input("  ")


class TestSearchMovies:
    @respx.mock
    def test_search_by_title(self, fixtures: dict, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(200, json=fixtures["search_inception"])
        )
        results = search_movies(client, "Inception")
        assert len(results) == fixtures["search_inception"]["total_results"]
        assert all(r["id"] for r in results)

    @respx.mock
    def test_search_by_imdb_id(self, fixtures: dict, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/find/tt1375666").mock(
            return_value=httpx.Response(200, json=fixtures["find_imdb"])
        )
        results = search_movies(client, "tt1375666")
        assert len(results) == len(fixtures["find_imdb"].get("movie_results", []))

    @respx.mock
    def test_search_by_tmdb_id(self, fixtures: dict, client: TMDBClient) -> None:
        movie = fixtures["movie_9013"]
        respx.get(f"https://api.themoviedb.org/3/movie/{movie['id']}").mock(
            return_value=httpx.Response(200, json=movie)
        )
        results = search_movies(client, str(movie["id"]))
        assert len(results) == 1
        assert results[0]["id"] == movie["id"]


class TestFetchFullMovie:
    @respx.mock
    def test_enrich_one_movie(self, fixtures: dict, tmp_project, fake_image_client, client: TMDBClient) -> None:
        movie = fixtures["movie_475557"]
        movie_id = movie["id"]
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(return_value=httpx.Response(200, json=movie))
        # Collection route if present.
        coll = movie.get("belongs_to_collection")
        if coll:
            respx.get(f"https://api.themoviedb.org/3/collection/{coll['id']}").mock(
                return_value=httpx.Response(200, json=fixtures["collection_633215"])
            )
        pair = fetch_full_movie(client, movie_id, fake_image_client)
        membership, detail = pair
        assert membership["id"] == movie_id
        assert detail["runtime"] == movie["runtime"]
        assert "enriched_at" in detail


class TestPushToTmdbList:
    @respx.mock
    def test_no_session_skips(self, client: TMDBClient) -> None:
        result = push_to_tmdb_list(client, 8678795, 550)
        assert result["success"] is False
        assert result["remote_push"] == "skipped"

    @respx.mock
    def test_successful_push(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        route = respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            return_value=httpx.Response(200, json={"status_code": 12})
        )
        result = push_to_tmdb_list(client, 8678795, 550)
        assert result["success"] is True
        assert result["remote_push"] == "ok"
        assert route.called

    @respx.mock
    def test_duplicate_push_reported_successfully(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        route = respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": False,
                    "status_code": 8,
                    "status_message": "Duplicate entry: The item you are trying to add is already on your list.",
                },
            )
        )
        result = push_to_tmdb_list(client, 8678795, 550)
        assert result["success"] is True
        assert result["remote_push"] == "duplicate"
        assert "already" in result["reason"].lower()
        assert route.called

    @respx.mock
    def test_other_tmdb_error_push(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        route = respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": False,
                    "status_code": 5,
                    "status_message": "Invalid format for the given parameters.",
                },
            )
        )
        result = push_to_tmdb_list(client, 8678795, 550)
        assert result["success"] is False
        assert result["remote_push"] == "failed"
        assert "TMDB error 5" in result["reason"]
        assert route.called
