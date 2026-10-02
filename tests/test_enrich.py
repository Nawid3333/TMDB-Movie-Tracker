"""Tests for src.enrich."""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

import config.config as _config
import src.enrich as enrich_mod
from config.config import TMDB_READ_MAX_RETRIES
from src.enrich import (
    _days_since_release,
    _enrich_one,
    _fetch_collection,
    _LockedCache,
    _release_year,
    _should_enrich,
    _volatility_tier,
)
from src.index import ensure_record_exists, load_details, load_index, save_index
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

    def test_a_timestamp_without_a_zone_is_read_as_utc(self) -> None:
        # Used to raise TypeError (naive minus aware), aborting the whole run.
        cold = {"status": "Released", "release_date": "2010-01-01"}
        assert _should_enrich(cold, {"enriched_at": "2020-01-01T00:00:00"}) is True
        recent = datetime.now(UTC).replace(tzinfo=None).isoformat()
        assert _should_enrich(cold, {"enriched_at": recent}) is False

    def test_a_non_string_timestamp_means_enrich_again(self) -> None:
        assert _should_enrich({"status": "Released", "release_date": "2010-01-01"}, {"enriched_at": 1700000000}) is True


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


class TestEnrichOne:
    def test_enrich_one_full(self, fixtures: dict, tmp_project: Path, fake_image_client, client: TMDBClient) -> None:
        movie = fixtures["movie_475557"]
        movie_id = movie["id"]
        # assert_all_called=False: the /discover route is there to catch a call, not to be called.
        with respx.mock(assert_all_called=False) as router:
            router.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(
                return_value=httpx.Response(200, json=movie)
            )
            coll = movie.get("belongs_to_collection")
            if coll:
                router.get(f"https://api.themoviedb.org/3/collection/{coll['id']}").mock(
                    return_value=httpx.Response(200, json=fixtures["collection_633215"])
                )
            discover = router.get(url__regex=r"https://api\.themoviedb\.org/3/discover/.*")
            index = load_index()
            details = {"movies": {}}
            membership, detail = ensure_record_exists(index, details, movie_id)
            _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)
        assert membership["title"]
        assert detail["runtime"] == movie["runtime"]
        assert detail["enriched_at"].endswith("Z")
        assert membership.get("poster_file")
        assert "enrich_incomplete" not in detail
        # Movies only: no TV lookup, and nothing written for one.
        assert not discover.called
        assert "connected_tv" not in detail

    @respx.mock
    def test_enrich_one_404_marks_gone(
        self, fixtures: dict, tmp_project: Path, fake_image_client, client: TMDBClient
    ) -> None:
        movie_id = 999999999
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(return_value=httpx.Response(404))
        index = load_index()
        details = {"movies": {}}
        membership, detail = ensure_record_exists(index, details, movie_id)
        _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)
        assert membership["gone"] is True
        assert "gone_since" in membership


def _minimal_movie_json(
    movie_id: int,
    *,
    status: str = "Released",
    runtime: int = 118,
    title: str = "Test Movie",
    release_date: str = "2020-01-01",
) -> dict:
    """The smallest TMDB /movie/{id} response _enrich_one can fully process.

    Every optional section (collection, credits) is left empty so
    _resolve_collection/download_poster both no-op --
    keeps these tests independent of the captured-fixture files.
    """
    return {
        "id": movie_id,
        "title": title,
        "original_title": title,
        "release_date": release_date,
        "status": status,
        "runtime": runtime,
        "overview": "",
        "tagline": "",
        "genres": [],
        "original_language": "en",
        "production_companies": [],
        "production_countries": [],
        "release_dates": {"results": []},
        "credits": {"cast": [], "crew": []},
        "keywords": {"keywords": []},
        "external_ids": {},
        "belongs_to_collection": None,
        "poster_path": None,
    }


class TestEnrichOneChangeDetection:
    """A re-enrich must say what changed, not just silently overwrite it."""

    @respx.mock
    def test_a_movies_first_enrichment_reports_no_changes(
        self, tmp_project: Path, fake_image_client, client: TMDBClient
    ) -> None:
        movie_id = 101
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(
            return_value=httpx.Response(200, json=_minimal_movie_json(movie_id))
        )
        index = load_index()
        details = {"movies": {}}
        membership, detail = ensure_record_exists(index, details, movie_id)

        changes = _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)

        assert changes == []

    @respx.mock
    def test_a_status_and_runtime_change_is_reported(
        self, tmp_project: Path, fake_image_client, client: TMDBClient
    ) -> None:
        movie_id = 102
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(
            return_value=httpx.Response(200, json=_minimal_movie_json(movie_id, status="Released", runtime=118))
        )
        index = load_index()
        details = {"movies": {}}
        membership, detail = ensure_record_exists(index, details, movie_id)
        # Simulate a movie enriched previously with now-stale data.
        membership["title"] = "Test Movie"
        membership["release_date"] = "2020-01-01"
        membership["status"] = "Post Production"
        detail["runtime"] = 90
        detail["enriched_at"] = "2025-01-01T00:00:00Z"

        changes = _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)

        by_field = {field: (old, new) for field, old, new in changes}
        assert by_field["Status"] == ("Post Production", "Released")
        assert by_field["Runtime"] == ("90 min", "118 min")
        assert "Title" not in by_field

    @respx.mock
    def test_nothing_changed_reports_no_changes(self, tmp_project: Path, fake_image_client, client: TMDBClient) -> None:
        movie_id = 103
        respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(
            return_value=httpx.Response(200, json=_minimal_movie_json(movie_id, status="Released", runtime=118))
        )
        index = load_index()
        details = {"movies": {}}
        membership, detail = ensure_record_exists(index, details, movie_id)
        membership["title"] = "Test Movie"
        membership["release_date"] = "2020-01-01"
        membership["status"] = "Released"
        detail["runtime"] = 118
        detail["enriched_at"] = "2025-01-01T00:00:00Z"

        changes = _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)

        assert changes == []


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

        def fake_enrich_one(_client, membership, _detail, _coll_cache, _image_client):
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

    def test_reports_per_field_changes_returned_by_enrich_one(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        client: TMDBClient,
    ) -> None:
        save_index({"movies": {"1": {"id": 1, "title": "Movie One", "release_date": "2020-01-01"}}})

        def fake_enrich_one(_client, membership, _detail, _coll_cache, _image_client):
            return [("Status", "Post Production", "Released")]

        monkeypatch.setattr(enrich_mod, "_enrich_one", fake_enrich_one)

        enrich_mod.run_full_scan(client, force=True, resume=False)

        out = capsys.readouterr().out
        # Once in the progress stream, once more in the recap after it.
        assert out.count("Status: Post Production → Released") == 2
        assert "● Movie One (2020)" in out
        assert "Changes since the last scan: 1" in out
        assert out.index("Changes since the last scan") < out.index("Full scan complete")
        assert "1 had field changes (listed above)." in out

    def test_unchanged_movies_keep_the_check_mark(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        client: TMDBClient,
    ) -> None:
        save_index({"movies": {"1": {"id": 1, "title": "Movie One", "release_date": "2020-01-01"}}})
        monkeypatch.setattr(enrich_mod, "_enrich_one", lambda *_args: [])

        enrich_mod.run_full_scan(client, force=True, resume=False)

        out = capsys.readouterr().out
        assert "✓ Movie One (2020)" in out
        assert "●" not in out
        assert "Changes since the last scan" not in out


# ── movies only, and collection lookups ─────────────────────────────────────

_KNOWN_PARTS = [{"id": 11, "title": "Star Wars"}, {"id": 1891, "title": "The Empire Strikes Back"}]
# What an older build left in details.json: connected TV found via keywords.
_OLD_TV = [{"id": 1399, "name": "The Mandalorian", "first_air_date": "2019-11-12", "via_keyword": "galaxy"}]


def _movie_with(movie_id: int, **fields) -> dict:
    return {**_minimal_movie_json(movie_id), **fields}


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries without the multi-second backoff between attempts."""
    monkeypatch.setattr("src.tmdb_api.TMDB_READ_RETRY_DELAY", 0.0)
    monkeypatch.setattr("src.tmdb_api.random.uniform", lambda a, b: 0.0)


class TestMoviesOnly:
    """The connected-TV lookup is gone: it cluttered the gaps report, and this tracker is for movies."""

    def test_a_full_scan_never_asks_tmdb_about_tv(self, tmp_project: Path, client: TMDBClient) -> None:
        save_index({"movies": {str(i): {"id": i, "title": f"M{i}"} for i in range(1, 4)}})
        # assert_all_called=False: the /discover route is there to catch a call, not to be called.
        with respx.mock(assert_all_called=False) as router:
            movies = router.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(side_effect=_movie_by_path)
            discover = router.get(url__regex=r"https://api\.themoviedb\.org/3/discover/.*")

            enrich_mod.run_full_scan(client, force=True, resume=False)

        assert movies.call_count == 3
        assert not discover.called
        assert "keywords" not in movies.calls.last.request.url.params["append_to_response"]

    @respx.mock
    def test_connected_tv_and_keywords_from_an_older_build_are_left_alone(
        self, fake_image_client, client: TMDBClient
    ) -> None:
        """Inert derived data: neither read nor rewritten, and not deleted either."""
        respx.get("https://api.themoviedb.org/3/movie/11").mock(side_effect=_movie_by_path)
        detail = {"id": 11, "connected_tv": _OLD_TV, "keywords": ["galaxy"]}

        _enrich_one(client, {"id": 11}, detail, _LockedCache(), fake_image_client)

        assert detail["connected_tv"] == _OLD_TV
        assert detail["keywords"] == ["galaxy"]

    def test_a_leftover_tv_lookup_marker_does_not_make_a_movie_due(self) -> None:
        cold = {"status": "Released", "release_date": "1977-05-25"}
        fresh = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        assert _should_enrich(cold, {"enriched_at": fresh, "enrich_incomplete": ["connected_tv"]}) is False
        assert _should_enrich(cold, {"enriched_at": fresh, "enrich_incomplete": ["collection"]}) is True


class TestCollectionLookupFailure:
    """A failed collection fetch used to wipe the known parts and still stamp the movie fresh."""

    def _movie(self) -> dict:
        return _movie_with(
            11,
            release_date="1977-05-25",
            belongs_to_collection={"id": 10, "name": "Star Wars Collection"},
        )

    @respx.mock
    def test_the_known_parts_are_kept_and_the_movie_is_due_again(
        self, fake_image_client, client: TMDBClient, no_backoff
    ) -> None:
        respx.get("https://api.themoviedb.org/3/movie/11").mock(return_value=httpx.Response(200, json=self._movie()))
        respx.get("https://api.themoviedb.org/3/collection/10").mock(return_value=httpx.Response(503))
        membership = {"id": 11, "title": "Star Wars"}
        detail = {
            "id": 11,
            "enriched_at": "2026-01-01T00:00:00Z",
            "collection": {"id": 10, "name": "Star Wars Collection", "parts": _KNOWN_PARTS},
        }

        _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)

        assert detail["collection"]["parts"] == _KNOWN_PARTS
        assert detail["enrich_incomplete"] == ["collection"]
        # A cold film enriched moments ago is not normally due for 90 days.
        assert _should_enrich(membership, detail) is True

    @respx.mock
    def test_a_complete_enrich_clears_the_marker(self, fake_image_client, client: TMDBClient) -> None:
        respx.get("https://api.themoviedb.org/3/movie/11").mock(return_value=httpx.Response(200, json=self._movie()))
        respx.get("https://api.themoviedb.org/3/collection/10").mock(
            return_value=httpx.Response(200, json={"id": 10, "name": "Star Wars Collection", "parts": _KNOWN_PARTS})
        )
        membership, detail = {"id": 11}, {"id": 11, "enrich_incomplete": ["collection"]}

        _enrich_one(client, membership, detail, _LockedCache(), fake_image_client)

        assert [p["id"] for p in detail["collection"]["parts"]] == [11, 1891]
        assert "enrich_incomplete" not in detail
        assert _should_enrich({"status": "Released", "release_date": "1977-05-25"}, detail) is False

    @respx.mock
    def test_a_failing_collection_is_asked_once_per_run(
        self, fake_image_client, client: TMDBClient, no_backoff
    ) -> None:
        respx.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(
            return_value=httpx.Response(200, json=self._movie())
        )
        collection = respx.get("https://api.themoviedb.org/3/collection/10").mock(return_value=httpx.Response(503))
        cache = _LockedCache()

        for _ in range(3):
            _enrich_one(client, {"id": 11}, {"id": 11}, cache, fake_image_client)

        # One lookup with its retries, not one per movie in the collection.
        assert collection.call_count == TMDB_READ_MAX_RETRIES


# ── full-scan progress, interrupts and a rejected key ───────────────────────


def _index_of(count: int, **extra) -> dict:
    return {
        "movies": {
            str(i): {"id": i, "title": f"M{i}", "release_date": "2000-01-01", **extra} for i in range(1, count + 1)
        }
    }


def _movie_by_path(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_minimal_movie_json(int(request.url.path.rsplit("/", 1)[1])))


class TestFullScanProgress:
    @respx.mock
    def test_an_interrupt_saves_what_finished_and_the_checkpoint_vouches_for_it(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch, client: TMDBClient
    ) -> None:
        """The checkpoint used to be written alone, every 60 s.

        After an interrupt the next run skipped the checkpointed movies
        although none of their data had been saved, called itself complete,
        and deleted the checkpoint: those movies were never enriched.
        """
        save_index(_index_of(12))
        respx.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(side_effect=_movie_by_path)
        monkeypatch.setattr(enrich_mod, "_CHECKPOINT_SECONDS", 0.0)
        real_title_link = enrich_mod.title_link
        rows = {"n": 0}

        def ctrl_c_on_the_fifth_row(record: dict, **kwargs) -> str:
            rows["n"] += 1
            if rows["n"] == 5:
                raise KeyboardInterrupt
            return real_title_link(record, **kwargs)

        monkeypatch.setattr(enrich_mod, "title_link", ctrl_c_on_the_fifth_row)
        with pytest.raises(KeyboardInterrupt):
            enrich_mod.run_full_scan(client, force=True, resume=True)

        checkpoint = json.loads(_config.ENRICH_CHECKPOINT_FILE.read_text(encoding="utf-8"))["done"]
        on_disk = {int(k) for k, v in load_details()["movies"].items() if v.get("enriched_at")}
        assert checkpoint, "nothing was checkpointed"
        assert set(checkpoint) <= on_disk, "the checkpoint names a movie whose data was not saved"

        monkeypatch.setattr(enrich_mod, "title_link", real_title_link)
        enrich_mod.run_full_scan(client, force=True, resume=True)

        saved = load_details()["movies"]
        assert all(saved[str(i)].get("enriched_at") for i in range(1, 13))
        assert not _config.ENRICH_CHECKPOINT_FILE.exists()

    @respx.mock
    def test_an_interrupt_drops_the_queued_movies(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch, client: TMDBClient
    ) -> None:
        """Leaving the worker pool used to wait for every queued movie: one
        Ctrl+C still fetched the whole index, then threw it all away."""
        save_index(_index_of(40))
        requested: list[int] = []

        def slow_movie(request: httpx.Request) -> httpx.Response:
            requested.append(1)
            time.sleep(0.02)
            return _movie_by_path(request)

        respx.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(side_effect=slow_movie)
        monkeypatch.setattr(enrich_mod, "TMDB_DETAIL_WORKERS", 2)

        def ctrl_c(record: dict, **kwargs) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(enrich_mod, "title_link", ctrl_c)
        with pytest.raises(KeyboardInterrupt):
            enrich_mod.run_full_scan(client, force=True, resume=False)

        assert len(requested) < 10, f"{len(requested)} of 40 movies were still fetched after the interrupt"

    @respx.mock
    def test_a_401_stops_the_scan_without_showing_the_credentials(
        self, tmp_project: Path, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """httpx's own error message quotes the URL, api_key and session_id included.

        And a rejected key used to fail every movie in the index, one leaking
        row each. The scan stops now; only the movies the workers had already
        picked up still go out.
        """
        save_index(_index_of(80))
        movies = respx.get(url__regex=r"https://api\.themoviedb\.org/3/movie/\d+").mock(
            return_value=httpx.Response(401, json={"status_code": 7})
        )
        # The project's own loggers. httpx logs every request URL at INFO,
        # which is why setup_logging() holds the httpx logger at WARNING.
        caplog.set_level(logging.DEBUG, logger="src")
        with TMDBClient(api_key="SECRETKEY123") as secret_client:
            secret_client.session_id = "SECRETSESSION456"
            enrich_mod.run_full_scan(secret_client, force=True, resume=False)

        captured = capsys.readouterr()
        shown = captured.out + captured.err + caplog.text
        assert "SECRETKEY123" not in shown
        assert "SECRETSESSION456" not in shown
        assert "HTTP 401 for GET /3/movie/" in shown
        assert "TMDB rejected the API key or session" in captured.out
        assert movies.call_count < 40, f"{movies.call_count} of 80 movies were still asked for"


class TestGoneMovies:
    """One 404 used to freeze a movie for good: nothing ever asked about it again."""

    @respx.mock
    def test_an_ordinary_scan_does_not_ask_about_a_gone_movie(self, tmp_project: Path, client: TMDBClient) -> None:
        save_index({"movies": {"1": {"id": 1, "title": "Gone", "gone": True}, "2": {"id": 2, "title": "Here"}}})
        gone = respx.get("https://api.themoviedb.org/3/movie/1").mock(side_effect=_movie_by_path)
        respx.get("https://api.themoviedb.org/3/movie/2").mock(side_effect=_movie_by_path)

        enrich_mod.run_full_scan(client, force=False, resume=False)

        assert not gone.called
        assert load_index()["movies"]["1"]["gone"] is True

    @respx.mock
    def test_force_asks_again_and_a_movie_tmdb_has_is_tracked_again(
        self, tmp_project: Path, client: TMDBClient
    ) -> None:
        save_index({"movies": {"1": {"id": 1, "title": "Gone", "gone": True, "gone_since": "2026-01-01T00:00:00Z"}}})
        respx.get("https://api.themoviedb.org/3/movie/1").mock(side_effect=_movie_by_path)

        enrich_mod.run_full_scan(client, force=True, resume=False)

        saved = load_index()["movies"]["1"]
        assert saved["gone"] is False
        assert "gone_since" not in saved
        assert load_details()["movies"]["1"].get("enriched_at")
