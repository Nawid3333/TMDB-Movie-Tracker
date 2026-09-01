"""Timing benchmarks for the index-sized paths in the movie tracker.

Skipped unless ``--benchmark`` is passed. See ``tests/bench.py`` for the
harness, the tolerance, and how to re-record the baseline.

What belongs here
-----------------
Work whose cost grows with the number of tracked movies: change detection over
the whole list, the gap scan that walks every collection and keyword, and the
atomic index writer that serialises the entire file on every save.

What does not: anything that makes a request. The API paths are covered by the
respx-mocked tests, and timing a mock measures the mock.

Every benchmark builds its input once, outside the timed callable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.atomic_io import atomic_write_json
from src.changes import apply_changes, detect_changes
from src.gaps import find_gaps
from src.index import build_membership_record, validate_membership_record


def _fetched(count: int) -> list[dict]:
    """A TMDB list payload of ``count`` movies, in the shape the API returns."""
    return [
        {
            "media_type": "movie",
            "id": n,
            "title": f"Movie {n:05d}",
            "original_title": f"Movie {n:05d}",
            "release_date": "2020-01-01",
            "poster_path": f"/p{n}.jpg",
        }
        for n in range(1, count + 1)
    ]


def _current(count: int) -> dict[str, dict]:
    return {str(movie["id"]): build_membership_record(movie) for movie in _fetched(count)}


@pytest.mark.benchmark
def test_change_detection_over_a_large_list(bench):
    """Runs on every fast scan, over the whole list."""
    current = _current(5000)
    fetched = _fetched(5000)
    bench("detect_changes/5000_movies", lambda: detect_changes(current, fetched), repeats=3)


@pytest.mark.benchmark
def test_change_detection_with_heavy_churn(bench):
    """Half added, half removed -- the worst case for the set arithmetic."""
    current = _current(5000)
    fetched = _fetched(7500)[2500:]
    bench("detect_changes/5000_movies_churn", lambda: detect_changes(current, fetched), repeats=3)


@pytest.mark.benchmark
def test_applying_approved_changes(bench):
    current = _current(5000)
    change_set = detect_changes(current, _fetched(7500))
    bench(
        "apply_changes/5000_movies",
        lambda: apply_changes(current, change_set, approve_additions=True, approve_removals=True),
        repeats=3,
    )


@pytest.mark.benchmark
def test_membership_record_validation(bench):
    """Called for every item of every fetched page."""
    records = list(_current(5000).values())
    bench("validate_membership_record/5000", lambda: [validate_membership_record(r) for r in records])


@pytest.mark.benchmark
def test_atomic_index_write(bench, tmp_path: Path):
    """Serialises and fsyncs the whole index on every save."""
    payload = {"schema_version": 1, "movies": _current(5000)}
    target = tmp_path / "index.json"
    bench("atomic_write_json/5000_movies", lambda: atomic_write_json(target, payload, backup=False), repeats=3)


@pytest.mark.benchmark
def test_gap_scan_over_a_full_index(bench, tmp_project, monkeypatch):
    """Walks every collection part and every connected TV entry in the index."""
    import src.gaps as gaps_module
    import src.index as index_module

    index = {"schema_version": 1, "movies": {str(n): {"id": n} for n in range(1, 2001)}}
    details = {
        "schema_version": 1,
        "movies": {
            str(n): {
                "id": n,
                "keywords": [f"franchise-{n % 40}", "sequel"],
                "collection": {
                    "id": n % 200,
                    "name": f"Collection {n % 200}",
                    "parts": [{"id": 900000 + n, "title": f"Missing {n}", "release_date": "2030-01-01"}],
                },
                "connected_tv": [
                    {
                        "id": 800000 + n,
                        "name": f"Show {n}",
                        "first_air_date": "2021-01-01",
                        "via_keyword": f"franchise-{n % 40}",
                    }
                ],
            }
            for n in range(1, 2001)
        },
    }
    monkeypatch.setattr(index_module, "load_index", lambda: index)
    monkeypatch.setattr(index_module, "load_details", lambda: details)
    monkeypatch.setattr(gaps_module, "load_index", lambda: index)
    monkeypatch.setattr(gaps_module, "load_details", lambda: details)

    assert find_gaps(persist=False)["missing_films"], "fixture must produce gaps before it is timed"
    bench("find_gaps/2000_movies", lambda: find_gaps(persist=False), repeats=3)


@pytest.mark.benchmark
def test_index_json_load(bench, tmp_path: Path):
    """Startup cost; the real file grows with the library."""
    payload = {"schema_version": 1, "movies": _current(5000)}
    target = tmp_path / "index.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    bench("index/json_load_5000_movies", lambda: json.loads(target.read_text(encoding="utf-8")), repeats=3)
