"""Tests for src.changes."""

from src.changes import ChangeSet, apply_changes, detect_changes


class TestDetectChanges:
    def test_empty_current(self, fixtures: dict) -> None:
        fetched = fixtures["list"]["items"][:2]
        change_set = detect_changes({}, fetched)
        assert len(change_set.additions) == 2
        assert not change_set.removals
        assert not change_set.unchanged

    def test_no_changes(self, fixtures: dict) -> None:
        fetched = fixtures["list"]["items"][:2]
        current = {}
        for item in fetched:
            movie = item if item.get("media_type") == "movie" else item
            current[str(movie["id"])] = {
                "id": movie["id"],
                "title": movie.get("title", ""),
                "release_date": movie.get("release_date", ""),
            }
        change_set = detect_changes(current, fetched)
        assert not change_set.additions
        assert not change_set.removals
        assert len(change_set.unchanged) == 2

    def test_shrink_gate_blocks_removals(self) -> None:
        current = {str(i): {"id": i, "title": f"Movie {i}"} for i in range(1, 11)}
        fetched = [{"media_type": "movie", "id": 1, "title": "Movie 1", "release_date": ""}]
        change_set = detect_changes(current, fetched, min_shrink_ratio=0.5)
        assert not change_set.removals
        assert change_set.incomplete is True

    def test_incomplete_flag_blocks_removals(self) -> None:
        current = {"1": {"id": 1, "title": "Movie 1"}}
        fetched = []
        change_set = detect_changes(current, fetched, incomplete=True)
        assert not change_set.removals
        assert change_set.incomplete is True


class TestApplyChanges:
    def test_approve_additions(self) -> None:
        current = {"1": {"id": 1}}
        change_set = ChangeSet(additions={"2": {"id": 2}})
        merged = apply_changes(current, change_set, approve_additions=True)
        assert "2" in merged
        assert "1" in merged

    def test_approve_removals(self) -> None:
        current = {"1": {"id": 1}, "2": {"id": 2}}
        change_set = ChangeSet(removals={"2": {"id": 2}})
        merged = apply_changes(current, change_set, approve_removals=True)
        assert "1" in merged
        assert "2" not in merged

    def test_default_does_not_change(self) -> None:
        current = {"1": {"id": 1}, "2": {"id": 2}}
        change_set = ChangeSet(
            additions={"3": {"id": 3}},
            removals={"2": {"id": 2}},
        )
        merged = apply_changes(current, change_set)
        assert merged == current
