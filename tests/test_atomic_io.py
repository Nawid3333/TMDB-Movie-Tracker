"""Tests for src.atomic_io.

Most of these cover paths that only run when something has already gone wrong
-- a backup rotation, a failed rename, a dump that raises midway -- because
that is the whole point of the module. The index is the only copy of the
user's list, and a half-finished save is how it gets lost.
"""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from src.atomic_io import _rotate_backups, atomic_write_json


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"key": "value"})
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}

    def test_non_ascii_survives_the_round_trip(self, tmp_path: Path) -> None:
        """ensure_ascii=False is deliberate: film titles are not all ASCII."""
        target = tmp_path / "data.json"
        title = "Amélie — 千と千尋"
        atomic_write_json(str(target), {"title": title})
        assert json.loads(target.read_text(encoding="utf-8"))["title"] == title

    def test_indent_none_writes_one_line(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"a": 1, "b": 2}, indent=None)
        assert "\n" not in target.read_text(encoding="utf-8")

    def test_missing_parent_directory_is_created(self, tmp_path: Path) -> None:
        target = tmp_path / "missing_dir" / "data.json"
        atomic_write_json(str(target), {"ok": True})
        assert target.exists()
        assert not any(tmp_path.glob("*.tmp"))

    def test_a_bare_filename_lands_in_the_working_directory(self, tmp_path: Path, monkeypatch) -> None:
        """A path with no directory part resolves against cwd, not the root."""
        monkeypatch.chdir(tmp_path)
        atomic_write_json("bare.json", {"ok": True})
        assert json.loads((tmp_path / "bare.json").read_text(encoding="utf-8")) == {"ok": True}

    def test_overwriting_replaces_the_content_entirely(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"old": True, "extra": 1})
        atomic_write_json(str(target), {"new": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}

    def test_no_temp_file_is_left_behind(self, tmp_path: Path) -> None:
        atomic_write_json(str(tmp_path / "data.json"), {"a": 1})
        assert not any(tmp_path.glob("*.tmp"))


class TestBackupGenerations:
    def test_first_write_makes_no_backup(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"gen": 1})
        assert not (tmp_path / "data.json.bak1").exists()

    def test_creates_backup(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        target.write_text('{"old": true}', encoding="utf-8")
        atomic_write_json(str(target), {"new": True})
        backup = tmp_path / "data.json.bak1"
        assert backup.exists()
        assert json.loads(backup.read_text(encoding="utf-8")) == {"old": True}

    def test_no_backup_when_disabled(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        target.write_text('{"old": true}', encoding="utf-8")
        atomic_write_json(str(target), {"new": True}, backup=False)
        assert not (tmp_path / "data.json.bak1").exists()

    def test_generations_shift_and_stop_at_three(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        for gen in range(1, 6):
            atomic_write_json(str(target), {"gen": gen})

        def read(name: str) -> dict:
            return json.loads((tmp_path / name).read_text(encoding="utf-8"))

        assert read("data.json") == {"gen": 5}
        assert read("data.json.bak1") == {"gen": 4}
        assert read("data.json.bak2") == {"gen": 3}
        assert read("data.json.bak3") == {"gen": 2}
        assert not (tmp_path / "data.json.bak4").exists()

    def test_a_generation_beyond_three_is_removed(self, tmp_path: Path) -> None:
        """An older layout kept more; a rotation must not leave them behind."""
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"gen": 1})
        for i in range(3, 10):
            (tmp_path / f"data.json.bak{i}").write_text("stale", encoding="utf-8")
        atomic_write_json(str(target), {"gen": 2})
        for i in range(4, 10):
            assert not (tmp_path / f"data.json.bak{i}").exists(), f"bak{i} survived"


class TestRotateBackups:
    """_rotate_backups on its own, including the errors it deliberately eats."""

    def test_rotation_is_a_no_op_when_there_is_nothing_to_rotate(self, tmp_path: Path) -> None:
        _rotate_backups(str(tmp_path / "data.json"))
        assert list(tmp_path.iterdir()) == []

    def test_rotation_shifts_each_generation_up_by_one(self, tmp_path: Path) -> None:
        (tmp_path / "data.json.bak1").write_text("one", encoding="utf-8")
        (tmp_path / "data.json.bak2").write_text("two", encoding="utf-8")
        _rotate_backups(str(tmp_path / "data.json"))
        assert (tmp_path / "data.json.bak2").read_text(encoding="utf-8") == "one"
        assert (tmp_path / "data.json.bak3").read_text(encoding="utf-8") == "two"

    def test_an_unremovable_stale_backup_does_not_stop_the_rotation(self, tmp_path: Path) -> None:
        """The suppressed OSError: a locked file must not fail the save."""
        (tmp_path / "data.json.bak1").write_text("one", encoding="utf-8")
        (tmp_path / "data.json.bak5").write_text("stale", encoding="utf-8")
        with mock.patch("src.atomic_io.os.remove", side_effect=OSError("locked")):
            _rotate_backups(str(tmp_path / "data.json"))
        assert (tmp_path / "data.json.bak2").read_text(encoding="utf-8") == "one"

    def test_an_unmovable_generation_does_not_stop_the_rotation(self, tmp_path: Path) -> None:
        (tmp_path / "data.json.bak1").write_text("one", encoding="utf-8")
        with mock.patch("src.atomic_io.os.replace", side_effect=OSError("locked")):
            _rotate_backups(str(tmp_path / "data.json"))
        assert (tmp_path / "data.json.bak1").read_text(encoding="utf-8") == "one"


class TestWriteFailures:
    """What is on disk after a write that did not finish."""

    def test_a_dump_that_raises_leaves_the_previous_file_intact(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"good": True})
        with mock.patch("src.atomic_io.json.dump", side_effect=ValueError("boom")), pytest.raises(ValueError):
            atomic_write_json(str(target), {"bad": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"good": True}

    def test_a_dump_that_raises_leaves_no_temp_file(self, tmp_path: Path) -> None:
        with mock.patch("src.atomic_io.json.dump", side_effect=ValueError("boom")), pytest.raises(ValueError):
            atomic_write_json(str(tmp_path / "data.json"), {"bad": True})
        assert not any(tmp_path.glob("*.tmp"))

    def test_a_failed_final_rename_restores_the_file_from_bak1(self, tmp_path: Path) -> None:
        """The bug this branch exists for: the path must never end up empty."""
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"gen": 1})

        real_replace = os.replace
        failed = []

        def replace(src, dst):
            # The write does: rotate, file -> .bak1, then tmp -> file, and on
            # failure .bak1 -> file. The last two share a destination, so the
            # source tells them apart: fail the incoming temp file only.
            if str(dst) == str(target) and str(src).endswith(".tmp"):
                failed.append(dst)
                raise OSError("rename failed")
            return real_replace(src, dst)

        with mock.patch("src.atomic_io.os.replace", side_effect=replace), pytest.raises(OSError):
            atomic_write_json(str(target), {"gen": 2})

        assert failed, "the failing rename never ran"
        assert target.exists(), "the file was left missing"
        assert json.loads(target.read_text(encoding="utf-8")) == {"gen": 1}

    def test_a_backup_that_cannot_be_made_does_not_stop_the_write(self, tmp_path: Path) -> None:
        """If the old file cannot be moved aside, the new data still lands."""
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"gen": 1})

        real_replace = os.replace

        def replace(src, dst):
            if str(dst) == f"{target}.bak1":
                raise OSError("locked")
            return real_replace(src, dst)

        with mock.patch("src.atomic_io.os.replace", side_effect=replace):
            atomic_write_json(str(target), {"gen": 2})

        assert json.loads(target.read_text(encoding="utf-8")) == {"gen": 2}

    def test_when_the_restore_also_fails_the_data_survives(self, tmp_path: Path) -> None:
        """The worst case. Nothing can be put back, so nothing may be lost."""
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"gen": 1})

        with mock.patch("src.atomic_io.os.replace", side_effect=OSError("read-only")), pytest.raises(OSError):
            atomic_write_json(str(target), {"gen": 2})

        surviving = target if target.exists() else tmp_path / "data.json.bak1"
        assert json.loads(surviving.read_text(encoding="utf-8")) == {"gen": 1}
