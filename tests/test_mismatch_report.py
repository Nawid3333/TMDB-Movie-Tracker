"""The mismatch report must only count movies.

A TMDB list can hold TV shows alongside movies -- each raw list item carries
its own "media_type" -- but this tracker's index is movie-only (the fast-scan
path already filters on this in src.changes._extract_list_movie). Before this
fix, the mismatch report counted every item regardless of type, so a TV show
sitting on the list showed up as a phantom "extra on list" movie: wrong total,
wrong title (borrowed from the TV show's name/first_air_date), and a
/movie/{id} URL that points at an unrelated or nonexistent film.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import main


@pytest.fixture
def _movie_index():
    return {"movies": {"1": {"title": "Movie One", "release_date": "2020-01-01"}}}


class TestMovieOnlyIds:
    def test_tv_items_are_excluded(self):
        items = [
            {"media_type": "movie", "id": 1},
            {"media_type": "tv", "id": 277439},
        ]
        assert main._movie_only_ids(items) == {1}

    def test_items_without_a_media_type_are_excluded(self):
        """Only items explicitly tagged "movie" count -- an untagged item is
        not assumed to be one."""
        assert main._movie_only_ids([{"id": 1}]) == set()


class TestMismatchReportIgnoresNonMovies:
    def test_a_tv_show_on_the_list_is_not_reported_as_an_extra_movie(self, _movie_index, tmp_path, monkeypatch):
        report_path = tmp_path / "mismatch_report.json"
        monkeypatch.setattr(main, "MISMATCH_REPORT_FILE", report_path)

        items = [
            {"media_type": "movie", "id": 1, "title": "Movie One", "release_date": "2020-01-01"},
            {"media_type": "tv", "id": 277439, "name": "Cape Fear", "first_air_date": "2026-01-01"},
        ]

        main._save_mismatch_report(_movie_index, items, set())

        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["list_total"] == 1
        assert report["extra_on_list_count"] == 0
        assert report["extra_on_list"] == []

    def test_the_tv_show_is_surfaced_separately_instead_of_silently_dropped(
        self, _movie_index, tmp_path, monkeypatch
    ):
        report_path = tmp_path / "mismatch_report.json"
        monkeypatch.setattr(main, "MISMATCH_REPORT_FILE", report_path)

        items = [
            {"media_type": "movie", "id": 1, "title": "Movie One", "release_date": "2020-01-01"},
            {"media_type": "tv", "id": 277439, "name": "Cape Fear", "first_air_date": "2026-01-01"},
        ]

        main._save_mismatch_report(_movie_index, items, set())

        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["non_movie_on_list"] == [
            {
                "id": 277439,
                "media_type": "tv",
                "title": "Cape Fear",
                "url": "https://www.themoviedb.org/tv/277439",
            }
        ]

    def test_a_real_extra_movie_still_gets_reported(self, _movie_index, tmp_path, monkeypatch):
        report_path = tmp_path / "mismatch_report.json"
        monkeypatch.setattr(main, "MISMATCH_REPORT_FILE", report_path)

        items = [
            {"media_type": "movie", "id": 1, "title": "Movie One", "release_date": "2020-01-01"},
            {"media_type": "movie", "id": 2, "title": "Movie Two", "release_date": "2021-01-01"},
            {"media_type": "tv", "id": 277439, "name": "Cape Fear", "first_air_date": "2026-01-01"},
        ]

        main._save_mismatch_report(_movie_index, items, set())

        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["list_total"] == 2
        assert report["extra_on_list_count"] == 1
        assert report["extra_on_list"][0]["id"] == 2

    def test_the_printed_summary_matches_the_saved_report(self, _movie_index, tmp_path, monkeypatch, capsys):
        report_path = tmp_path / "mismatch_report.json"
        monkeypatch.setattr(main, "MISMATCH_REPORT_FILE", report_path)

        items = [
            {"media_type": "movie", "id": 1, "title": "Movie One", "release_date": "2020-01-01"},
            {"media_type": "tv", "id": 277439, "name": "Cape Fear", "first_air_date": "2026-01-01"},
        ]

        main._render_mismatch_summary(_movie_index, items, set())

        printed = capsys.readouterr().out
        assert "List total:   1" in printed
        assert "Extra on list (on site, not in index):     0" in printed


class TestNonMovieItems:
    def test_normalizes_id_title_and_link_by_media_type(self):
        items = [
            {"media_type": "movie", "id": 1, "title": "Movie One"},
            {"media_type": "tv", "id": 277439, "name": "Cape Fear"},
        ]
        assert main._non_movie_items(items) == [
            {
                "id": 277439,
                "media_type": "tv",
                "title": "Cape Fear",
                "url": "https://www.themoviedb.org/tv/277439",
            }
        ]

    def test_duplicate_ids_are_collapsed(self):
        items = [
            {"media_type": "tv", "id": 5, "name": "Show"},
            {"media_type": "tv", "id": 5, "name": "Show"},
        ]
        assert len(main._non_movie_items(items)) == 1


class TestNotifyNonMovieListItems:
    """Covers the terminal notification + optional removal for non-movie list items."""

    def test_does_nothing_when_the_list_is_all_movies(self, capsys):
        client = SimpleNamespace(session_id="s")
        main._notify_non_movie_list_items(client, [{"media_type": "movie", "id": 1}])
        assert capsys.readouterr().out == ""

    def test_prints_the_item_and_its_own_link_for_verification(self, monkeypatch, capsys):
        client = SimpleNamespace(session_id="s")
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        printed = capsys.readouterr().out
        assert "Cape Fear" in printed
        assert "https://www.themoviedb.org/tv/277439" in printed

    def test_without_either_v4_credential_it_points_at_the_link_instead_of_prompting(self, monkeypatch, capsys):
        """Removing a non-movie item needs the v4 API. With neither the final
        access token nor the read-access token needed to go get one, offering
        to remove would just be a dead end, so it should skip any prompt."""
        client = SimpleNamespace(session_id="s")
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main._config, "TMDB_V4_ACCESS_TOKEN", "")
        monkeypatch.setattr(main._config, "TMDB_API_READ_ACCESS_TOKEN", "")
        asked = []
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: asked.append(1) or False)

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert asked == []
        assert "TMDB_API_READ_ACCESS_TOKEN" in capsys.readouterr().out

    def test_with_a_read_access_token_it_offers_to_set_up_v4_access(self, monkeypatch, capsys):
        """With TMDB_API_READ_ACCESS_TOKEN configured but no final access
        token yet, the user should be offered the acquisition flow instead of
        a dead end -- declining leaves everything on the list, untouched.

        prompts.confirm is mocked out entirely (it owns its own printing/
        input), so the offer is verified via the prompt text it was called
        with, not via captured stdout.
        """
        client = SimpleNamespace(session_id="s", acquire_v4_access_token=lambda: pytest.fail("should not run"))
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main._config, "TMDB_V4_ACCESS_TOKEN", "")
        monkeypatch.setattr(main._config, "TMDB_API_READ_ACCESS_TOKEN", "fake_read_token")
        prompts_seen = []
        monkeypatch.setattr(main.prompts, "confirm", lambda prompt, **k: prompts_seen.append(prompt) or False)

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert any("Set up TMDB v4 access now" in p for p in prompts_seen)

    def test_accepting_setup_acquires_a_token_and_proceeds_to_remove(self, monkeypatch, capsys):
        """Accepting the v4 setup offer, succeeding, should fall straight
        through to the normal removal prompt/flow in the same run."""
        client = SimpleNamespace(session_id="s", acquire_v4_access_token=lambda: "fresh_v4_token")
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main._config, "TMDB_V4_ACCESS_TOKEN", "")
        monkeypatch.setattr(main._config, "TMDB_API_READ_ACCESS_TOKEN", "fake_read_token")
        confirms = iter([True, True])  # 1: set up v4 access, 2: remove now
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: next(confirms))
        calls = []
        monkeypatch.setattr(main, "remove_from_tmdb_list", lambda *a, **k: calls.append(a) or {"success": True})

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert calls == [(client, "8678795", 277439, "tv")]
        assert main._config.TMDB_V4_ACCESS_TOKEN == "fresh_v4_token"
        assert "Removed 1" in capsys.readouterr().out

    def test_a_failed_acquisition_falls_back_to_the_manual_link(self, monkeypatch, capsys):
        client = SimpleNamespace(session_id="s", acquire_v4_access_token=lambda: None)
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main._config, "TMDB_V4_ACCESS_TOKEN", "")
        monkeypatch.setattr(main._config, "TMDB_API_READ_ACCESS_TOKEN", "fake_read_token")
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)
        removed = []
        monkeypatch.setattr(main, "remove_from_tmdb_list", lambda *a, **k: removed.append(a))

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert removed == []
        assert "Could not obtain a v4 access token" in capsys.readouterr().out

    def test_declining_leaves_the_item_on_the_list(self, monkeypatch, capsys):
        client = SimpleNamespace(session_id="s")
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main._config, "TMDB_V4_ACCESS_TOKEN", "fake_v4_token")
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)
        removed = []
        monkeypatch.setattr(main, "remove_from_tmdb_list", lambda *a, **k: removed.append(a))

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert removed == []

    def test_confirming_removes_it_via_the_api(self, monkeypatch, capsys):
        client = SimpleNamespace(session_id="s")
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main._config, "TMDB_V4_ACCESS_TOKEN", "fake_v4_token")
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)
        calls = []

        def _fake_remove(client_arg, list_id, media_id, media_type):
            calls.append((list_id, media_id, media_type))
            return {"success": True}

        monkeypatch.setattr(main, "remove_from_tmdb_list", _fake_remove)

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert calls == [("8678795", 277439, "tv")]
        assert "Removed 1" in capsys.readouterr().out

    def test_without_a_session_it_only_notifies_and_does_not_prompt(self, monkeypatch, capsys):
        client = SimpleNamespace(session_id=None)
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        asked = []
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: asked.append(1) or False)

        main._notify_non_movie_list_items(client, [{"media_type": "tv", "id": 277439, "name": "Cape Fear"}])

        assert asked == []
        assert "Cape Fear" in capsys.readouterr().out
