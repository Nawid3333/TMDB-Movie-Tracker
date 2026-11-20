"""The startup table and the main menu, as the user sees them.

Two things went wrong here on 2026-11-20. The startup table said FAILED
because TMDB counted a list entry it never returns, and the push option told
the user to "Run option 1 (Fast scan)" while Fast scan is option 2.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import main
from src.list_fetcher import ListFetchResult
from src.tmdb_api import TMDBClient


def _movies(*ids: int) -> list[dict]:
    return [{"media_type": "movie", "id": i, "title": f"M{i}", "release_date": "2020-01-01"} for i in ids]


def _index(*ids: int) -> dict:
    return {"movies": {str(i): {"title": f"M{i}", "release_date": "2020-01-01"} for i in ids}}


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """Run the startup probe against a canned fetch result; returns the printed text."""
    monkeypatch.setattr(main, "MISMATCH_REPORT_FILE", tmp_path / "mismatch_report.json")
    monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")

    def _run(result: ListFetchResult, index: dict, capsys) -> str:
        monkeypatch.setattr(main, "fetch_list_result", lambda *a, **k: result)
        main._probe_tmdb_status(cast(TMDBClient, SimpleNamespace(session_id=None)), index)
        return capsys.readouterr().out

    return _run


def _status_row(out: str) -> list[str]:
    # The host column's exact label, not a substring test: CodeQL reads
    # `"tmdb.org" in line` as an incomplete URL check.
    return next(cells for cells in (line.split() for line in out.splitlines()) if cells[:1] == ["tmdb.org"])


class TestStartupProbe:
    def test_entries_tmdb_hides_do_not_fail_the_probe(self, probe, capsys):
        out = probe(ListFetchResult(_movies(1, 2, 3), False, hidden=1, expected=4), _index(1, 2, 3), capsys)
        assert _status_row(out) == ["tmdb.org", "OK", "3", "3", "match"]
        assert "TMDB counts 4 entries on the list but shows only 3" in out
        assert "1 is a movie TMDB hides" in out

    def test_several_hidden_entries_read_as_a_plural(self, probe, capsys):
        out = probe(ListFetchResult(_movies(1), False, hidden=2, expected=3), _index(1), capsys)
        assert "2 are movies TMDB hides" in out

    def test_a_clean_fetch_prints_no_note(self, probe, capsys):
        out = probe(ListFetchResult(_movies(1, 2), False, expected=2), _index(1, 2), capsys)
        assert _status_row(out) == ["tmdb.org", "OK", "2", "2", "match"]
        assert "TMDB counts" not in out

    def test_a_real_short_read_still_fails(self, probe, capsys):
        out = probe(ListFetchResult(_movies(1), True, expected=2), _index(1, 2), capsys)
        assert _status_row(out)[:2] == ["tmdb.org", "FAILED"]
        assert "TMDB counts" not in out


class TestMenu:
    def test_the_menu_reads_exactly_as_before(self, capsys):
        """The menu now comes from MENU; its text must not have moved a character."""
        main.show_menu()
        lines = capsys.readouterr().out.splitlines()
        assert lines[-6:] == [
            "  1. Full scan       — every detail (slow, accurate)",
            "  2. Fast scan       — list membership only (quick)",
            "  3. Franchise gaps  — films from your movies' collections you missed",
            "  4. Push URL file   — push URLs/IDs to remote list without adding locally",
            "  5. Force re-enrich — re-fetch every movie, ignoring freshness",
            "  0. Exit",
        ]

    def test_menu_option_finds_each_number(self):
        assert [main.menu_option(label) for _key, label, _blurb in main.MENU] == ["1", "2", "3", "4", "5"]
        assert main.menu_option("Fast scan") == "2"
        with pytest.raises(KeyError):
            main.menu_option("Search & add")

    def test_the_fast_scan_hint_names_the_fast_scan_option(self):
        assert "Run option 2 (Fast scan)" in main._fast_scan_hint()

    def test_no_hint_hardcodes_a_menu_number(self):
        """Every "option N" in main.py's user-facing text must come from MENU."""
        source = Path(main.__file__).read_text(encoding="utf-8")
        assert not re.findall(r"[Oo]ption \d", source)


class TestPushHints:
    """Both hints in option 4 point at the Fast scan's real number."""

    @pytest.fixture
    def push(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "8678795")
        monkeypatch.setattr(main, "_select_batch_source", lambda: ("single", "550"))
        monkeypatch.setattr(main, "resolve_movie_input", lambda client, value: ({"id": 550, "title": "Fight Club"}, ""))
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)
        monkeypatch.setattr(main, "push_to_tmdb_list", lambda *a, **k: {"success": True, "remote_push": "ok"})
        return cast(TMDBClient, SimpleNamespace(session_id="s"))

    def test_nothing_new_to_push(self, push, monkeypatch, capsys):
        monkeypatch.setattr(main, "fetch_list", lambda *a, **k: (_movies(550), False))
        main.run_push_url_file_only(push)
        out = capsys.readouterr().out
        assert "Nothing new to push" in out
        assert "Run option 2 (Fast scan)" in out
        assert "option 1" not in out

    def test_after_a_push(self, push, monkeypatch, capsys):
        monkeypatch.setattr(main, "fetch_list", lambda *a, **k: ([], False))
        main.run_push_url_file_only(push)
        out = capsys.readouterr().out
        assert "1 ok" in out
        assert "Run option 2 (Fast scan)" in out
        assert "option 1" not in out
