import httpx
import pytest

from src.gaps import find_gaps
from src.index import save_details, save_index


class _FakeClient:
    session_id: str | None = None

    def get(self, path: str, *, params: dict | None = None, auth: bool = True) -> httpx.Response:
        raise NotImplementedError

    def ensure_session(self) -> str | None:
        return None


class TestFranchiseGapsExport:
    """Coverage for the always-fresh, two-section export in run_franchise_gaps."""

    @pytest.fixture
    def gaps_fixture(self, tmp_project):
        """Create a deterministic gaps report with two missing collection films."""
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "Parent"},
                },
            }
        )
        save_details(
            {
                "movies": {
                    "1": {
                        "id": 1,
                        "collection": {
                            "id": 10,
                            "name": "Franchise",
                            "parts": [
                                {"id": 1, "title": "Parent", "release_date": "2020-01-01"},
                                {"id": 2, "title": "Missing Two", "release_date": "2021-01-01"},
                                {"id": 3, "title": "Missing Three", "release_date": "2022-01-01"},
                            ],
                        },
                        "keywords": [],
                    }
                }
            }
        )
        return find_gaps(persist=False)

    def test_export_written_without_prompts(self, gaps_fixture, tmp_project):
        """The export file is written on every run without asking."""
        import config.config as _config
        from main import run_franchise_gaps

        run_franchise_gaps(_FakeClient())

        text = _config.FRANCHISE_GAPS_EXPORT_FILE.read_text(encoding="utf-8")
        assert "https://www.themoviedb.org/movie/2" in text
        assert "https://www.themoviedb.org/movie/3" in text

    def test_export_separates_new_from_previously_shown(self, gaps_fixture, tmp_project):
        """First run puts both films under New; second run moves them down."""
        import config.config as _config
        from main import run_franchise_gaps

        target = _config.FRANCHISE_GAPS_EXPORT_FILE

        # First run: everything is new.
        run_franchise_gaps(_FakeClient())
        first = target.read_text(encoding="utf-8")
        assert "── New since last run (2) ──" in first
        assert "── Previously shown" not in first

        # Second run: same gaps, but now flagged as previously shown.
        run_franchise_gaps(_FakeClient())
        second = target.read_text(encoding="utf-8")
        assert "── New since last run" not in second
        assert "── Nothing new since last run ──" in second
        assert "── Previously shown (2) ──" in second
        assert second.index("── Previously shown") < second.index("movie/2")

    def test_export_rewritten_fresh_each_run(self, gaps_fixture, tmp_project):
        """Stale manual content disappears -- the file is rewritten, not appended."""
        import config.config as _config
        from main import run_franchise_gaps

        target = _config.FRANCHISE_GAPS_EXPORT_FILE
        target.write_text("# Stale header\nhttps://www.themoviedb.org/movie/999\n", encoding="utf-8")

        run_franchise_gaps(_FakeClient())

        text = target.read_text(encoding="utf-8")
        assert "# Stale header" not in text
        assert "https://www.themoviedb.org/movie/999" not in text
        assert text.startswith("# Franchise gaps export")
        assert "movie/2" in text

    def test_export_includes_titles_as_comments(self, gaps_fixture, tmp_project):
        """Each URL line carries its title so links can be read at a glance."""
        import config.config as _config
        from main import run_franchise_gaps

        run_franchise_gaps(_FakeClient())

        text = _config.FRANCHISE_GAPS_EXPORT_FILE.read_text(encoding="utf-8")
        assert "https://www.themoviedb.org/movie/2  # Missing Two (2021)" in text
        assert "https://www.themoviedb.org/movie/3  # Missing Three (2022)" in text

    def test_gap_table_titles_are_clickable_links_on_a_real_terminal(
        self, gaps_fixture, tmp_project, monkeypatch, capsys
    ):
        """The terminal table's title column links to the same URL as its Link column."""
        import main
        from main import run_franchise_gaps

        monkeypatch.setattr(main.term, "_COLOR", True)

        run_franchise_gaps(_FakeClient())

        printed = capsys.readouterr().out
        url = "https://www.themoviedb.org/movie/2"
        assert f"\x1b]8;;{url}\x1b\\Missing Two (2021)\x1b]8;;\x1b\\" in printed
        # The plain-text Link column stays alongside it as a fallback.
        assert url in printed

    def test_indexed_films_leave_the_report(self, gaps_fixture, tmp_project):
        """Adding a gap film to the index removes it from the next report."""
        import config.config as _config
        from main import run_franchise_gaps

        target = _config.FRANCHISE_GAPS_EXPORT_FILE
        run_franchise_gaps(_FakeClient())
        assert "movie/2" in target.read_text(encoding="utf-8")

        # User pushes film 2 via the batch flow -> it enters the index.
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "Parent"},
                    "2": {"id": 2, "title": "Missing Two"},
                },
            }
        )
        run_franchise_gaps(_FakeClient())

        text = target.read_text(encoding="utf-8")
        assert "movie/2" not in text
        assert "movie/3" in text
        # Film 3 was already shown in the first run, so nothing is new.
        assert "0 new, 1 previously shown" in text
