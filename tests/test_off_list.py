"""Index movies that are not on the TMDB list: asked once, and listed when kept.

The index and the list should always match. A movie in the index but missing
from the list is asked about ("delete?"); one the user keeps is written to the
kept-movies file, so it can be checked on TMDB without searching the index,
and it leaves that file once it is back on the list or out of the index.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import main
from src import off_list
from src.list_fetcher import ListFetchResult
from src.tmdb_api import TMDBClient

LIST_ID = "8678795"


def _index(*ids: int, gone: tuple[int, ...] = ()) -> dict:
    movies = {str(i): {"id": i, "title": f"Movie {i}", "release_date": "2020-01-01"} for i in ids}
    for i in gone:
        movies[str(i)]["gone"] = True
    return {"movies": movies, "meta": {}}


def _list(*ids: int) -> list[dict]:
    return [{"media_type": "movie", "id": i, "title": f"Movie {i}", "release_date": "2020-01-01"} for i in ids]


def _client(session_id: str | None = "s") -> TMDBClient:
    return cast(TMDBClient, SimpleNamespace(session_id=session_id))


def _kept_ids() -> list[int]:
    path = off_list.kept_file()
    if not path.exists():
        return []
    return [entry["id"] for entry in json.loads(path.read_text(encoding="utf-8"))["movies"]]


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    snapshots: list[dict] = []
    monkeypatch.setattr(main._config, "TMDB_LIST_ID", LIST_ID)
    monkeypatch.setattr(main, "save_index", lambda data: snapshots.append(copy.deepcopy(data)))
    monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)
    return snapshots


def _answer(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    remaining = list(answers)
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        if not remaining:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return asked


class TestTheKeptFile:
    def test_it_lists_title_link_and_dates(self, _kept_off_list_file: Path):
        off_list.record_kept(_index(5)["movies"], [5])
        data = json.loads(_kept_off_list_file.read_text(encoding="utf-8"))
        (entry,) = data["movies"]
        assert entry["id"] == 5
        assert entry["title"] == "Movie 5 (2020)"
        assert entry["url"] == "https://www.themoviedb.org/movie/5"
        assert entry["kept_since"] and entry["last_seen_missing"]
        assert entry["gone_on_tmdb"] is False
        assert data["count"] == 1

    def test_keeping_again_keeps_the_first_date(self, monkeypatch):
        movies = _index(5)["movies"]
        monkeypatch.setattr(off_list, "now_iso", lambda: "2026-01-01T00:00:00Z")
        off_list.record_kept(movies, [5])
        monkeypatch.setattr(off_list, "now_iso", lambda: "2026-02-01T00:00:00Z")
        off_list.record_kept(movies, [5])
        (entry,) = off_list.load_kept().values()
        assert (entry["kept_since"], entry["last_seen_missing"]) == ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")

    def test_a_movie_marked_gone_says_so(self):
        off_list.record_kept(_index(5, gone=(5,))["movies"], [5])
        assert off_list.load_kept()["5"]["gone_on_tmdb"] is True

    def test_back_on_the_list_or_out_of_the_index_leaves_the_file(self):
        movies = _index(1, 2, 3)["movies"]
        off_list.record_kept(movies, [1, 2, 3])
        del movies["2"]
        off_list.reconcile_kept(movies, live_ids={1})
        assert _kept_ids() == [3]

    def test_the_file_goes_when_nothing_is_left(self, _kept_off_list_file: Path):
        movies = _index(1)["movies"]
        off_list.record_kept(movies, [1])
        off_list.reconcile_kept(movies, live_ids={1})
        assert not _kept_off_list_file.exists()

    def test_a_damaged_file_does_not_stop_anything(self, _kept_off_list_file: Path):
        _kept_off_list_file.write_text("{not json", encoding="utf-8")
        assert off_list.load_kept() == {}
        off_list.record_kept(_index(1)["movies"], [1])
        assert _kept_ids() == [1]


class TestTheVanishedPrompt:
    def test_no_lists_the_kept_movies(self, saved, monkeypatch):
        _answer(monkeypatch, "n")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3))
        assert _kept_ids() == [2, 3]
        assert saved == []

    def test_running_out_of_answers_keeps_and_lists_them(self, saved, monkeypatch):
        monkeypatch.setattr(main.prompts, "MAX_UNRECOGNIZED", 1)
        _answer(monkeypatch, "x")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2))
        assert _kept_ids() == [2]

    def test_yes_lists_nothing(self, saved, monkeypatch):
        _answer(monkeypatch, "y")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2))
        assert _kept_ids() == []

    def test_one_by_one_lists_only_the_skipped(self, saved, monkeypatch):
        _answer(monkeypatch, "o", "1", "3")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3))
        assert _kept_ids() == [3]

    def test_a_failed_re_add_is_listed_a_good_one_is_not(self, saved, monkeypatch):
        monkeypatch.setattr(
            main, "push_to_tmdb_list", lambda client, list_id, movie_id: {"success": movie_id == 2, "id": movie_id}
        )
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)  # no live re-check
        _answer(monkeypatch, "o", "2", "2")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3))
        assert _kept_ids() == [3]

    def test_re_add_without_a_session_is_listed(self, saved, monkeypatch):
        _answer(monkeypatch, "o", "2")
        main._prompt_clean_vanished(_client(session_id=None), _list(1), _index(1, 2))
        assert _kept_ids() == [2]

    def test_movies_already_asked_are_not_asked_again(self, saved, monkeypatch):
        _answer(monkeypatch)  # any prompt fails the test
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2), asked={2})
        assert _kept_ids() == []

    def test_an_incomplete_fetch_lists_nothing(self, saved, monkeypatch):
        _answer(monkeypatch)
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2), incomplete=True)
        assert _kept_ids() == []


class TestTheFastScan:
    """The fast scan asks once per movie that left the list."""

    @pytest.fixture
    def scan(self, saved, monkeypatch):
        def _run(index: dict, fetched: tuple[list[dict], bool], *, approve: tuple[str, ...] = ()) -> None:
            monkeypatch.setattr(main, "load_index", lambda: index)
            monkeypatch.setattr(main, "fetch_list", lambda *a, **k: fetched)
            monkeypatch.setattr(
                main.prompts, "confirm_category", lambda category, titles: category.split()[0] in approve
            )
            monkeypatch.setattr(main, "_render_mismatch_summary", lambda *a, **k: None)
            main.run_fast_scan(_client())

        return _run

    def test_declined_removals_are_listed_and_not_asked_twice(self, scan, monkeypatch):
        _answer(monkeypatch)  # the vanished prompt must not ask about them again
        scan(_index(1, 2), (_list(1, 3), False), approve=("Additions",))
        assert _kept_ids() == [2]

    def test_declining_everything_lists_them_too(self, scan, monkeypatch):
        _answer(monkeypatch)
        scan(_index(1, 2), (_list(1, 3), False))
        assert _kept_ids() == [2]

    def test_approved_but_not_saved_removals_are_listed(self, scan, monkeypatch):
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)
        _answer(monkeypatch)
        scan(_index(1, 2), (_list(1), False), approve=("Removals",))
        assert _kept_ids() == [2]

    def test_an_approved_removal_leaves_no_entry(self, scan, monkeypatch, saved):
        off_list.record_kept(_index(1, 2)["movies"], [2])
        index = _index(1, 2)
        _answer(monkeypatch)
        scan(index, (_list(1), False), approve=("Removals",))
        assert sorted(saved[-1]["movies"]) == ["1"]
        assert _kept_ids() == []

    def test_a_kept_movie_back_on_the_list_leaves_the_file(self, scan, monkeypatch):
        off_list.record_kept(_index(1, 2)["movies"], [2])
        _answer(monkeypatch)
        scan(_index(1, 2), (_list(1, 2), False))
        assert _kept_ids() == []

    def test_an_incomplete_fetch_leaves_the_file_alone(self, scan, monkeypatch):
        off_list.record_kept(_index(1, 2)["movies"], [2])
        _answer(monkeypatch)
        scan(_index(1, 2), (_list(1, 2), True))
        assert _kept_ids() == [2]

    def test_a_gone_movie_off_the_list_is_asked_not_called_up_to_date(self, scan, monkeypatch, capsys):
        """Removals leave movies marked gone out, so nothing else asked about this one."""
        asked = _answer(monkeypatch, "n")
        scan(_index(1, 2, gone=(2,)), (_list(1), False))
        out = capsys.readouterr().out
        assert "Index is up to date" not in out
        assert len(asked) == 1
        assert _kept_ids() == [2]


@pytest.fixture
def probe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "MISMATCH_REPORT_FILE", tmp_path / "mismatch_report.json")
    monkeypatch.setattr(main._config, "TMDB_LIST_ID", LIST_ID)

    def _run(result: ListFetchResult, index: dict) -> str:
        monkeypatch.setattr(main, "fetch_list_result", lambda *a, **k: result)
        main._probe_tmdb_status(_client(session_id=None), index)
        return capsys.readouterr().out

    return _run


class TestTheStartupTable:
    def test_equal_counts_with_different_movies_are_a_mismatch(self, probe):
        """One movie left the list, another was added: 2 and 2, which used to read "match"."""
        out = probe(ListFetchResult(_list(1, 3), False), _index(1, 2))
        row = next(line for line in out.splitlines() if "tmdb.org" in line).split()
        assert row == ["tmdb.org", "OK", "2", "2", "mismatch"]
        assert "1 movie(s) in your index are not on the TMDB list" in out
        assert "Movie 2 (2020)" in out
        assert "1 movie(s) on the TMDB list are not in your index" in out
        assert "Movie 3 (2020)" in out
        assert "Run option 2 (Fast scan)" in out

    def test_a_count_difference_keeps_its_sign(self, probe):
        out = probe(ListFetchResult(_list(1), False), _index(1, 2))
        assert "mismatch (+1)" in out

    def test_kept_movies_are_marked_and_the_file_named(self, probe):
        off_list.record_kept(_index(2)["movies"], [2])
        out = probe(ListFetchResult(_list(1), False), _index(1, 2))
        assert "Movie 2 (2020)" in out and "(kept)" in out
        assert "kept_not_on_list.json" in out

    def test_a_match_lists_nothing(self, probe):
        out = probe(ListFetchResult(_list(1, 2), False), _index(1, 2))
        assert "match" in out
        assert "not on the TMDB list" not in out

    def test_long_lists_are_cut_short(self, probe):
        out = probe(ListFetchResult([], False), _index(*range(1, 16)))
        assert "… and 5 more" in out

    def test_an_incomplete_read_names_no_movies_and_keeps_the_file(self, probe):
        off_list.record_kept(_index(2)["movies"], [2])
        out = probe(ListFetchResult(_list(1, 2), True), _index(1, 2))
        assert "not on the TMDB list" not in out
        assert _kept_ids() == [2]

    def test_a_complete_read_clears_resolved_entries(self, probe):
        off_list.record_kept(_index(2)["movies"], [2])
        probe(ListFetchResult(_list(1, 2), False), _index(1, 2))
        assert _kept_ids() == []
