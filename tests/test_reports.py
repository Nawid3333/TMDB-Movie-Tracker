"""Tests for src.ui.reports.

The change report is the last thing a user reads before approving additions
and removals, so what it does and does not print is a correctness concern:
a removal that is proposed but not shown is a removal nobody agreed to.
"""

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta

import pytest

from src.changes import ChangeSet
from src.ui import term
from src.ui.reports import _is_upcoming, movie_url, render_change_report, title_line, title_link, title_link_rows


def _movie(movie_id: int, title: str, release: str = "1999-03-31") -> dict:
    return {"id": movie_id, "title": title, "release_date": release}


def _rendered(change_set: ChangeSet) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        render_change_report(change_set)
    return out.getvalue()


class TestIsUpcoming:
    def test_a_future_date_is_upcoming(self):
        future = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
        assert _is_upcoming(future) is True

    def test_a_past_date_is_not(self):
        assert _is_upcoming("1999-03-31") is False

    def test_today_is_not_upcoming(self):
        """The boundary is strict: released today counts as released."""
        assert _is_upcoming(datetime.now(UTC).date().isoformat()) is False

    @pytest.mark.parametrize("value", ["", "not-a-date", "1999", "31-03-1999", "1999-13-01"])
    def test_anything_unparseable_is_not_upcoming(self, value):
        """TMDB leaves release_date blank or partial often enough to matter."""
        assert _is_upcoming(value) is False


class TestTitleLine:
    def test_title_and_year(self):
        assert title_line(_movie(1, "The Matrix")) == "The Matrix (1999)"

    def test_a_tv_record_uses_name_and_first_air_date(self):
        record = {"name": "Severance", "first_air_date": "2022-02-18"}
        assert title_line(record) == "Severance (2022)"

    def test_a_record_with_no_title_is_labelled_not_blank(self):
        assert title_line({"id": 1}) == "(untitled)"

    def test_a_missing_release_date_drops_the_year_rather_than_guessing(self):
        assert title_line({"title": "Untitled Project"}) == "Untitled Project"

    def test_a_truncated_release_date_drops_the_year(self):
        assert title_line({"title": "Odd", "release_date": "199"}) == "Odd"

    def test_upcoming_is_only_marked_when_asked_for(self):
        future = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
        record = {"title": "Next One", "release_date": future}
        assert "(upcoming)" not in title_line(record)
        assert "(upcoming)" in title_line(record, show_upcoming=True)

    def test_a_released_film_is_never_marked_upcoming(self):
        assert "(upcoming)" not in title_line(_movie(1, "The Matrix"), show_upcoming=True)


class TestMovieUrl:
    def test_builds_the_tmdb_movie_page_url(self):
        assert movie_url(603) == "https://www.themoviedb.org/movie/603"


class TestTitleLink:
    def test_the_url_is_always_visible_plain_text_when_colour_is_off(self):
        """The default (non-tty) case: the URL is still shown, just not as a hyperlink."""
        out = title_link(_movie(603, "The Matrix"))
        assert out == "The Matrix (1999) — https://www.themoviedb.org/movie/603"

    def test_the_url_becomes_an_osc8_hyperlink_when_colour_is_on(self, monkeypatch):
        monkeypatch.setattr(term, "_COLOR", True)
        out = title_link(_movie(603, "The Matrix"))
        url = "https://www.themoviedb.org/movie/603"
        assert out == f"The Matrix (1999) — \x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"

    def test_falls_back_to_plain_title_when_there_is_no_id_to_link_to(self, monkeypatch):
        monkeypatch.setattr(term, "_COLOR", True)
        assert title_link({"title": "Untitled Project"}) == "Untitled Project"


class TestTitleLinkRows:
    def test_short_titles_are_padded_to_match_the_longest_in_the_batch(self):
        rows = title_link_rows(
            [
                _movie(1, "Cat"),
                _movie(2, "A Much Longer Title"),
            ]
        )
        dash_positions = [row.index("—") for row in rows]
        assert dash_positions[0] == dash_positions[1]

    def test_a_single_record_needs_no_padding(self):
        """A one-item batch is identical to the plain title_link output."""
        record = _movie(603, "The Matrix")
        assert title_link_rows([record]) == [title_link(record)]

    def test_a_record_with_no_id_is_left_unpadded_and_unlinked(self):
        rows = title_link_rows([{"title": "Untitled"}, _movie(2, "Longer Title")])
        assert rows[0] == "Untitled"

    def test_an_empty_batch_returns_an_empty_list(self):
        assert title_link_rows([]) == []


class TestRenderChangeReport:
    def test_the_counts_are_always_reported(self):
        out = _rendered(ChangeSet(current_count=10, proposed_count=12))
        assert "10" in out
        assert "12" in out

    def test_no_changes_says_so_rather_than_printing_nothing(self):
        assert "No changes to review." in _rendered(ChangeSet())

    def test_additions_are_listed_with_a_plus(self):
        cs = ChangeSet(additions={"603": _movie(603, "The Matrix")})
        out = _rendered(cs)
        assert "Additions: 1" in out
        assert "+ The Matrix (1999)" in out

    def test_additions_and_removals_are_clickable_links_on_a_real_terminal(self, monkeypatch):
        monkeypatch.setattr(term, "_COLOR", True)
        cs = ChangeSet(
            additions={"603": _movie(603, "The Matrix")},
            removals={"550": _movie(550, "Fight Club")},
        )
        out = _rendered(cs)
        url_603 = "https://www.themoviedb.org/movie/603"
        url_550 = "https://www.themoviedb.org/movie/550"
        assert f"The Matrix (1999) — \x1b]8;;{url_603}\x1b\\{url_603}\x1b]8;;\x1b\\" in out
        assert f"Fight Club (1999) — \x1b]8;;{url_550}\x1b\\{url_550}\x1b]8;;\x1b\\" in out

    def test_removals_are_listed_with_a_minus(self):
        cs = ChangeSet(removals={"550": _movie(550, "Fight Club")})
        out = _rendered(cs)
        assert "Removals: 1" in out
        assert "- Fight Club (1999)" in out

    def test_every_proposed_change_is_shown_not_a_sample(self):
        """Nothing may be silently truncated: this list is the consent prompt."""
        additions = {str(i): _movie(i, f"Add {i}") for i in range(25)}
        removals = {str(i): _movie(i, f"Drop {i}") for i in range(100, 118)}
        out = _rendered(ChangeSet(additions=additions, removals=removals))
        for i in range(25):
            assert f"+ Add {i} (1999)" in out
        for i in range(100, 118):
            assert f"- Drop {i} (1999)" in out

    def test_an_incomplete_scan_is_flagged_and_says_removals_are_blocked(self):
        out = _rendered(ChangeSet(incomplete=True))
        assert "incomplete" in out.lower()
        assert "blocked" in out.lower()

    def test_a_complete_scan_carries_no_warning(self):
        assert "blocked" not in _rendered(ChangeSet(additions={"1": _movie(1, "X")})).lower()

    def test_additions_and_removals_render_together(self):
        cs = ChangeSet(
            additions={"603": _movie(603, "The Matrix")},
            removals={"550": _movie(550, "Fight Club")},
            current_count=1,
            proposed_count=1,
        )
        out = _rendered(cs)
        assert "+ The Matrix (1999)" in out
        assert "- Fight Club (1999)" in out
        assert "No changes to review." not in out

    def test_unchanged_entries_are_not_listed(self):
        """Only what the user has to decide on belongs in the report."""
        cs = ChangeSet(unchanged={"550": _movie(550, "Fight Club")})
        out = _rendered(cs)
        assert "Fight Club" not in out
        assert "No changes to review." in out
