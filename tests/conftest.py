"""Shared pytest fixtures and configuration."""

import json
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.tmdb_api import TMDBClient  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "fixtures" / "generated"


def _load_fixture(rel_path: str) -> dict:
    path = FIXTURES / rel_path
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def fixtures() -> dict[str, dict]:
    """Load all captured real TMDB fixtures by friendly key.

    These are captured from the live API and deliberately kept out of git, so
    a fresh clone does not have them. Skip rather than error in that case: the
    suite has to stay runnable for anyone who has not captured them, which is
    every CI run and every new contributor. Regenerate with
    ``python tests/capture_fixtures.py``.
    """
    try:
        return _all_fixtures()
    except FileNotFoundError as missing:
        pytest.skip(f"no captured fixture ({missing.filename}) -- run tests/capture_fixtures.py")


def _all_fixtures() -> dict[str, dict]:
    return {
        "list": _load_fixture("lists/list_8678795.json"),
        "movie_9013": _load_fixture("movies/movie_9013.json"),
        "movie_475557": _load_fixture("movies/movie_475557.json"),
        "movie_398978": _load_fixture("movies/movie_398978.json"),
        "movie_769": _load_fixture("movies/movie_769.json"),
        "movie_1013601": _load_fixture("movies/movie_1013601.json"),
        "search_inception": _load_fixture("search/inception.json"),
        "find_imdb": _load_fixture("search/find_imdb.json"),
        "collection_633215": _load_fixture("collections/collection_633215.json"),
        "collection_987044": _load_fixture("collections/collection_987044.json"),
    }


@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect data/logs dirs to a temp path so tests never touch real files."""
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("config.config.DATA_DIR", data_dir)
    monkeypatch.setattr("config.config.LOGS_DIR", logs_dir)
    monkeypatch.setattr("config.config.INDEX_FILE", data_dir / "index.json")
    monkeypatch.setattr("config.config.DETAILS_FILE", data_dir / "details.json")
    monkeypatch.setattr("config.config.TMDB_SESSION_ID", "")
    monkeypatch.setattr("config.config.COLLECTION_CACHE_FILE", data_dir / "collection_cache.json")
    monkeypatch.setattr("config.config.KEYWORD_TV_CACHE_FILE", data_dir / "keyword_tv_cache.json")
    monkeypatch.setattr("config.config.GAPS_FILE", data_dir / "gaps.json")
    monkeypatch.setattr("config.config.ENRICH_CHECKPOINT_FILE", data_dir / "enrich_checkpoint.json")
    monkeypatch.setattr("config.config.POSTERS_DIR", data_dir / "posters")
    monkeypatch.setattr("config.config.LOG_FILE", logs_dir / "movie_tracker.log")

    # Modules import config constants at load time; patch their copies too.
    import src.gaps as _gaps
    import src.index as _index
    import src.list_fetcher as _list_fetcher

    monkeypatch.setattr(_index, "INDEX_FILE", data_dir / "index.json")
    monkeypatch.setattr(_index, "DETAILS_FILE", data_dir / "details.json")
    monkeypatch.setattr(_gaps, "GAPS_FILE", data_dir / "gaps.json")
    monkeypatch.setattr(_list_fetcher, "DATA_DIR", data_dir)

    return tmp_path


@pytest.fixture
def client(tmp_path: Path) -> Generator[TMDBClient, None, None]:
    """Build a TMDBClient with a test API key and ensure it is closed."""
    with TMDBClient(session_file=tmp_path / "session.json", api_key="test_key") as c:
        yield c


@pytest.fixture
def httpx_client() -> Generator[httpx.Client, None, None]:
    """Yield a plain httpx client and close it."""
    client = httpx.Client(timeout=5)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def fake_image_client(monkeypatch: pytest.MonkeyPatch) -> Generator[httpx.Client, None, None]:
    """httpx client whose get() returns a fake poster bytes response."""
    client = httpx.Client(timeout=5)

    def _fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, content=b"fake-poster-bytes", headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(client, "get", _fake_get)
    try:
        yield client
    finally:
        client.close()


# ── benchmark wiring ────────────────────────────────────────────────────────
# Timing tests live in tests/test_benchmarks.py and are skipped in a normal
# run so the everyday suite stays fast. See tests/bench.py for the harness,
# the tolerance, and how to re-record the baseline.


def pytest_addoption(parser):
    parser.addoption(
        "--benchmark",
        action="store_true",
        default=False,
        help="Run the timing benchmarks in tests/test_benchmarks.py.",
    )
    parser.addoption(
        "--benchmark-update",
        action="store_true",
        default=False,
        help="Run the benchmarks and rewrite tests/benchmark_baseline.json with the new timings.",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "benchmark: a timing test, skipped unless --benchmark is passed")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--benchmark") or config.getoption("--benchmark-update"):
        return
    skip = pytest.mark.skip(reason="timing test; pass --benchmark to run")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def bench(request):
    """Session-wide timing recorder; see tests/bench.py for the contract."""
    from tests.bench import Recorder

    recorder = Recorder(update=request.config.getoption("--benchmark-update"))
    yield recorder
    recorder.flush()
