"""Tests for src.enrich."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

import src.enrich as enrich_mod
from src.enrich import (
    _days_since_release,
    _enrich_one,
    _fetch_collection,
    _fetch_keyword_tv,
    _LockedCache,
    _LockedListCache,
    _release_year,
    _should_enrich,
    _volatility_tier,
)
from src.index import ensure_record_exists, load_index, save_index
from src.tmdb_api import TMDBClient


class TestReleaseHelpers:
    def test_release_year(self) -> None:
        assert _release_year("2010-07-16") == 2010
        assert _release_year("") is None

    def test_days_since_release(self) -> None:
        assert isinstance(_days_since_release("2010-07-16"), int)
        assert _days_since_release("") is None


class TestVolatilityTier:
    def test_unreleased_is_hot(self) -> None:
        future = (datetime.now(UTC).year + 1).__str__() + "-01-01"
        assert _volatility_tier({"status": "Post Production", "release_date": future}) == "hot"

    def test_recent_is_warm(self) -> None:
        recent = datetime.now(UTC).date().replace(day=1).isoformat()
        assert _volatility_tier({"status": "Released", "release_date": recent}) == "warm"

    def test_old_is_cool_or_cold(self) -> None:
        tier = _volatility_tier({"status": "Released", "release_date": "2010-01-01"})
        assert tier in ("cool", "cold")


class TestShouldEnrich:
    def test_force_true(self) -> None:
        assert _should_enrich({}, {}, force=True) is True

    def test_no_enriched_at(self) -> None:
        assert _should_enrich({"status": "Released", "release_date": "2020-01-01"}, {}) is True

    def test_cold_record_recently_enriched(self) -> None:
        details = {"enriched_at": "2020-01-01T00:00:00Z"}
        assert _should_enrich({"status": "Released", "release_date": "2010-01-01"}, details) is True


class TestFetchCollection:
    @respx.mock
    def test_fetch_collection(self, fixtures: dict, client: TMDBClient) -> None:
        coll = fixtures["collection_987044"]
        coll_id = coll["id"]
        respx.get(f"https://api.themoviedb.org/3/collection/{coll_id}").mock(
            return_value=httpx.Response(200, json=coll)
        )
        result = _fetch_collection(client, coll_id)
        assert result is not None
        assert result["id"] == coll_id
        assert all("id" in p and "title" in p for p in result["parts"])

    @respx.mock
    def test_fetch_collection_failure(self, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/collection/0").mock(return_value=httpx.Response(404))
        assert _fetch_collection(client, 0) is None


class TestFetchKeywordTv:
    @respx.mock
    def test_fetch_keyword_tv(self, fixtures: dict, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/discover/tv").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 1, "name": "Show"}]})
        )
        results = _fetch_keyword_tv(client, 12345)
        assert results == [{"id": 1, "name": "Show", "first_air_date": None}]


class TestEnrichOne:
    @respx.mock
    def test_enrich_one_full(self, fixtures: dict, tmp_project: Path, fake_image_client, client: TMDBClient) -> None:
        movie = fixtures["movie_475557"]
        movie_id = movie["id"]
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(return_value=httpx.Response(200, json=movie))
        coll = movie.get("belongs_to_collection")
        if coll:
            respx.get(f"https://api.themoviedb.org/3/collection/{coll['id']}").mock(
                return_value=httpx.Response(200, json=fixtures["collection_633215"])
            )
        index = load_index()
        details = {"movies": {}}
        membership, detail = ensure_record_exists(index, details, movie_id)
        _enrich_one(client, membership, detail, _LockedCache(), _LockedListCache(), fake_image_client)
        assert membership["title"]
        assert detail["runtime"] == movie["runtime"]
        assert detail["enriched_at"].endswith("Z")
        assert membership.get("poster_file")

    @respx.mock
    def test_enrich_one_404_marks_gone(
        self, fixtures: dict, tmp_project: Path, fake_image_client, client: TMDBClient
    ) -> None:
        movie_id = 999999999
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(return_value=httpx.Response(404))
        index = load_index()
        details = {"movies": {}}
        membership, detail = ensure_record_exists(index, details, movie_id)
        _enrich_one(client, membership, detail, _LockedCache(), _LockedListCache(), fake_image_client)
        assert membership["gone"] is True
        assert "gone_since" in membership


class TestRunFullScanReporting:
    """Full scan must name which movies it touched, not just print a count."""

    def test_reports_enriched_gone_and_failed_titles(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        client: TMDBClient,
    ) -> None:
        save_index(
            {
                "movies": {
                    "1": {"id": 1, "title": "Movie One", "release_date": "2020-01-01"},
                    "2": {"id": 2, "title": "Movie Two", "release_date": "2021-01-01"},
                    "3": {"id": 3, "title": "Movie Three", "release_date": "2022-01-01"},
                }
            }
        )

        def fake_enrich_one(_client, membership, _detail, _coll_cache, _kw_cache, _image_client):
            if membership["id"] == 2:
                membership["gone"] = True
                membership["gone_since"] = "2026-01-01T00:00:00Z"
                return
            if membership["id"] == 3:
                raise RuntimeError("boom")
            membership["title"] = membership["title"] + " (enriched)"

        monkeypatch.setattr(enrich_mod, "_enrich_one", fake_enrich_one)

        enrich_mod.run_full_scan(client, force=True, resume=False)

        out = capsys.readouterr().out
        assert "Movie One (enriched) (2020)" in out
        assert "Movie Two (2021)" in out
        assert "no longer on TMDB, marked gone" in out
        assert "Movie Three (2022)" in out
        assert "boom" in out
        assert "Full scan complete. Enriched 1 movie." in out
        assert "1 marked gone" in out
        assert "1 failed" in out

        saved = load_index()
        assert saved["movies"]["1"]["title"] == "Movie One (enriched)"
        assert saved["movies"]["2"]["gone"] is True
