"""Tests for src.gaps."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from src.gaps import find_gaps, load_gaps
from src.index import save_details, save_index
from src.ui.reports import _is_upcoming
from src.ui.reports import title_line as _title_line


class TestUpcoming:
    def test_future_date(self) -> None:
        assert _is_upcoming("2099-01-01") is True

    def test_past_date(self) -> None:
        assert _is_upcoming("2000-01-01") is False

    def test_invalid_date(self) -> None:
        assert _is_upcoming("not-a-date") is False


class TestTitleLine:
    def test_title_with_year(self) -> None:
        assert _title_line({"title": "Inception", "release_date": "2010-07-16"}) == "Inception (2010)"

    def test_title_upcoming(self) -> None:
        line = _title_line({"title": "Future Film", "release_date": "2099-07-16"}, show_upcoming=True)
        assert "Future Film" in line
        assert "(upcoming)" in line


class TestFindGaps:
    def test_finds_missing_collection_parts(self, tmp_project) -> None:
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
                                {"id": 2, "title": "Missing", "release_date": "2021-01-01"},
                            ],
                        },
                        "keywords": [],
                    }
                }
            }
        )
        gaps = find_gaps()
        assert len(gaps["missing_films"]) == 1
        assert gaps["missing_films"][0]["id"] == 2
        assert gaps["indexed_count"] == 1

    def test_keyword_tv_qualifies(self, tmp_project) -> None:
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "A", "collection": {"id": 5, "name": "Shared Name"}},
                    "2": {"id": 2, "title": "B"},
                },
            }
        )
        save_details(
            {
                "movies": {
                    "1": {"id": 1, "keywords": ["Shared Name"]},
                    "2": {"id": 2, "keywords": ["Shared Name"]},
                }
            }
        )
        gaps = find_gaps()
        assert gaps["indexed_count"] == 2

    def test_keyword_tv_indexed_not_duplicated(self, tmp_project) -> None:
        """TV ids already in the index must not be reported again."""
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "A"},
                    "100": {"id": 100, "title": "Already Here"},
                },
            }
        )
        save_details(
            {
                "movies": {
                    "1": {
                        "id": 1,
                        "keywords": ["Shared Name"],
                        "connected_tv": [
                            {
                                "id": 100,
                                "name": "Already Here",
                                "first_air_date": "2020-01-01",
                                "via_keyword": "Shared Name",
                            }
                        ],
                    },
                }
            }
        )
        gaps = find_gaps()
        assert len(gaps["connected_tv"]) == 0

    def test_missing_films_deduped(self, tmp_project) -> None:
        """A collection part referenced by multiple indexed movies is reported once."""
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "One"},
                    "2": {"id": 2, "title": "Two"},
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
                                {"id": 1, "title": "One", "release_date": "2020-01-01"},
                                {"id": 3, "title": "Missing", "release_date": "2021-01-01"},
                            ],
                        },
                        "keywords": [],
                    },
                    "2": {
                        "id": 2,
                        "collection": {
                            "id": 10,
                            "name": "Franchise",
                            "parts": [
                                {"id": 2, "title": "Two", "release_date": "2020-02-01"},
                                {"id": 3, "title": "Missing", "release_date": "2021-01-01"},
                            ],
                        },
                        "keywords": [],
                    },
                }
            }
        )
        gaps = find_gaps()
        assert len(gaps["missing_films"]) == 1
        assert gaps["missing_films"][0]["id"] == 3

    def test_persists_gaps_file(self, tmp_project) -> None:
        """find_gaps() should write a JSON report to GAPS_FILE."""
        import config.config as _config
        from src.gaps import load_gaps

        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "A"},
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
                                {"id": 1, "title": "A", "release_date": "2020-01-01"},
                                {"id": 2, "title": "Missing", "release_date": "2021-01-01"},
                            ],
                        },
                        "keywords": [],
                    }
                }
            }
        )
        gaps = find_gaps()
        gaps_file = _config.GAPS_FILE
        assert gaps_file.exists()
        loaded = load_gaps()
        assert loaded["indexed_count"] == gaps["indexed_count"]
        assert len(loaded["missing_films"]) == 1


class TestGapBenchmarks:
    """Lightweight in-tree benchmarks for speed and detection accuracy.

    These tests use plain pytest + the standard library so they run without
    installing extra packages. They assert both a runtime ceiling and the
    precision/recall of the two gap detectors against a known fixture.
    """

    @pytest.fixture
    def known_fixture(self, tmp_project) -> dict:
        """Build a deterministic index/details fixture with expected gaps."""
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "10": {"id": 10, "title": "Alpha"},
                    "20": {"id": 20, "title": "Beta"},
                    "30": {"id": 30, "title": "Gamma"},
                },
            }
        )
        save_details(
            {
                "movies": {
                    "10": {
                        "id": 10,
                        "collection": {
                            "id": 1,
                            "name": "Saga One",
                            "parts": [
                                {"id": 10, "title": "Alpha", "release_date": "2018-01-01"},
                                {"id": 11, "title": "Alpha Two", "release_date": "2019-01-01"},
                                {"id": 12, "title": "Alpha Three", "release_date": "2020-01-01"},
                            ],
                        },
                        "keywords": ["saga one"],
                        "connected_tv": [
                            {
                                "id": 100,
                                "name": "Alpha TV",
                                "first_air_date": "2021-01-01",
                                "via_keyword": "saga one",
                            }
                        ],
                    },
                    "20": {
                        "id": 20,
                        "collection": {
                            "id": 2,
                            "name": "Saga Two",
                            "parts": [
                                {"id": 20, "title": "Beta", "release_date": "2018-02-01"},
                                {"id": 21, "title": "Beta Two", "release_date": "2022-01-01"},
                            ],
                        },
                        "keywords": ["saga two"],
                        "connected_tv": [
                            {
                                "id": 101,
                                "name": "Beta TV",
                                "first_air_date": "2023-01-01",
                                "via_keyword": "saga two",
                            }
                        ],
                    },
                    "30": {
                        "id": 30,
                        "keywords": ["saga one", "saga two"],
                        # No collection; keywords get boosted by other movies.
                    },
                }
            }
        )
        return {
            "expected_missing_ids": {11, 12, 21},
            "expected_tv_ids": {100, 101},
            "indexed_count": 3,
        }

    def test_speed_under_ceiling(self, known_fixture) -> None:
        """find_gaps() should complete in well under 1 second for 3 movies."""
        start = time.perf_counter()
        find_gaps()
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"find_gaps took {elapsed:.3f}s, expected < 1.0s"

    def test_collection_detection_accuracy(self, known_fixture) -> None:
        """All expected missing collection parts are found and no extras."""
        gaps = find_gaps()
        missing_ids = {m["id"] for m in gaps["missing_films"]}
        assert missing_ids == known_fixture["expected_missing_ids"]
        assert gaps["indexed_count"] == known_fixture["indexed_count"]

    def test_connected_tv_detection_accuracy(self, known_fixture) -> None:
        """All expected connected TV series are found and no extras."""
        gaps = find_gaps()
        tv_ids = {t["id"] for t in gaps["connected_tv"]}
        assert tv_ids == known_fixture["expected_tv_ids"]

    def test_precision_no_false_positives(self, known_fixture) -> None:
        """Indexed ids must never appear as gaps."""
        gaps = find_gaps()
        indexed_ids = {10, 20, 30}
        missing_ids = {m["id"] for m in gaps["missing_films"]}
        tv_ids = {t["id"] for t in gaps["connected_tv"]}
        assert not (missing_ids & indexed_ids)
        assert not (tv_ids & indexed_ids)


class TestLoadGapsFreshness:
    """Coverage for load_gaps() and the freshness heuristic in main.py."""

    def test_load_gaps_returns_persisted_report(self, tmp_project) -> None:
        """load_gaps() should read back a report written by find_gaps()."""
        save_index(
            {
                "list_id": 8678795,
                "movies": {
                    "1": {"id": 1, "title": "A"},
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
                                {"id": 1, "title": "A", "release_date": "2020-01-01"},
                                {"id": 2, "title": "Missing", "release_date": "2021-01-01"},
                            ],
                        },
                        "keywords": [],
                    }
                }
            }
        )
        find_gaps()
        loaded = load_gaps()
        assert loaded["indexed_count"] == 1
        assert len(loaded["missing_films"]) == 1

    def test_load_gaps_missing_file_returns_empty(self, tmp_project) -> None:
        """load_gaps() should return an empty dict when no report exists."""
        assert load_gaps() == {}

    def test_gaps_report_fresh_within_ceiling(self) -> None:
        """A report generated now is considered fresh."""
        from main import _gaps_report_fresh

        now = datetime.now(UTC).isoformat()
        assert _gaps_report_fresh(now) is True

    def test_gaps_report_stale_after_window(self) -> None:
        """A report older than the freshness window is stale."""
        from main import _gaps_report_fresh

        stale = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        assert _gaps_report_fresh(stale) is False

    def test_gaps_report_invalid_timestamp_is_stale(self) -> None:
        """Malformed timestamps are treated as not fresh."""
        from main import _gaps_report_fresh

        assert _gaps_report_fresh("not-a-date") is False
