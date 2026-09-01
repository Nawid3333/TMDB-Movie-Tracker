"""Tests for src.index persistence and validation."""

import json
from pathlib import Path

import pytest

from src.index import (
    ensure_record_exists,
    load_details,
    load_index,
    now_iso,
    save_index,
    validate_membership_record,
    validate_movie_id,
)


class TestValidateMovieId:
    def test_accepts_int(self) -> None:
        assert validate_movie_id(42) == 42

    def test_accepts_numeric_string(self) -> None:
        assert validate_movie_id("42") == 42

    @pytest.mark.parametrize("value", [0, -1, "abc", None])
    def test_rejects_invalid(self, value) -> None:
        with pytest.raises(ValueError):
            validate_movie_id(value)


class TestValidateMembershipRecord:
    def test_valid_record(self) -> None:
        record = {"id": 1, "title": "Test", "release_date": "2020-01-01"}
        validate_membership_record(record)  # does not raise

    def test_missing_title(self) -> None:
        with pytest.raises(ValueError, match="no title"):
            validate_membership_record({"id": 1})

    def test_invalid_release_date_type(self) -> None:
        with pytest.raises(ValueError, match="release_date"):
            validate_membership_record({"id": 1, "title": "Test", "release_date": 2020})


class TestLoadSaveIndex:
    def test_save_and_load_roundtrip(self, tmp_project: Path) -> None:
        index = {
            "list_id": 8678795,
            "list_name": "Test List",
            "movies": {
                "42": {"id": 42, "title": "Answer"},
            },
        }
        save_index(index)
        loaded = load_index()
        assert loaded["list_id"] == 8678795
        assert loaded["movies"]["42"]["title"] == "Answer"

    def test_load_missing_returns_default(self, tmp_project: Path) -> None:
        loaded = load_index()
        assert loaded["schema_version"] == 1
        assert loaded["movies"] == {}

    def test_backup_restored_on_corruption(self, tmp_project: Path) -> None:
        index_file = tmp_project / "data" / "index.json"
        _ = tmp_project / "data" / "index.json.bak1"
        good = {"list_id": 1, "movies": {"1": {"id": 1, "title": "Good"}}}
        index_file.write_text(json.dumps(good), encoding="utf-8")
        save_index(good)
        index_file.write_text("not json", encoding="utf-8")
        loaded = load_index()
        assert loaded["movies"]["1"]["title"] == "Good"


class TestEnsureRecordExists:
    def test_creates_records(self, tmp_project: Path) -> None:
        index = load_index()
        details = load_details()
        membership, detail = ensure_record_exists(index, details, 123)
        assert membership == {"id": 123}
        assert detail == {"id": 123}

    def test_preserves_existing(self, tmp_project: Path) -> None:
        index = load_index()
        details = load_details()
        index["movies"]["123"] = {"id": 123, "title": "Existing"}
        details["movies"]["123"] = {"id": 123, "runtime": 90}
        membership, detail = ensure_record_exists(index, details, 123)
        assert membership["title"] == "Existing"
        assert detail["runtime"] == 90


class TestNowIso:
    def test_ends_with_z(self) -> None:
        assert now_iso().endswith("Z")
