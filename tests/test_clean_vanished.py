"""Vanished-movie cleanup: the prompt that deletes movies from the index.

A movie in the index but no longer on the live TMDB list is offered for
deletion, in bulk or one by one. These pin down what each answer removes,
that nothing is saved unless something was actually chosen, and that a TV
show on the list cannot stand in for a movie that shares its numeric id.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import cast

import pytest

import main
from src.tmdb_api import TMDBClient

LIST_ID = "8678795"


def _index(*ids: int) -> dict:
    return {"movies": {str(i): {"id": i, "title": f"Movie {i}", "release_date": "2020-01-01"} for i in ids}}


def _list(*ids: int, media_type: str = "movie") -> list[dict]:
    return [{"media_type": media_type, "id": i, "title": f"Movie {i}", "release_date": "2020-01-01"} for i in ids]


def _client(session_id: str | None = "s") -> TMDBClient:
    return cast(TMDBClient, SimpleNamespace(session_id=session_id))


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every index snapshot the prompt saves."""
    snapshots: list[dict] = []
    monkeypatch.setattr(main._config, "TMDB_LIST_ID", LIST_ID)
    monkeypatch.setattr(main, "save_index", lambda data: snapshots.append(copy.deepcopy(data)))
    monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)
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


def _kept(snapshot: dict) -> list[int]:
    return sorted(int(k) for k in snapshot["movies"])


class TestNothingToAsk:
    def test_without_a_configured_list_nothing_is_asked(self, saved, monkeypatch):
        monkeypatch.setattr(main._config, "TMDB_LIST_ID", "")
        _answer(monkeypatch)
        main._prompt_clean_vanished(_client(), _list(), _index(1, 2))
        assert saved == []

    def test_when_every_movie_is_still_listed_nothing_is_asked(self, saved, monkeypatch):
        _answer(monkeypatch)
        main._prompt_clean_vanished(_client(), _list(1, 2), _index(1, 2))
        assert saved == []

    def test_a_tv_show_sharing_an_id_does_not_hide_a_missing_movie(self, saved, monkeypatch, capsys):
        asked = _answer(monkeypatch, "n")
        main._prompt_clean_vanished(_client(), _list(1) + _list(2, media_type="tv"), _index(1, 2))
        assert len(asked) == 1
        assert "Movie 2" in capsys.readouterr().out


class TestIncompleteFetch:
    """A movie on a list page that failed to load looks exactly like one that left."""

    def test_an_incomplete_fetch_offers_nothing_for_deletion(self, saved, monkeypatch, capsys):
        _answer(monkeypatch)
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3), incomplete=True)
        assert saved == []
        assert "incomplete" in capsys.readouterr().out

    def test_the_menu_option_honours_an_incomplete_fetch(self, saved, monkeypatch):
        monkeypatch.setattr(main, "fetch_list", lambda *a, **k: (_list(1), True))
        monkeypatch.setattr(main, "load_index", lambda: _index(1, 2, 3))
        _answer(monkeypatch)
        main.run_clean_vanished(_client())
        assert saved == []

    def test_the_menu_option_skips_on_no_like_the_scan_does(self, saved, monkeypatch):
        # The menu used to run its own copy of this prompt, where "n" fell
        # through to asking about every movie one by one.
        monkeypatch.setattr(main, "fetch_list", lambda *a, **k: (_list(1), False))
        monkeypatch.setattr(main, "load_index", lambda: _index(1, 2, 3))
        asked = _answer(monkeypatch, "n")
        main.run_clean_vanished(_client())
        assert len(asked) == 1
        assert saved == []

    def test_the_fast_scan_passes_its_incomplete_flag_on(self, saved, monkeypatch):
        calls: list[dict] = []
        index = _index(1, 2)
        index["meta"] = {}
        monkeypatch.setattr(main, "load_index", lambda: index)
        monkeypatch.setattr(main, "fetch_list", lambda *a, **k: (_list(1, 3), True))
        monkeypatch.setattr(main.prompts, "confirm_category", lambda *a, **k: True)
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)
        monkeypatch.setattr(main, "_render_mismatch_summary", lambda *a, **k: None)
        monkeypatch.setattr(main, "_prompt_clean_vanished", lambda *a, **k: calls.append(k))
        main.run_fast_scan(_client())
        assert calls == [{"incomplete": True}]


class TestBulkAnswer:
    def test_yes_deletes_only_the_missing_movies(self, saved, monkeypatch):
        _answer(monkeypatch, "y")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3))
        assert [_kept(s) for s in saved] == [[1]]

    def test_no_changes_nothing(self, saved, monkeypatch):
        index = _index(1, 2)
        _answer(monkeypatch, "n")
        main._prompt_clean_vanished(_client(), _list(1), index)
        assert saved == []
        assert _kept(index) == [1, 2]


class TestOneByOne:
    """Any other bulk answer walks the movies one at a time."""

    def test_only_the_movies_marked_for_deletion_go(self, saved, monkeypatch):
        _answer(monkeypatch, "", "1", "3")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3))
        assert [_kept(s) for s in saved] == [[1, 3]]

    def test_enter_skips_and_saves_nothing(self, saved, monkeypatch):
        _answer(monkeypatch, "", "", "")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2, 3))
        assert saved == []

    def test_re_add_without_a_session_pushes_nothing(self, saved, monkeypatch):
        pushed: list[tuple] = []
        monkeypatch.setattr(main, "push_to_tmdb_list", lambda *a: pushed.append(a) or {"success": True})
        _answer(monkeypatch, "", "2")
        main._prompt_clean_vanished(_client(session_id=None), _list(1), _index(1, 2))
        assert pushed == []
        assert saved == []

    def test_re_add_pushes_the_chosen_movie_and_keeps_it(self, saved, monkeypatch):
        pushed: list[tuple] = []
        monkeypatch.setattr(main, "push_to_tmdb_list", lambda *a: pushed.append(a[1:]) or {"success": True})
        _answer(monkeypatch, "", "2")
        main._prompt_clean_vanished(_client(), _list(1), _index(1, 2))
        assert pushed == [(LIST_ID, 2)]
        assert _kept(saved[-1]) == [1, 2]
        assert saved[-1]["movies"]["2"]["remote_push"] == "rescraped"
