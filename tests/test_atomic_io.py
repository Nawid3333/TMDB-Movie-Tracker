"""Tests for src.atomic_io."""

import json
from pathlib import Path

from src.atomic_io import atomic_write_json


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(str(target), {"key": "value"})
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}

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

    def test_cleanup_temp_on_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "missing_dir" / "data.json"
        atomic_write_json(str(target), {"ok": True})
        assert target.exists()
        assert not any(tmp_path.glob("*.tmp"))
