"""TMDB API client: retrying request helper, token-bucket rate limiter, and auth ladder."""

import contextlib
import logging
import random
import threading
import time
import webbrowser
from typing import Any

import httpx

import config.config as _config
from config.config import (
    TMDB_API_BASE_URL,
    TMDB_DETAIL_WORKERS,
    TMDB_FALLBACK_REGION,
    TMDB_HTTP_TIMEOUT,
    TMDB_LIST_PAGE_WORKERS,
    TMDB_MAX_CONNECTIONS,
    TMDB_MAX_REQUESTS_PER_SECOND,
    TMDB_READ_MAX_RETRIES,
    TMDB_READ_RETRY_DELAY,
)

logger = logging.getLogger(__name__)


class TokenBucket:
    """Thread-safe token bucket for request pacing.

    The actual sleep is performed outside the lock so that parallel workers
    are not serialized while waiting for tokens.
    """

    def __init__(self, rate_per_second: float):
        self.rate = rate_per_second
        self.tokens = rate_per_second
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        sleep_time = 0.0
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_update = now
            if self.tokens < 1.0:
                deficit = 1.0 - self.tokens
                sleep_time = deficit / self.rate
                self.tokens = 0.0
            else:
                self.tokens -= 1.0

        if sleep_time > 0.0:
            time.sleep(sleep_time)
            with self._lock:
                self.last_update = time.monotonic()


class TMDBClient:
    """Rate-limited, retrying TMDB v3 client with session management."""

    def __init__(self, session_file: Any | None = None, api_key: str | None = None) -> None:
        self.api_key = (api_key or _config.TMDB_API_KEY).strip()
        self.session_file = None  # deprecated: sessions now come from .env only
        self.session_id: str | None = None
        self.bucket = TokenBucket(TMDB_MAX_REQUESTS_PER_SECOND)
        limits = httpx.Limits(
            max_connections=TMDB_MAX_CONNECTIONS,
            max_keepalive_connections=TMDB_DETAIL_WORKERS + TMDB_LIST_PAGE_WORKERS,
        )
        self.client = httpx.Client(
            base_url=TMDB_API_BASE_URL,
            timeout=TMDB_HTTP_TIMEOUT,
            limits=limits,
        )
        self._session_lock = threading.Lock()
        self._closed = False

    def _retry_delay(self, response: httpx.Response | None) -> float:
        base = TMDB_READ_RETRY_DELAY
        if response is not None:
            raw = response.headers.get("Retry-After", "")
            with contextlib.suppress(ValueError):
                base = max(float(raw), 0.0)
        return base + random.uniform(0.0, 1.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        retries: int = TMDB_READ_MAX_RETRIES,
    ) -> httpx.Response:
        """Make a rate-limited, retrying request."""
        params = dict(params) if params else {}
        params["api_key"] = self.api_key
        if auth and self.session_id:
            params["session_id"] = self.session_id

        self.bucket.acquire()
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                )
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"Server error {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                return resp
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < retries:
                    delay = self._retry_delay(getattr(exc, "response", None))
                    logger.warning(
                        "Request failed (%s %s) attempt %d/%d: %s – retrying in %.0fs...",
                        method,
                        path,
                        attempt,
                        retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error("Request failed after %d attempts: %s", retries, last_exc)
                    raise
        raise RuntimeError("Request loop exited without returning or raising")

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        retries: int = TMDB_READ_MAX_RETRIES,
    ) -> httpx.Response:
        return self.request("GET", path, params=params, auth=auth, retries=retries)

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        retries: int = TMDB_READ_MAX_RETRIES,
    ) -> httpx.Response:
        return self.request("POST", path, params=params, json_body=json_body, auth=auth, retries=retries)

    def _session_valid(self, session_id: str) -> bool:
        try:
            resp = self.request("GET", "/account", params={"session_id": session_id}, auth=False)
            return resp.status_code == 200 and isinstance(resp.json(), dict)
        except Exception as exc:
            logger.debug("Session validation failed: %s", exc)
            return False

    def _create_request_token(self) -> str | None:
        try:
            resp = self.request("GET", "/authentication/token/new", auth=False)
            data = resp.json()
            return data.get("request_token") if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Could not create request token: %s", exc)
            return None

    def _validate_token_with_login(self, token: str) -> str | None:
        if not _config.TMDB_USERNAME or not _config.TMDB_PASSWORD:
            return None
        try:
            resp = self.request(
                "POST",
                "/authentication/token/validate_with_login",
                json_body={
                    "username": _config.TMDB_USERNAME,
                    "password": _config.TMDB_PASSWORD,
                    "request_token": token,
                },
                auth=False,
            )
            data = resp.json()
            return data.get("request_token") if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Password login failed: %s", exc)
            return None

    def _create_session_from_token(self, token: str) -> str | None:
        try:
            resp = self.request(
                "POST",
                "/authentication/session/new",
                json_body={"request_token": token},
                auth=False,
            )
            data = resp.json()
            return data.get("session_id") if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("Could not convert request token to session: %s", exc)
            return None

    def _convert_v4_token(self) -> str | None:
        if not _config.TMDB_V4_ACCESS_TOKEN:
            return None
        try:
            resp = self.request(
                "POST",
                "/authentication/session/convert/4",
                json_body={"access_token": _config.TMDB_V4_ACCESS_TOKEN},
                auth=False,
            )
            data = resp.json()
            return data.get("session_id") if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("v4 token conversion failed: %s", exc)
            return None

    def _browser_approval_flow(self) -> str | None:
        token = self._create_request_token()
        if not token:
            return None
        url = f"https://www.themoviedb.org/authenticate/{token}"
        logger.info("Opening browser for TMDB approval: %s", url)
        try:
            webbrowser.open(url)
        except Exception as exc:
            logger.warning("Could not open browser: %s", exc)
        input_text = input("Press Enter after approving TMDB in your browser...")
        _ = input_text
        session_id = self._create_session_from_token(token)
        if session_id:
            print()
            print(f"New TMDB session created: {session_id}")
            print("Add it to your .env file so it is reused on the next run:")
            print(f"  TMDB_SESSION_ID={session_id}")
        return session_id

    def _resolve_session_no_lock(self) -> str | None:
        """Run the full auth ladder without holding the session lock."""
        # 1. env session
        env_session = _config.TMDB_SESSION_ID.strip() if _config.TMDB_SESSION_ID else None
        if env_session and self._session_valid(env_session):
            return env_session

        # 2. v4 access token
        v4_session = self._convert_v4_token()
        if v4_session and self._session_valid(v4_session):
            return v4_session

        # 3. username + password
        token = self._create_request_token()
        validated = token and self._validate_token_with_login(token)
        if validated:
            session = self._create_session_from_token(validated)
            if session and self._session_valid(session):
                return session

        # 4. browser approval
        browser_session = self._browser_approval_flow()
        if browser_session and self._session_valid(browser_session):
            return browser_session

        logger.warning("No TMDB session available; reads that require a session will fail, writes are disabled.")
        return None

    def ensure_session(self) -> str | None:
        """Resolve a session id using the auth ladder. Returns session_id or None.

        Thread-safe: concurrent calls serialize on an internal lock for the
        short-circuit checks, but the actual network/user-interactive auth ladder
        runs outside the lock so one stalled approval flow does not block other
        workers that already have a valid session.
        """
        if not self.api_key:
            logger.error("TMDB_API_KEY is required")
            return None

        # Fast path: cached in memory.
        with self._session_lock:
            if self.session_id and self._session_valid(self.session_id):
                return self.session_id

        # Interactive/network ladder: avoid holding the lock during blocking calls.
        resolved = self._resolve_session_no_lock()
        if resolved:
            with self._session_lock:
                # Only adopt the resolved session if we still do not have one.
                if not self.session_id:
                    self.session_id = resolved
        return self.session_id

    def invalidate_session_cache(self) -> None:
        with self._session_lock:
            self.session_id = None

    def close(self) -> None:
        if not self._closed:
            self.client.close()
            self._closed = True

    def __enter__(self) -> "TMDBClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def pick_certification(
    release_dates: list[dict],
    origin_country: str | None,
    fallback: str = TMDB_FALLBACK_REGION,
) -> dict | None:
    """Select the best certification from release_dates for a film.

    Priority: country of origin, then configured fallback region.
    """
    if not release_dates or not isinstance(release_dates, list):
        return None
    by_country: dict[str, list[dict]] = {}
    for entry in release_dates:
        if not isinstance(entry, dict):
            continue
        country = entry.get("iso_3166_1")
        dates = entry.get("release_dates")
        if country and isinstance(dates, list):
            by_country[country] = dates

    def _first_cert(dates: list[dict]) -> dict | None:
        for d in dates:
            if not isinstance(d, dict):
                continue
            cert = d.get("certification")
            if cert:
                return {"region": d.get("iso_3166_1", ""), "rating": cert, "date": d.get("release_date")}
        return None

    if origin_country and origin_country in by_country:
        found = _first_cert(by_country[origin_country])
        if found:
            return found
    if fallback in by_country:
        found = _first_cert(by_country[fallback])
        if found:
            return found
    return None


# `build_language_params` was removed because it was unused. Re-add a
# consistent language-param builder here if multiple endpoints need it.
