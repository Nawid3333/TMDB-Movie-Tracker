"""A mistyped answer is asked again; only the answers a prompt offers count.

The prompts here used to guess. confirm() took Enter as its default -- so
"Approve these changes? [Y/n]" approved the additions on a stray keypress --
and the vanished-movie prompts read every typo as some answer: "yy" at
"Delete all these vanished entries?" walked the movies one by one, and a typo
at "Choice (1/2/3)" skipped the movie without a word. "Push URL file" read
Enter as "use the default file".

prompts.confirm and prompts.ask now take only what the prompt shows -- y or n
in either case, or a listed option -- and ask again on anything else, saying
what is allowed. There are no defaults: Enter alone is never an answer (it
only turns a page in the pager, and says "done" after a browser approval).
End of input, or MAX_UNRECOGNIZED wrong answers in a row, gives the answer
that changes nothing, so an unattended run cannot loop forever.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import main
from src.tmdb_api import TMDBClient
from src.ui import prompts, term

REPO = Path(__file__).resolve().parent.parent


class _Script:
    """Hand out *answers* one per prompt and record every prompt shown.

    Running out is a failure, not an end of input: a prompt that asks more
    often than the test expects is exactly what these tests are here to see.
    """

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"asked again after the scripted answers ran out: {prompt!r}")
        return self.answers.pop(0)


def _feed(monkeypatch: pytest.MonkeyPatch, *answers: str) -> _Script:
    script = _Script(*answers)
    monkeypatch.setattr("builtins.input", script)
    return script


def _eof(prompt: str = "") -> str:
    raise EOFError


def _shown(capsys: pytest.CaptureFixture) -> str:
    return term.strip_ansi(capsys.readouterr().out)


class TestConfirm:
    def test_y_and_n_answer_in_either_case(self, monkeypatch, capsys):
        for answer, expected in (("y", True), ("Y", True), ("n", False), ("N", False)):
            script = _feed(monkeypatch, answer)
            assert prompts.confirm("Go?") is expected
            assert len(script.asked) == 1

    def test_the_prompt_shows_what_it_accepts(self, monkeypatch, capsys):
        script = _feed(monkeypatch, "n")
        prompts.confirm("Go?")
        assert script.asked == ["Go? (y/n): "]

    def test_a_typo_is_asked_again_and_the_next_answer_counts(self, monkeypatch, capsys):
        script = _feed(monkeypatch, "sy", "y")
        assert prompts.confirm("Go?") is True
        assert len(script.asked) == 2
        assert "'sy' is not an option - type y or n." in _shown(capsys)

    def test_nothing_but_y_or_n_is_accepted(self, monkeypatch, capsys):
        for wrong in ("yes", "no", "j", "ja", "nein", "yn", "1", "0", "q"):
            for then, expected in (("y", True), ("n", False)):
                script = _feed(monkeypatch, wrong, then)
                assert prompts.confirm("Go?") is expected, (wrong, then)
                assert len(script.asked) == 2, f"{wrong!r} was taken instead of asked again"

    def test_enter_alone_is_not_an_answer(self, monkeypatch, capsys):
        for blank in ("", "   "):
            script = _feed(monkeypatch, blank, "n")
            assert prompts.confirm("Go?") is False
            assert len(script.asked) == 2
        assert "No answer - type y or n." in _shown(capsys)

    def test_a_right_answer_after_several_wrong_ones_still_counts(self, monkeypatch, capsys):
        script = _feed(monkeypatch, *["x"] * (prompts.MAX_UNRECOGNIZED - 1), "y")
        assert prompts.confirm("Go?") is True
        assert len(script.asked) == prompts.MAX_UNRECOGNIZED

    def test_end_of_input_answers_no(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", _eof)
        assert prompts.confirm("Go?") is False

    def test_endless_wrong_answers_stop_and_answer_no(self, monkeypatch, capsys):
        # A finite feed: one answer more than the prompt may ask for would
        # fail the test (_Script raises) instead of looping forever.
        script = _feed(monkeypatch, *["x"] * prompts.MAX_UNRECOGNIZED)
        assert prompts.confirm("Go?") is False
        assert len(script.asked) == prompts.MAX_UNRECOGNIZED
        assert f"No usable answer after {prompts.MAX_UNRECOGNIZED} tries" in _shown(capsys)


class TestAsk:
    def test_a_listed_option_is_returned(self, monkeypatch, capsys):
        for answer in ("1", "2", "3"):
            _feed(monkeypatch, answer)
            assert prompts.ask("Choice (1/2/3): ", ("1", "2", "3"), safe="3") == answer

    def test_an_unlisted_answer_is_asked_again(self, monkeypatch, capsys):
        for wrong in ("4", "12", "x", "-1", "01"):
            script = _feed(monkeypatch, wrong, "1")
            assert prompts.ask("? ", ("1", "2", "3"), safe="3") == "1"
            assert len(script.asked) == 2
            assert f"{wrong!r} is not an option - type one of 1, 2, 3." in _shown(capsys)

    def test_enter_is_never_an_answer(self, monkeypatch, capsys):
        script = _feed(monkeypatch, "", "2")
        assert prompts.ask("? ", ("1", "2", "3"), safe="3") == "2"
        assert len(script.asked) == 2
        assert "No answer - type one of 1, 2, 3." in _shown(capsys)

    def test_letters_match_in_either_case(self, monkeypatch, capsys):
        _feed(monkeypatch, "O")
        assert prompts.ask("? ", ("y", "n", "o"), safe="n") == "o"

    def test_end_of_input_gives_the_safe_answer(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", _eof)
        assert prompts.ask("? ", ("1", "2", "3"), safe="3") == "3"

    def test_endless_wrong_answers_give_the_safe_answer(self, monkeypatch, capsys):
        script = _feed(monkeypatch, *["x"] * prompts.MAX_UNRECOGNIZED)
        assert prompts.ask("? ", ("1", "2", "3"), safe="3") == "3"
        assert len(script.asked) == prompts.MAX_UNRECOGNIZED


class TestWaitForEnter:
    """The one place Enter is the answer: "I am done approving in the browser"."""

    def test_enter_continues(self, monkeypatch, capsys):
        _feed(monkeypatch, "")
        assert prompts.wait_for_enter("Press Enter when done...") is True

    def test_typed_text_is_asked_again(self, monkeypatch, capsys):
        script = _feed(monkeypatch, "y", "")
        assert prompts.wait_for_enter("Press Enter when done...") is True
        assert len(script.asked) == 2

    def test_end_of_input_does_not_continue(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", _eof)
        assert prompts.wait_for_enter("Press Enter when done...") is False

    def test_end_of_input_at_the_browser_approval_creates_no_session(self, client, monkeypatch, capsys):
        # It used to raise out of ensure_session() and end the program at startup.
        converted: list[str] = []
        monkeypatch.setattr(client, "_create_request_token", lambda: "req-token")
        monkeypatch.setattr(client, "_create_session_from_token", lambda token: converted.append(token) or "sess")
        monkeypatch.setattr("webbrowser.open", lambda url: True)
        monkeypatch.setattr("builtins.input", _eof)
        assert client._browser_approval_flow() is None
        assert converted == []

    def test_end_of_input_at_the_v4_approval_exchanges_nothing(self, client, monkeypatch, capsys):
        exchanged: list[str] = []
        monkeypatch.setattr("config.config.TMDB_API_READ_ACCESS_TOKEN", "read-token")
        monkeypatch.setattr(client, "_create_v4_request_token", lambda: "req-token")
        monkeypatch.setattr(client, "_exchange_v4_access_token", lambda token: exchanged.append(token) or "v4")
        monkeypatch.setattr("webbrowser.open", lambda url: True)
        monkeypatch.setattr("builtins.input", _eof)
        assert client.acquire_v4_access_token() is None
        assert exchanged == []


class TestBatchSourcePrompt:
    """Option 4: 1 is the default file now, as in the scrapers, and a mistake is asked again."""

    @pytest.fixture
    def batch_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "movie_urls.txt"
        path.write_text("550\n", encoding="utf-8")
        monkeypatch.setattr(main, "DEFAULT_BATCH_FILE", path)
        return path

    def test_1_uses_the_default_file(self, batch_file, monkeypatch, capsys):
        _feed(monkeypatch, "1")
        assert main._select_batch_source() == ("file", str(batch_file))

    def test_the_prompt_says_what_1_means(self, batch_file, monkeypatch, capsys):
        """So nobody wonders why a bare 1 is not TMDB id 1."""
        script = _feed(monkeypatch, "0")
        main._select_batch_source()
        assert "1 = movie_urls.txt, 0 = back" in script.asked[0]

    def test_every_other_number_is_still_a_tmdb_id(self, batch_file, monkeypatch, capsys):
        _feed(monkeypatch, "603")
        assert main._select_batch_source() == ("single", "603")

    def test_1_without_a_default_file_is_asked_again_not_read_as_id_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(main, "DEFAULT_BATCH_FILE", tmp_path / "missing.txt")
        script = _feed(monkeypatch, "1", "0")
        assert main._select_batch_source() is None
        assert len(script.asked) == 2
        assert "The default file does not exist" in _shown(capsys)

    def test_enter_is_asked_again_rather_than_meaning_the_default_file(self, batch_file, monkeypatch, capsys):
        script = _feed(monkeypatch, "", "1")
        assert main._select_batch_source() == ("file", str(batch_file))
        assert len(script.asked) == 2
        assert "No answer" in _shown(capsys)

    def test_a_named_file_is_used(self, batch_file, monkeypatch, capsys):
        _feed(monkeypatch, str(batch_file))
        assert main._select_batch_source() == ("file", str(batch_file))

    def test_a_movie_url_or_id_is_a_single_movie(self, batch_file, monkeypatch, capsys):
        url = "https://www.themoviedb.org/movie/550-fight-club"
        _feed(monkeypatch, url)
        assert main._select_batch_source() == ("single", url)

    def test_a_missing_file_is_asked_again(self, batch_file, monkeypatch, capsys):
        script = _feed(monkeypatch, "no-such-file.txt", "1")
        assert main._select_batch_source() == ("file", str(batch_file))
        assert len(script.asked) == 2
        assert "No such file, and not a TMDB/IMDb URL or id: no-such-file.txt" in _shown(capsys)

    def test_a_directory_is_asked_again(self, batch_file, monkeypatch, capsys):
        script = _feed(monkeypatch, str(batch_file.parent), "1")
        assert main._select_batch_source() == ("file", str(batch_file))
        assert len(script.asked) == 2
        assert "That is a directory" in _shown(capsys)

    def test_0_goes_back(self, batch_file, monkeypatch, capsys):
        _feed(monkeypatch, "0")
        assert main._select_batch_source() is None

    def test_end_of_input_goes_back(self, batch_file, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", _eof)
        assert main._select_batch_source() is None

    def test_endless_mistakes_go_back_to_the_menu(self, batch_file, monkeypatch, capsys):
        script = _feed(monkeypatch, *["nope.txt"] * prompts.MAX_UNRECOGNIZED)
        assert main._select_batch_source() is None
        assert len(script.asked) == prompts.MAX_UNRECOGNIZED


class TestMainMenu:
    """The menu asks again on a typo, and end of input exits instead of looping."""

    @pytest.fixture
    def menu(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        ran: list[str] = []

        class _Client(SimpleNamespace):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(main, "bootstrap", lambda: None)
        monkeypatch.setattr(main, "log", main.log)  # main() rebinds it; put it back afterwards
        monkeypatch.setattr(main, "setup_logging", lambda: logging.getLogger("test_menu"))
        monkeypatch.setattr(main, "check_api_key", lambda: True)
        monkeypatch.setattr(main, "load_index", lambda: {"movies": {}})
        monkeypatch.setattr(main, "TMDBClient", lambda: _Client(session_id=None, ensure_session=lambda: None))
        monkeypatch.setattr(main, "_probe_tmdb_status", lambda *a: None)
        monkeypatch.setattr(main, "run_fast_scan", lambda client: ran.append("2"))
        return ran

    def test_a_typo_is_asked_again(self, menu, monkeypatch, capsys):
        script = _feed(monkeypatch, "7", "2", "0")
        main.main()
        assert menu == ["2"]
        assert len(script.asked) == 3
        assert "'7' is not an option" in _shown(capsys)

    def test_end_of_input_exits(self, menu, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", _eof)
        main.main()
        assert menu == []
        assert "Goodbye" in _shown(capsys)


# ── source guards ───────────────────────────────────────────────────────────

_SOURCES = [REPO / "main.py", *sorted((REPO / "src").rglob("*.py"))]
# The prompt helpers themselves, and the one wrapper around input() they use.
_PROMPT_MODULES = {REPO / "src" / "ui" / "prompts.py", REPO / "src" / "ui" / "term.py"}

# How a prompt used to offer an answer for Enter.
_DEFAULT_MARKERS = ("[Y/n]", "[y/N]", "[default", "Enter =", "Enter →", "Enter ->", "Press Enter →")
# The pager's "Enter = more": Enter turns a page there, it never answers a question.
_PAGER_HINTS = ("Enter = more",)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Return the ids of every docstring node; a docstring may describe a prompt."""
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            ids.add(id(body[0].value))
    return ids


def _strings(path: Path):
    """Yield (node, text) for every string constant in *path* except docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in exempt:
            yield node, node.value


def _direct_input_calls(path: Path) -> list[str]:
    """Return "file:line" for each call to input()/cinput() -- only the prompt helpers may read input."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in ("input", "cinput"):
            lines.append(node.lineno)
    return [f"{path.name}:{line}" for line in sorted(lines)]


def _yn_prompts(path: Path) -> list[str]:
    """Return "file:line" for each "(y/n)"-style string: confirm() adds that itself."""
    return [
        f"{path.name}:{node.lineno}"
        for node, text in _strings(path)
        if "(y/n)" in text.lower() or "[y/n]" in text.lower()
    ]


def _offered_defaults(path: Path) -> list[str]:
    """Return "file:line" for each string that offers Enter an answer."""
    return [
        f"{path.name}:{node.lineno}"
        for node, text in _strings(path)
        if any(marker in text for marker in _DEFAULT_MARKERS) and not any(hint in text for hint in _PAGER_HINTS)
    ]


class TestSourceGuards:
    """Neither rule can quietly come back in a later change."""

    def test_only_the_prompt_helpers_read_input(self):
        offenders = [hit for path in _SOURCES if path not in _PROMPT_MODULES for hit in _direct_input_calls(path)]
        assert offenders == [], "a prompt reads input directly; use prompts.confirm / prompts.ask"

    def test_no_y_n_prompt_is_built_outside_confirm(self):
        offenders = [hit for path in _SOURCES if path not in _PROMPT_MODULES for hit in _yn_prompts(path)]
        assert offenders == [], "a y/n prompt is spelled out by hand; use prompts.confirm"

    def test_no_prompt_offers_a_default(self):
        offenders = [hit for path in _SOURCES for hit in _offered_defaults(path)]
        assert offenders == [], "a prompt offers Enter an answer; make every answer typed"

    def test_the_guards_see_what_they_are_meant_to(self, tmp_path: Path):
        planted = tmp_path / "planted.py"
        planted.write_text(
            'def f():\n    """Asks (y/n) [Y/n] in a docstring - allowed."""\n'
            '    return input("Delete all? (y/n) [Y/n]: ") == "y"\n'
            'def g():\n    return cinput("  (1/3) Enter = more, q = skip: ")\n',
            encoding="utf-8",
        )
        assert _direct_input_calls(planted) == ["planted.py:3", "planted.py:5"]
        assert _yn_prompts(planted) == ["planted.py:3"]
        assert _offered_defaults(planted) == ["planted.py:3"]


def test_the_vanished_prompt_offers_one_by_one_as_a_typed_answer(monkeypatch, capsys):
    """o, not Enter, walks the movies -- and the prompt says so."""
    script = _feed(monkeypatch, "n")
    monkeypatch.setattr(main._config, "TMDB_LIST_ID", "1")
    client = cast(TMDBClient, SimpleNamespace(session_id="s"))
    index = {"movies": {"2": {"id": 2, "title": "Gone"}}}
    main._prompt_clean_vanished(client, [], index)
    assert "o = decide one by one" in term.strip_ansi(script.asked[0])
    assert "Enter" not in script.asked[0]
