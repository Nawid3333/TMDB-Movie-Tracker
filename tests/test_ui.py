"""The terminal layer: width maths, wrapping, boxes, prompts, and cards.

This was the least-covered part of the project (cards.py at 0%) and it is the
part the user actually reads and answers. A miscounted column pushes a box out
of alignment, a wrong default on a confirm approves something the user meant to
decline, and a card that raises takes down the browse loop. None of that is
caught by testing the data layer.

Style note for future edits
---------------------------
Rendering tests assert on *content and invariants* -- "the box is rectangular",
"the title appears", "the default is used" -- not on exact strings, so
restyling the output does not break the suite. The width maths is the one place
exact numbers matter, because everything else is built on it.
"""

from __future__ import annotations

import builtins
import contextlib
import io

from src.ui import term
from src.ui.cards import render_detail_card
from src.ui.prompts import ask_choice, confirm, confirm_category, paginate_list
from src.ui.reports import _is_upcoming, title_line


@contextlib.contextmanager
def captured():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


@contextlib.contextmanager
def answers(*scripted: str, default: str = ""):
    """Feed input() from a script and record the prompts it was asked with.

    Yields the list of prompt strings. That matters because input() writes its
    prompt to stdout itself, so a mocked input means the prompt never lands in
    captured output -- asserting on what the user was actually asked has to go
    through this list.
    """
    remaining = list(scripted)
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        return remaining.pop(0) if remaining else default

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
        assert term.display_width(term.style("hello", term._T.BOLD)) == 5

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
        assert term.strip_ansi(term.style("x", term._T.BOLD, term._T.CYAN)) == "x"

    def test_leaves_plain_text_alone(self):
        assert term.strip_ansi("plain") == "plain"


class TestStyle:
    def test_no_color_disables_styling(self, monkeypatch):
        """NO_COLOR is a standard; honouring it must survive refactors."""
        monkeypatch.setattr(term, "_COLOR", False)
        assert term.style("x", term._T.BOLD) == "x"

    def test_styling_wraps_and_resets(self, monkeypatch):
        monkeypatch.setattr(term, "_COLOR", True)
        assert term.style("x", term._T.BOLD).endswith(term._T.RESET)


# ── wrapping and boxes ──────────────────────────────────────────────────────


class TestWrap:
    def test_short_text_is_one_line(self):
        assert term.wrap("short", width=40) == ["short"]

    def test_no_line_exceeds_the_width(self):
        text = "the quick brown fox jumps over the lazy dog " * 4
        assert all(term.display_width(line) <= 30 for line in term.wrap(text, width=30))

    def test_existing_line_breaks_are_preserved(self):
        assert term.wrap("one\ntwo", width=40) == ["one", "two"]

    def test_a_word_longer_than_the_width_is_cut_rather_than_looping(self):
        """The cut-back loop can reach 0; it must fall back, not spin."""
        lines = term.wrap("x" * 100, width=20)
        assert lines and all(term.display_width(line) <= 20 for line in lines)

    def test_wrapping_never_loses_words(self):
        text = "alpha beta gamma delta epsilon zeta eta theta"
        assert " ".join(term.wrap(text, width=12)).split() == text.split()


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
        lines = term.box([term.style("coloured", term._T.CYAN), "plain"])
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


class TestConfirm:
    def test_yes_is_accepted(self):
        with answers("y"), captured():
            assert confirm("go?") is True

    def test_no_is_accepted(self):
        with answers("n"), captured():
            assert confirm("go?", default=True) is False

    def test_empty_input_takes_the_default(self):
        with answers(""), captured():
            assert confirm("go?", default=True) is True
        with answers(""), captured():
            assert confirm("go?", default=False) is False

    def test_the_default_is_shown_so_it_is_never_a_surprise(self):
        """The capitalised letter in [Y/n] is which key Enter presses."""
        with answers("") as asked, captured():
            confirm("go?", default=True)
        assert "[Y/n]" in asked[0]
        with answers("") as asked, captured():
            confirm("go?", default=False)
        assert "[y/N]" in asked[0]

    def test_an_unrecognised_answer_is_re_asked_rather_than_guessed(self):
        with answers("maybe", "y") as asked, captured():
            assert confirm("go?") is True
        assert len(asked) == 2


class TestAskChoice:
    def test_a_listed_option_is_returned(self):
        with answers("b"), captured():
            assert ask_choice("pick", ["a", "b"], default="a") == "b"

    def test_empty_input_takes_the_default(self):
        with answers(""), captured():
            assert ask_choice("pick", ["a", "b"], default="a") == "a"

    def test_an_unlisted_answer_is_re_asked_rather_than_guessed(self):
        with answers("zzz", "a"), captured():
            assert ask_choice("pick", ["a", "b"], default="a") == "a"


class TestConfirmCategory:
    def test_an_empty_category_still_asks_and_honours_the_default(self):
        """It does not short-circuit -- main.py guards with `if change_set.removals`.

        Pinned so that guard cannot be dropped without a test noticing: without
        it, an empty category would put a bare "Approve these changes?" in front
        of the user with nothing listed above it.
        """
        with answers("") as asked, captured():
            assert confirm_category("Removals", [], default=False) is False
        assert asked, "an empty category currently still prompts"

    def test_the_items_are_shown_before_the_question(self):
        with answers("y"), captured() as out:
            confirm_category("Removals", ["Movie A", "Movie B"], default=False)
        printed = out.getvalue()
        assert "Movie A" in printed and "Movie B" in printed


class TestPaginateList:
    def test_a_short_list_prints_in_full_without_prompting(self):
        with captured() as out:
            paginate_list([f"item {n}" for n in range(5)])
        assert "item 4" in out.getvalue()

    def test_a_long_list_can_be_skipped(self):
        with answers("q", default="q"), captured() as out:
            paginate_list([f"item {n}" for n in range(500)], page_size=10)
        assert "item 499" not in out.getvalue()

    def test_an_empty_list_prints_nothing(self):
        with captured() as out:
            paginate_list([])
        assert out.getvalue().strip() == ""


# ── detail card ─────────────────────────────────────────────────────────────


class TestRenderDetailCard:
    """cards.py had no coverage at all; it is pure rendering over sparse dicts."""

    def test_a_full_record_renders_every_section(self):
        membership = {"id": 27205, "title": "Inception", "release_date": "2010-07-16", "status": "Released"}
        detail = {
            "tagline": "Your mind is the scene of the crime.",
            "runtime": 148,
            "genres": ["Action", "Science Fiction"],
            "directors": ["Christopher Nolan"],
            "cast": [{"name": "Leonardo DiCaprio", "character": "Cobb"}],
            "collection": {"id": 1, "name": "Nolan Collection"},
            "connected_tv": [{"name": "Some Show", "via_keyword": "dream"}],
            "overview": "A thief who steals corporate secrets.",
        }
        with captured() as out:
            render_detail_card(membership, detail)
        printed = out.getvalue()
        for expected in ("Inception", "148", "Action", "Christopher Nolan", "Leonardo DiCaprio", "Nolan Collection"):
            assert expected in printed, f"{expected!r} missing from the card"
        assert "themoviedb.org/movie/27205" in printed

    def test_an_almost_empty_record_still_renders(self):
        """Enrichment is optional, so a bare membership record must not crash."""
        with captured() as out:
            render_detail_card({"id": 1, "title": "Bare"}, {})
        assert "Bare" in out.getvalue()

    def test_a_single_director_is_labelled_in_the_singular(self):
        with captured() as out:
            render_detail_card({"id": 1, "title": "X"}, {"directors": ["Solo"]})
        assert "Director:" in out.getvalue()

    def test_several_directors_are_labelled_in_the_plural(self):
        with captured() as out:
            render_detail_card({"id": 1, "title": "X"}, {"directors": ["A", "B"]})
        assert "Directors:" in out.getvalue()

    def test_cast_is_capped_so_one_movie_cannot_fill_the_screen(self):
        cast = [{"name": f"Actor {n}", "character": f"Role {n}"} for n in range(50)]
        with captured() as out:
            render_detail_card({"id": 1, "title": "X"}, {"cast": cast})
        assert "Actor 40" not in out.getvalue()

    def test_a_cast_member_without_a_character_still_appears(self):
        with captured() as out:
            render_detail_card({"id": 1, "title": "X"}, {"cast": [{"name": "Nameless Role"}]})
        assert "Nameless Role" in out.getvalue()

    def test_the_collection_falls_back_to_the_membership_record(self):
        with captured() as out:
            render_detail_card({"id": 1, "title": "X", "collection": {"name": "From Membership"}}, {})
        assert "From Membership" in out.getvalue()
