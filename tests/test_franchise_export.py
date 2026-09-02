"""Tests for franchise gaps export to text files."""

import pytest

from src.gaps import find_gaps
from src.index import save_details, save_index


class TestFranchiseGapsExport:
    """Coverage for the append-only, deduplicated export in run_franchise_gaps."""

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

    def test_export_appends_missing_urls(self, gaps_fixture, tmp_path, monkeypatch):
        """Exporting missing films appends URLs to the target file."""
        from main import run_franchise_gaps

        target = tmp_path / "movie_urls.txt"

        monkeypatch.setattr("builtins.input", lambda prompt="": str(target))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)

        class FakeClient:
            session_id = None

        run_franchise_gaps(FakeClient())

        text = target.read_text(encoding="utf-8")
        assert "https://www.themoviedb.org/movie/2" in text
        assert "https://www.themoviedb.org/movie/3" in text
        assert text.count("# Franchise gaps export") == 1

    def test_export_does_not_overwrite_existing_file(self, gaps_fixture, tmp_path, monkeypatch):
        """Existing content in the target file is preserved."""
        from main import run_franchise_gaps

        target = tmp_path / "movie_urls.txt"
        target.write_text("# Existing\nhttps://www.themoviedb.org/movie/999\n", encoding="utf-8")

        monkeypatch.setattr("builtins.input", lambda prompt="": str(target))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)

        class FakeClient:
            session_id = None

        run_franchise_gaps(FakeClient())

        text = target.read_text(encoding="utf-8")
        assert "# Existing" in text
        assert "https://www.themoviedb.org/movie/999" in text
        assert "https://www.themoviedb.org/movie/2" in text

    def test_export_deduplicates_urls(self, gaps_fixture, tmp_path, monkeypatch):
        """URLs already present in the target file are not added again."""
        from main import run_franchise_gaps

        target = tmp_path / "movie_urls.txt"
        target.write_text(
            "https://www.themoviedb.org/movie/2\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("builtins.input", lambda prompt="": str(target))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)

        class FakeClient:
            session_id = None

        run_franchise_gaps(FakeClient())

        text = target.read_text(encoding="utf-8")
        assert text.count("https://www.themoviedb.org/movie/2") == 1
        assert "https://www.themoviedb.org/movie/3" in text

    def test_export_skips_when_user_declines(self, gaps_fixture, tmp_path, monkeypatch):
        """If the user declines the export prompt, the file is not created."""
        from main import run_franchise_gaps

        target = tmp_path / "movie_urls.txt"

        monkeypatch.setattr("builtins.input", lambda prompt="": str(target))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: False)

        class FakeClient:
            session_id = None

        run_franchise_gaps(FakeClient())

        assert not target.exists()
