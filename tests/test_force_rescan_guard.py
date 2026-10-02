"""Force re-enrich must ask before spending the whole rate limit.

Menu option 5 ignores the freshness tiers and asks TMDB for every movie in
the index. On a large index that is a long run and a real slice of the rate
limit, and it sits one keystroke from the fast scan on the same menu -- so it
confirms first, and a refusal must not reach the enrichment call at all.
"""

from pathlib import Path
from typing import cast
from unittest import mock

import pytest

import main
from src.tmdb_api import TMDBClient


@pytest.fixture
def _index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A three-movie index, so the prompt has a count to report."""
    monkeypatch.setattr(main, "load_index", lambda: {"movies": {"1": {}, "2": {}, "3": {}}})


class TestForceRescanConfirms:
    def test_declining_does_not_start_a_scan(self, _index, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        started = []
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: started.append(k))
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)

        main.run_force_full_scan(cast(TMDBClient, object()))

        assert started == []
        assert "Cancelled" in capsys.readouterr().out

    def test_accepting_forces_the_scan(self, _index, monkeypatch: pytest.MonkeyPatch) -> None:
        started = []
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: started.append(k))
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)

        main.run_force_full_scan(cast(TMDBClient, object()))

        assert len(started) == 1
        assert started[0]["force"] is True

    def test_the_prompt_says_how_many_movies_it_will_refetch(
        self, _index, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: None)
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)

        main.run_force_full_scan(cast(TMDBClient, object()))

        assert "all 3 movie(s)" in capsys.readouterr().out

    def test_enter_alone_does_not_launch_it(self, _index, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """An accidental Enter must not launch it.

        It used to be safe by a default ("[y/N]", Enter meant no). There are no
        defaults now: Enter is asked again like any other non-answer, and a run
        of them ends in the safe answer, no.
        """
        # A finite feed: asking once more than allowed raises StopIteration
        # instead of looping forever.
        feed = mock.Mock(side_effect=[""] * main.prompts.MAX_UNRECOGNIZED)
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: pytest.fail("should not run"))
        monkeypatch.setattr("builtins.input", feed)

        main.run_force_full_scan(cast(TMDBClient, object()))

        assert feed.call_count == main.prompts.MAX_UNRECOGNIZED
        assert "Cancelled" in capsys.readouterr().out

    def test_the_prompt_says_gone_movies_are_asked_about_again(
        self, _index, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)

        main.run_force_full_scan(cast(TMDBClient, object()))

        assert "marked gone are asked about again" in capsys.readouterr().out
