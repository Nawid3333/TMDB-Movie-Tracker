"""The terminal layer: width maths, boxes and prompts.

This was the least-covered part of the project, and it is the
part the user actually reads and answers. A miscounted column pushes a box out
of alignment, and a wrong default on a confirm approves something the user
meant to decline. None of that is caught by testing the data layer.

Style note for future edits
---------------------------
Rendering tests assert on *content and invariants* -- "the box is rectangular",
"the title appears", "a typo is asked again" -- not on exact strings, so
restyling the output does not break the suite. The width maths is the one place
exact numbers matter, because everything else is built on it.
"""

from __future__ import annotations

import builtins
import contextlib
import io

from src.ui import term
from src.ui.prompts import confirm_category, paginate_list
from src.ui.reports import _is_upcoming, title_line


@contextlib.contextmanager
def captured():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


@contextlib.contextmanager
def answers(*scripted: str):
    """Feed input() from a script and record the prompts it was asked with.

    Yields the list of prompt strings. That matters because input() writes its
    prompt to stdout itself, so a mocked input means the prompt never lands in
    captured output -- asserting on what the user was actually asked has to go
    through this list.

    The script is finite: a prompt asked once more than scripted fails the
    test instead of being fed a fallback answer forever.
    """
    remaining = list(scripted)
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        if not remaining:
            raise AssertionError(f"asked again after the scripted answers ran out: {prompt!r}")
        return remaining.pop(0)

    real = builtins.input
    builtins.input = fake_input
    try:
        yield asked
    finally:
        builtins.input = real


# ── width maths ─────────────────────────────────────────────────────────────


class TestDisplayWidth:
    """Everything that lines up in a column depends on this being right."""

    def test_plain_ascii_is_its_length(self):
        assert term.display_width("hello") == 5

    def test_ansi_codes_take_no_columns(self):
        assert term.display_width(term.style("hello", term.Style.BOLD)) == 5

    def test_a_wide_cjk_character_takes_two_columns(self):
        assert term.display_width("漢") == 2

    def test_a_combining_mark_takes_none(self):
        assert term.display_width("é") == 1

    def test_an_emoji_presentation_selector_makes_a_narrow_glyph_wide(self):
        """U+FE0F asks for emoji presentation, which is two columns."""
        assert term.display_width("⚠️") == 2

    def test_an_empty_string_is_zero(self):
        assert term.display_width("") == 0


class TestStripAnsi:
    def test_removes_codes_and_keeps_text(self):
        assert term.strip_ansi(term.style("x", term.Style.BOLD, term.Style.CYAN)) == "x"

    def test_leaves_plain_text_alone(self):
        assert term.strip_ansi("plain") == "plain"


class TestStyle:
    def test_no_color_disables_styling(self, monkeypatch):
        """NO_COLOR is a standard; honouring it must survive refactors."""
        monkeypatch.setattr(term, "_COLOR", False)
        assert term.style("x", term.Style.BOLD) == "x"

    def test_styling_wraps_and_resets(self, monkeypatch):
        monkeypatch.setattr(term, "_COLOR", True)
        assert term.style("x", term.Style.BOLD).endswith(term.Style.RESET)


# ── boxes ──────────────────────────────────────────────────────


class TestBox:
    def test_a_box_is_rectangular(self):
        lines = term.box(["short", "a much longer line than the first"])
        widths = {term.display_width(line) for line in lines}
        assert len(widths) == 1, f"box edges are ragged: {widths}"

    def test_it_grows_to_fit_content_rather_than_truncating(self):
        long_line = "x" * 200
        rendered = term.box([long_line])
        assert long_line in "".join(rendered)

    def test_an_empty_box_still_renders(self):
        assert len(term.box([])) == 2, "top and bottom edges only"

    def test_ansi_styling_does_not_skew_the_edges(self):
        lines = term.box([term.style("coloured", term.Style.CYAN), "plain"])
        widths = {term.display_width(line) for line in lines}
        assert len(widths) == 1


class TestHr:
    def test_it_is_a_single_run_of_one_character(self):
        rule = term.hr(30)
        assert set(rule) == {"─"} and len(rule) == 30

    def test_a_silly_width_is_clamped(self):
        assert len(term.hr(1)) >= 20


# ── reports ─────────────────────────────────────────────────────────────────


class TestIsUpcoming:
    def test_a_far_future_date_is_upcoming(self):
        assert _is_upcoming("2099-01-01") is True

    def test_a_past_date_is_not(self):
        assert _is_upcoming("1999-01-01") is False

    def test_an_empty_or_malformed_date_is_not_upcoming(self):
        """Unknown must never read as upcoming, or gaps fill with noise."""
        for value in ("", "not-a-date", "2020-13-45"):
            assert _is_upcoming(value) is False


class TestTitleLine:
    def test_it_shows_the_title_and_year(self):
        line = title_line({"title": "Inception", "release_date": "2010-07-16"})
        assert "Inception" in line and "2010" in line

    def test_a_missing_release_date_still_renders(self):
        assert "Inception" in title_line({"title": "Inception"})

    def test_upcoming_can_be_flagged(self):
        plain = title_line({"title": "Later", "release_date": "2099-01-01"})
        flagged = title_line({"title": "Later", "release_date": "2099-01-01"}, show_upcoming=True)
        assert len(flagged) >= len(plain)


# ── prompts ─────────────────────────────────────────────────────────────────
# confirm() and ask() themselves -- strict answers, re-asking, the safe answer
# at end of input -- are covered in tests/test_prompt_retry.py.


class TestConfirmCategory:
    def test_an_empty_category_still_asks(self):
        """It does not short-circuit -- main.py guards with `if change_set.removals`.

        Pinned so that guard cannot be dropped without a test noticing: without
        it, an empty category would put a bare "Approve these changes?" in front
        of the user with nothing listed above it.
        """
        with answers("n") as asked, captured():
            assert confirm_category("Removals", []) is False
        assert asked, "an empty category currently still prompts"

    def test_enter_does_not_approve_a_category(self):
        """It used to: the Additions prompt read "[Y/n]" and Enter approved them."""
        with answers("", "n") as asked, captured():
            assert confirm_category("Additions", ["Movie A"]) is False
        assert len(asked) == 2

    def test_the_items_are_shown_before_the_question(self):
        with answers("y"), captured() as out:
            confirm_category("Removals", ["Movie A", "Movie B"])
        printed = out.getvalue()
        assert "Movie A" in printed and "Movie B" in printed


class TestPaginateList:
    def test_a_short_list_prints_in_full_without_prompting(self):
        with captured() as out:
            paginate_list([f"item {n}" for n in range(5)])
        assert "item 4" in out.getvalue()

    def test_a_long_list_can_be_skipped(self):
        with answers("q"), captured() as out:
            paginate_list([f"item {n}" for n in range(500)], page_size=10)
        assert "item 499" not in out.getvalue()

    def test_enter_turns_the_page(self):
        with answers("", "q"), captured() as out:
            paginate_list([f"item {n}" for n in range(30)], page_size=10)
        printed = out.getvalue()
        assert "item 19" in printed
        assert "item 20" not in printed

    def test_a_typo_is_asked_again_instead_of_paging_on(self):
        with answers("x", "q") as asked, captured() as out:
            paginate_list([f"item {n}" for n in range(30)], page_size=10)
        assert len(asked) == 2
        assert "item 10" not in out.getvalue()

    def test_an_empty_list_prints_nothing(self):
        with captured() as out:
            paginate_list([])
        assert out.getvalue().strip() == ""
