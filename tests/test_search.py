"""Tests for src.search with real fixtures as mocked responses."""

import httpx
import pytest
import respx

import config.config as _config
from src.search import (
    SearchError,
    _parse_user_input,
    fetch_full_movie,
    push_to_tmdb_list,
    remove_from_tmdb_list,
    resolve_movie_ids_from_file,
    resolve_movie_input,
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


class TestRemoveFromTmdbList:
    @respx.mock
    def test_no_session_skips(self, client: TMDBClient) -> None:
        result = remove_from_tmdb_list(client, 8678795, 277439)
        assert result["success"] is False
        assert result["remote_push"] == "skipped"

    @respx.mock
    def test_successful_removal(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        route = respx.post("https://api.themoviedb.org/3/list/8678795/remove_item").mock(
            return_value=httpx.Response(
                200, json={"status_code": 13, "status_message": "The item/record was deleted successfully."}
            )
        )
        result = remove_from_tmdb_list(client, 8678795, 277439)
        assert result["success"] is True
        assert result["remote_push"] == "removed"
        assert route.called

    @respx.mock
    def test_other_tmdb_error_reported_as_failure(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        route = respx.post("https://api.themoviedb.org/3/list/8678795/remove_item").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": False,
                    "status_code": 5,
                    "status_message": "Invalid format for the given parameters.",
                },
            )
        )
        result = remove_from_tmdb_list(client, 8678795, 277439)
        assert result["success"] is False
        assert result["remote_push"] == "failed"
        assert "TMDB error 5" in result["reason"]
        assert route.called


class TestRemoveFromTmdbListV4:
    """v3's remove_item only recognizes movies (confirmed live: a non-movie id
    comes back "Entry not found" even though it's genuinely on the list), so
    a non-"movie" media_type is routed to v4 instead."""

    @respx.mock
    def test_non_movie_removal_uses_v4_with_bearer_auth(self, client: TMDBClient, monkeypatch) -> None:
        monkeypatch.setattr(_config, "TMDB_V4_ACCESS_TOKEN", "fake_v4_token")
        client.session_id = "fake_session"  # not used by the v4 path, but shouldn't matter either way
        route = respx.delete("https://api.themoviedb.org/4/list/8678795/items").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "results": [{"media_id": 277439, "success": True}]},
            )
        )
        result = remove_from_tmdb_list(client, 8678795, 277439, media_type="tv")
        assert result["success"] is True
        assert result["remote_push"] == "removed"
        assert route.called
        assert route.calls.last.request.headers["Authorization"] == "Bearer fake_v4_token"

    @respx.mock
    def test_a_per_item_v4_failure_is_reported_with_tmdbs_own_message(self, client: TMDBClient, monkeypatch) -> None:
        monkeypatch.setattr(_config, "TMDB_V4_ACCESS_TOKEN", "fake_v4_token")
        respx.delete("https://api.themoviedb.org/4/list/8678795/items").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "results": [{"media_id": 277439, "success": False, "status_message": "Invalid item"}],
                },
            )
        )
        result = remove_from_tmdb_list(client, 8678795, 277439, media_type="tv")
        assert result["success"] is False
        assert "Invalid item" in result["reason"]

    def test_without_a_v4_token_it_is_skipped_with_no_network_call(self, client: TMDBClient, monkeypatch) -> None:
        monkeypatch.setattr(_config, "TMDB_V4_ACCESS_TOKEN", "")
        with respx.mock:
            result = remove_from_tmdb_list(client, 8678795, 277439, media_type="tv")
        assert result == {"success": False, "reason": "no v4 access token configured", "remote_push": "skipped"}

    @respx.mock
    def test_a_movie_still_goes_through_v3(self, client: TMDBClient, monkeypatch) -> None:
        """The default media_type keeps existing movie-removal behaviour unchanged."""
        monkeypatch.setattr(_config, "TMDB_V4_ACCESS_TOKEN", "fake_v4_token")
        client.session_id = "fake_session"
        v3_route = respx.post("https://api.themoviedb.org/3/list/8678795/remove_item").mock(
            return_value=httpx.Response(200, json={"status_code": 13})
        )
        v4_route = respx.delete("https://api.themoviedb.org/4/list/8678795/items").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        result = remove_from_tmdb_list(client, 8678795, 550, media_type="movie")
        assert result["success"] is True
        assert v3_route.called
        assert not v4_route.called


class TestNonJsonErrorBodies:
    """An error page in front of TMDB (an HTML 403, a plain-text 401).

    resp.json() raised JSONDecodeError -- a ValueError, which the
    `except httpx.HTTPError` around these calls does not catch -- so one such
    reply ended a whole batch push half way through.
    """

    _HTML_403 = httpx.Response(403, text="<html>403 Forbidden</html>", headers={"content-type": "text/html"})

    @respx.mock
    def test_a_push_reports_a_failure(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(return_value=self._HTML_403)
        result = push_to_tmdb_list(client, 8678795, 550)
        assert result["success"] is False
        assert result["remote_push"] == "failed"
        assert "HTTP 403" in result["reason"]

    @respx.mock
    def test_a_v3_removal_reports_a_failure(self, client: TMDBClient) -> None:
        client.session_id = "fake_session"
        respx.post("https://api.themoviedb.org/3/list/8678795/remove_item").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        result = remove_from_tmdb_list(client, 8678795, 550)
        assert result["success"] is False
        assert "HTTP 401" in result["reason"]

    @respx.mock
    def test_a_v4_removal_reports_a_failure(self, client: TMDBClient, monkeypatch) -> None:
        monkeypatch.setattr(_config, "TMDB_V4_ACCESS_TOKEN", "fake_v4_token")
        respx.delete("https://api.themoviedb.org/4/list/8678795/items").mock(return_value=self._HTML_403)
        result = remove_from_tmdb_list(client, 8678795, 277439, media_type="tv")
        assert result["success"] is False

    @respx.mock
    def test_a_failure_message_never_carries_the_credentials(self, client: TMDBClient) -> None:
        client.session_id = "SECRETSESSION"
        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(return_value=self._HTML_403)
        result = push_to_tmdb_list(client, 8678795, 550)
        assert "test_key" not in result["reason"]
        assert "SECRETSESSION" not in result["reason"]


class TestBatchFileReading:
    """What a batch file's lines come back as, and why a line was skipped."""

    def _movie(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": int(request.url.path.rsplit("/", 1)[1]), "title": "X"})

    @respx.mock
    def test_a_byte_order_mark_does_not_cost_the_first_line(self, tmp_path, client: TMDBClient) -> None:
        """Notepad's "UTF-8 with BOM" left the mark on line 1: "\\ufeff603" was skipped."""
        path = tmp_path / "urls.txt"
        path.write_text("603\n550\n", encoding="utf-8-sig")
        respx.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(side_effect=self._movie)
        records, skipped = resolve_movie_ids_from_file(client, str(path))
        assert [r["id"] for r in records] == [603, 550]
        assert skipped == []

    def test_a_utf16_file_is_a_search_error_not_a_crash(self, tmp_path, client: TMDBClient) -> None:
        """What PowerShell 5's `>` writes. UnicodeDecodeError went past `except OSError`."""
        path = tmp_path / "urls.txt"
        path.write_text("603\n", encoding="utf-16")
        with pytest.raises(SearchError, match="not UTF-8"):
            resolve_movie_ids_from_file(client, str(path))

    @respx.mock
    def test_a_rejected_key_is_reported_as_such_not_as_a_bad_line(self, tmp_path, client: TMDBClient) -> None:
        path = tmp_path / "urls.txt"
        path.write_text("603\nnot a movie\n", encoding="utf-8")
        respx.get("https://api.themoviedb.org/3/movie/603").mock(return_value=httpx.Response(401, json={}))
        records, skipped = resolve_movie_ids_from_file(client, str(path))
        assert records == []
        assert skipped == [
            (1, "603", "TMDB lookup failed (/movie/603 failed: HTTP 401 for GET /3/movie/603)"),
            (2, "not a movie", "not a TMDB/IMDb movie URL or id"),
        ]

    @respx.mock
    def test_a_movie_tmdb_does_not_have_says_so(self, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/movie/999999999").mock(return_value=httpx.Response(404))
        assert resolve_movie_input(client, "999999999") == (None, "not found on TMDB")
