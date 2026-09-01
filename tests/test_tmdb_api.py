"""Tests for src.tmdb_api."""

import time
from unittest.mock import patch

import httpx
import pytest
import respx

from src.tmdb_api import TMDBClient, TokenBucket, pick_certification


class TestTokenBucket:
    def test_acquire_does_not_sleep_when_tokens_available(self) -> None:
        bucket = TokenBucket(rate_per_second=10)
        with patch("time.sleep") as mock_sleep:
            bucket.acquire()
            mock_sleep.assert_not_called()

    def test_acquire_waits_when_bucket_empty(self) -> None:
        bucket = TokenBucket(rate_per_second=1)
        bucket.tokens = 0.0
        bucket.last_update = time.monotonic()
        with patch("time.sleep") as mock_sleep:
            bucket.acquire()
            mock_sleep.assert_called_once()


class TestTMDBClient:
    @respx.mock
    def test_get_injects_api_key(self, client: TMDBClient) -> None:
        route = respx.get("https://api.themoviedb.org/3/movie/550").mock(
            return_value=httpx.Response(200, json={"id": 550})
        )
        resp = client.get("/movie/550", auth=False)
        assert resp.status_code == 200
        assert route.calls.last.request.url.params["api_key"] == "test_key"

    @respx.mock
    def test_retry_on_500(self, client: TMDBClient) -> None:
        route = respx.get("https://api.themoviedb.org/3/movie/550").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200, json={"id": 550}),
            ]
        )
        with patch("time.sleep"):
            resp = client.get("/movie/550", auth=False)
        assert resp.status_code == 200
        assert len(route.calls) == 2

    @respx.mock
    def test_retry_exhausted_raises(self, client: TMDBClient) -> None:
        route = respx.get("https://api.themoviedb.org/3/movie/550").mock(return_value=httpx.Response(500))
        with patch("time.sleep"), pytest.raises(httpx.HTTPStatusError):
            client.get("/movie/550", auth=False, retries=2)
        assert len(route.calls) == 2

    @respx.mock
    def test_session_valid(self, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/account").mock(return_value=httpx.Response(200, json={"id": 1}))
        assert client._session_valid("fake_session") is True

    @respx.mock
    def test_session_invalid(self, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/account").mock(
            return_value=httpx.Response(401, json={"status_message": "Invalid session"})
        )
        assert client._session_valid("fake_session") is False

    @respx.mock
    def test_ensure_session_loads_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.get("https://api.themoviedb.org/3/account").mock(return_value=httpx.Response(200, json={"id": 1}))
        monkeypatch.setattr("config.config.TMDB_SESSION_ID", "env_session")
        client = TMDBClient(api_key="test_key")
        try:
            assert client.ensure_session() == "env_session"
        finally:
            client.close()


class TestPickCertification:
    def test_prefers_origin_country(self) -> None:
        results = [
            {
                "iso_3166_1": "US",
                "release_dates": [{"iso_3166_1": "US", "certification": "PG-13"}],
            },
            {
                "iso_3166_1": "DE",
                "release_dates": [{"iso_3166_1": "DE", "certification": "12"}],
            },
        ]
        cert = pick_certification(results, origin_country="DE", fallback="US")
        assert cert is not None
        assert cert == {"region": "DE", "rating": "12", "date": None}

    def test_falls_back_to_configured_region(self) -> None:
        results = [
            {
                "iso_3166_1": "US",
                "release_dates": [{"iso_3166_1": "US", "certification": "R"}],
            }
        ]
        cert = pick_certification(results, origin_country="FR", fallback="US")
        assert cert is not None
        assert cert["rating"] == "R"

    def test_returns_none_when_empty(self) -> None:
        assert pick_certification([], origin_country="US") is None
