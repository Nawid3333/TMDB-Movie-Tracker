"""The monthly live-API check, run against a fake TMDB.

The check can only earn trust by being right in both directions: silent on a
healthy API, loud on each change the tracker would quietly store less from,
and never calling an outage a change. Nothing here touches the network.
"""

from __future__ import annotations

import copy
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

import httpx
import pytest

import config.config as _config
import src.tmdb_api
from tests import site_check
from tests.site_check import FAIL, PASS, SKIPPED, UNREACHABLE

API_KEY = "SECRETKEY123"
LIST_ID = "8678795"
API = "api.themoviedb.org"
IMAGES = "image.tmdb.org"

MOVIE = {
    "id": 120,
    "title": "Der Herr der Ringe: Die Gefährten",
    "original_title": "The Lord of the Rings: The Fellowship of the Ring",
    "original_language": "en",
    "release_date": "2001-12-18",
    "status": "Released",
    "runtime": 179,
    "poster_path": "/poster.jpg",
    "genres": [{"id": 12, "name": "Abenteuer"}],
    "production_countries": [{"iso_3166_1": "NZ"}, {"iso_3166_1": "US"}],
    "belongs_to_collection": {"id": 119, "name": "Der Herr der Ringe Filmreihe"},
    "credits": {
        "cast": [{"id": 109, "name": "Elijah Wood", "character": "Frodo", "order": 0}],
        "crew": [{"id": 108, "name": "Peter Jackson", "job": "Director", "department": "Directing"}],
    },
    "keywords": {"keywords": [{"id": 818, "name": "based on novel or book"}]},
    "external_ids": {"imdb_id": "tt0120737"},
    "release_dates": {
        "results": [{"iso_3166_1": "DE", "release_dates": [{"certification": "12", "release_date": "2001-12-19"}]}]
    },
}
COLLECTION = {"id": 119, "name": "Der Herr der Ringe Filmreihe", "parts": [{"id": 120}, {"id": 121}, {"id": 122}]}
LIST_PAGE = {
    "items": [{"id": 120, "media_type": "movie", "title": "Die Gefährten"}, {"id": 1399, "media_type": "tv"}],
    "item_count": 2,
    "total_pages": 1,
}


class FakeTmdb:
    """TMDB's API and image CDN, keyed by (host, path); the API wants the key."""

    def __init__(self, overrides: dict | None = None):
        self.routes: dict[tuple[str, str], object] = {
            (API, "/3/movie/120"): (200, MOVIE),
            (API, "/3/collection/119"): (200, COLLECTION),
            (API, f"/3/list/{LIST_ID}"): (200, LIST_PAGE),
            (IMAGES, "/t/p/w342/poster.jpg"): (200, b"\xff\xd8\xff" + b"0" * 4096),
        }
        self.routes.update(overrides or {})
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = (request.url.host, request.url.path)
        self.requests.append(key)
        route = self.routes.get(key, (404, {"status_code": 34}))
        if isinstance(route, Exception):
            raise route
        status, body = route  # type: ignore[misc]
        if request.url.host == API and request.url.params.get("api_key") != API_KEY:
            return httpx.Response(401, json={"status_code": 7})
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers={"content-type": "image/jpeg"})
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/html"})
        return httpx.Response(status, json=body)


def run(fake: FakeTmdb, *, key: str = API_KEY, list_id: str = LIST_ID) -> dict[str, site_check.Result]:
    transport = httpx.MockTransport(fake)
    real_client = httpx.Client

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with (
        mock.patch.object(httpx, "Client", RoutedClient),
        mock.patch.object(_config, "TMDB_API_KEY", key),
        mock.patch.object(_config, "TMDB_LIST_ID", list_id),
        mock.patch.object(src.tmdb_api.time, "sleep"),
    ):
        return {r.check: r for r in site_check.run_checks()}


def movie_with(**changes) -> tuple[int, dict]:
    body = copy.deepcopy(MOVIE)
    body.update(changes)
    return 200, body


def statuses(results: dict[str, site_check.Result]) -> dict[str, str]:
    return {name: r.status for name, r in results.items()}


def assert_only_failure(results: dict[str, site_check.Result], check: str) -> None:
    assert [name for name, r in results.items() if r.status == FAIL] == [check]
    assert site_check.exit_code(list(results.values())) == 1


class TestHealthyApi:
    def test_without_a_key_everything_is_skipped_and_nothing_is_asked(self) -> None:
        fake = FakeTmdb()
        results = run(fake, key="")
        assert statuses(results) == {"API": SKIPPED}
        assert fake.requests == []
        assert site_check.exit_code(list(results.values())) == 0

    def test_every_check_passes(self) -> None:
        results = run(FakeTmdb())
        assert statuses(results) == {"movie details": PASS, "collection": PASS, "poster": PASS, "list": PASS}

    def test_without_a_list_id_only_the_list_is_skipped(self) -> None:
        results = run(FakeTmdb(), list_id="")
        assert results["list"].status == SKIPPED
        assert site_check.exit_code(list(results.values())) == 0

    def test_the_report_never_carries_the_api_key(self) -> None:
        # Including on the error paths, where an httpx message would carry the URL.
        for fake in (FakeTmdb(), FakeTmdb({(API, f"/3/list/{LIST_ID}"): httpx.ConnectError("refused")})):
            report = site_check.render(list(run(fake).values()), date(2026, 10, 3))
            assert API_KEY not in report


class TestApiChanges:
    def test_a_refused_key(self) -> None:
        results = run(FakeTmdb(), key="stale")
        assert_only_failure(results, "movie details")
        assert "TMDB_API_KEY" in results["movie details"].detail

    def test_credits_without_a_director(self) -> None:
        credits = {"cast": MOVIE["credits"]["cast"], "crew": []}
        results = run(FakeTmdb({(API, "/3/movie/120"): movie_with(credits=credits)}))
        assert_only_failure(results, "movie details")
        assert "Director" in results["movie details"].detail

    def test_keywords_in_a_new_shape(self) -> None:
        results = run(FakeTmdb({(API, "/3/movie/120"): movie_with(keywords=[{"name": "novel"}])}))
        assert_only_failure(results, "movie details")
        assert "franchises" in results["movie details"].detail

    def test_release_dates_with_no_certification_to_pick(self) -> None:
        dates = {"results": [{"iso_3166_1": "DE", "dates": [{"cert": "12"}]}]}
        results = run(FakeTmdb({(API, "/3/movie/120"): movie_with(release_dates=dates)}))
        assert_only_failure(results, "movie details")

    def test_a_collection_that_no_longer_lists_its_parts(self) -> None:
        results = run(FakeTmdb({(API, "/3/collection/119"): (200, {"id": 119, "name": "x", "films": []})}))
        assert_only_failure(results, "collection")

    def test_a_poster_url_that_serves_a_page(self) -> None:
        results = run(FakeTmdb({(IMAGES, "/t/p/w342/poster.jpg"): (200, "<html>moved</html>")}))
        assert_only_failure(results, "poster")

    def test_a_list_that_went_private(self) -> None:
        results = run(FakeTmdb({(API, f"/3/list/{LIST_ID}"): (404, {"status_code": 34})}))
        assert_only_failure(results, "list")

    def test_a_list_read_that_comes_up_short(self) -> None:
        page = dict(LIST_PAGE, item_count=40)
        results = run(FakeTmdb({(API, f"/3/list/{LIST_ID}"): (200, page)}))
        assert_only_failure(results, "list")
        assert "incomplete" in results["list"].detail

    def test_list_items_without_a_media_type(self) -> None:
        page = dict(LIST_PAGE, items=[{"id": 120, "title": "x"}, {"id": 121, "title": "y"}])
        results = run(FakeTmdb({(API, f"/3/list/{LIST_ID}"): (200, page)}))
        assert_only_failure(results, "list")


class TestUnreachable:
    def test_an_outage_is_unreachable_not_a_change(self) -> None:
        results = run(FakeTmdb({(API, "/3/movie/120"): (503, {})}))
        assert results["movie details"].status == UNREACHABLE
        assert site_check.exit_code(list(results.values())) == 2

    def test_a_dropped_connection_on_the_list_is_unreachable(self) -> None:
        results = run(FakeTmdb({(API, f"/3/list/{LIST_ID}"): httpx.ConnectError("refused")}))
        assert results["list"].status == UNREACHABLE

    def test_a_cdn_outage_is_unreachable(self) -> None:
        results = run(FakeTmdb({(IMAGES, "/t/p/w342/poster.jpg"): (502, "<html>Bad Gateway</html>")}))
        assert results["poster"].status == UNREACHABLE


class TestMain:
    def test_the_report_file_and_exit_code(self) -> None:
        result = site_check.Result("list", FAIL, "a | b")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(site_check, "run_checks", return_value=[result]),
            mock.patch("builtins.print"),
        ):
            report = Path(tmp, "report.md")
            assert site_check.main_cli(["--report", str(report)]) == 1
            assert "a / b" in report.read_text(encoding="utf-8")

    @pytest.mark.parametrize("error", [KeyError("boom"), RuntimeError("boom")])
    def test_a_crash_is_reported_as_one_not_as_a_failed_check(self, error: Exception) -> None:
        with (
            mock.patch.object(site_check, "run_checks", side_effect=error),
            mock.patch("builtins.print"),
            mock.patch("traceback.print_exc"),
        ):
            assert site_check.main_cli([]) == 3
