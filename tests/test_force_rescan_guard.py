"""Force re-enrich must ask before spending the whole rate limit.

Menu option 5 ignores the freshness tiers and asks TMDB for every movie in
the index. On a large index that is a long run and a real slice of the rate
limit, and it sits one keystroke from the fast scan on the same menu -- so it
confirms first, and a refusal must not reach the enrichment call at all.
"""

from pathlib import Path

import pytest

import main


@pytest.fixture
def _index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A three-movie index, so the prompt has a count to report."""
    monkeypatch.setattr(main, "load_index", lambda: {"movies": {"1": {}, "2": {}, "3": {}}})


class TestForceRescanConfirms:
    def test_declining_does_not_start_a_scan(self, _index, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        started = []
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: started.append(k))
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)

        main.run_force_full_scan(object())

        assert started == []
        assert "Cancelled" in capsys.readouterr().out

    def test_accepting_forces_the_scan(self, _index, monkeypatch: pytest.MonkeyPatch) -> None:
        started = []
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: started.append(k))
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: True)

        main.run_force_full_scan(object())

        assert len(started) == 1
        assert started[0]["force"] is True

    def test_the_prompt_says_how_many_movies_it_will_refetch(
        self, _index, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: None)
        monkeypatch.setattr(main.prompts, "confirm", lambda *a, **k: False)

        main.run_force_full_scan(object())

        assert "all 3 movie(s)" in capsys.readouterr().out

    def test_the_default_answer_is_no(self, _index, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accidental Enter must not launch it."""
        seen = {}

        def _confirm(prompt, default=False):
            seen["default"] = default
            return default

        monkeypatch.setattr(main, "enrich_run_full_scan", lambda *a, **k: pytest.fail("should not run"))
        monkeypatch.setattr(main.prompts, "confirm", _confirm)

        main.run_force_full_scan(object())

        assert seen["default"] is False
