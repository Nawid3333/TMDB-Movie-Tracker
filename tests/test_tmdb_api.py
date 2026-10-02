"""Tests for src.tmdb_api."""

import threading
import time
from unittest.mock import patch

import httpx
import pytest
import respx

from src.tmdb_api import TMDBClient, TokenBucket, check_status, pick_certification


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

    def test_callers_arriving_together_queue_one_slot_apart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The old bucket gave every caller that found it empty the same short
        wait, so they all went at once; each one now waits behind the last."""
        monkeypatch.setattr("src.tmdb_api.time.monotonic", lambda: 1000.0)
        bucket = TokenBucket(rate_per_second=10)
        waits: list[float] = []
        monkeypatch.setattr("src.tmdb_api.time.sleep", waits.append)

        for _ in range(5):
            bucket.acquire()

        assert waits == pytest.approx([0.1, 0.2, 0.3, 0.4])

    def test_the_rate_holds_across_concurrent_workers(self) -> None:
        """Measured on the old bucket: 16 workers at a configured 30/s ran 486/s."""
        rate = 40.0
        bucket = TokenBucket(rate_per_second=rate)
        count = 0
        lock = threading.Lock()
        deadline = time.monotonic() + 0.5

        def worker() -> None:
            nonlocal count
            while time.monotonic() < deadline:
                bucket.acquire()
                with lock:
                    count += 1

        started = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - started

        # One token of burst, plus each worker's last reservation past the deadline.
        assert count <= rate * elapsed + 1 + len(threads)


class TestCheckStatus:
    """raise_for_status() quoted the whole URL -- api_key and session_id with it."""

    def _response(self, status: int) -> httpx.Response:
        request = httpx.Request(
            "GET", "https://api.themoviedb.org/3/movie/11?api_key=SECRETKEY&session_id=SECRETSESSION"
        )
        return httpx.Response(status, request=request)

    def test_a_failure_names_the_status_method_and_path_only(self) -> None:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            check_status(self._response(401))
        assert str(caught.value) == "HTTP 401 for GET /3/movie/11"
        assert caught.value.response.status_code == 401

    def test_success_passes(self) -> None:
        check_status(self._response(200))


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
    def test_every_attempt_waits_for_the_rate_limiter(self, client: TMDBClient, monkeypatch) -> None:
        """A retry -- typically after a 429 -- used to skip the pacer entirely."""
        respx.get("https://api.themoviedb.org/3/movie/550").mock(
            side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"id": 550})]
        )
        acquired: list[int] = []
        monkeypatch.setattr(client.bucket, "acquire", lambda: acquired.append(1))
        with patch("time.sleep"):
            client.get("/movie/550", auth=False)
        assert len(acquired) == 3

    @respx.mock
    def test_a_reset_connection_is_retried(self, client: TMDBClient) -> None:
        """Only ConnectError and timeouts were retried; a reset mid-response failed outright."""
        route = respx.get("https://api.themoviedb.org/3/movie/550").mock(
            side_effect=[
                httpx.ReadError("connection reset"),
                httpx.RemoteProtocolError("server disconnected"),
                httpx.Response(200, json={"id": 550}),
            ]
        )
        with patch("time.sleep"):
            resp = client.get("/movie/550", auth=False)
        assert resp.status_code == 200
        assert len(route.calls) == 3

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


class TestV4AuthBootstrap:
    """The v4 auth flow that trades a read-only "API Read Access Token" for a
    real, write-capable v4 access token -- needed because v3's remove_item
    can't touch non-movie list items at all (confirmed live: "Entry not
    found" even when the item is genuinely on the list)."""

    @respx.mock
    def test_create_v4_request_token_uses_the_read_access_token_as_bearer(
        self, client: TMDBClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("config.config.TMDB_API_READ_ACCESS_TOKEN", "fake_read_token")
        route = respx.post("https://api.themoviedb.org/4/auth/request_token").mock(
            return_value=httpx.Response(200, json={"success": True, "request_token": "req123"})
        )
        assert client._create_v4_request_token() == "req123"
        assert route.calls.last.request.headers["Authorization"] == "Bearer fake_read_token"

    def test_create_v4_request_token_without_a_read_access_token_makes_no_call(
        self, client: TMDBClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("config.config.TMDB_API_READ_ACCESS_TOKEN", "")
        with respx.mock:
            assert client._create_v4_request_token() is None

    @respx.mock
    def test_exchange_v4_access_token_returns_the_new_token(
        self, client: TMDBClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("config.config.TMDB_API_READ_ACCESS_TOKEN", "fake_read_token")
        route = respx.post("https://api.themoviedb.org/4/auth/access_token").mock(
            return_value=httpx.Response(200, json={"success": True, "access_token": "final_v4_token"})
        )
        assert client._exchange_v4_access_token("req123") == "final_v4_token"
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer fake_read_token"
        assert b'"request_token":"req123"' in request.content.replace(b" ", b"")

    def test_acquire_v4_access_token_without_a_read_access_token_returns_none_and_makes_no_call(
        self, client: TMDBClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("config.config.TMDB_API_READ_ACCESS_TOKEN", "")
        with respx.mock:
            assert client.acquire_v4_access_token() is None


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
