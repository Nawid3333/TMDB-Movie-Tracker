"""Tests for src.posters."""

from pathlib import Path
from unittest.mock import patch

import httpx
import respx

from src.posters import (
    _poster_url,
    detect_poster_mode,
    download_poster,
    poster_cache_path,
)


class TestPosterCachePath:
    def test_deterministic(self, tmp_path: Path) -> None:
        path1 = poster_cache_path(550, "/xx.jpg", tmp_path)
        path2 = poster_cache_path(550, "/xx.jpg", tmp_path)
        assert path1 == path2
        assert path1.name.startswith("550_")
        assert path1.suffix == ".jpg"


class TestPosterUrl:
    def test_url(self) -> None:
        assert _poster_url("/abc.jpg", "w342") == "https://image.tmdb.org/t/p/w342/abc.jpg"


class TestDownloadPoster:
    @respx.mock
    def test_downloads_when_missing(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(
            return_value=httpx.Response(200, content=b"poster-data")
        )
        dest = download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path)
        assert dest is not None
        assert dest.read_bytes() == b"poster-data"

    @respx.mock
    def test_skip_existing(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        dest = poster_cache_path(550, "/abc.jpg", tmp_path)
        dest.write_bytes(b"cached")
        result = download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path, skip_existing=True)
        assert result == dest
        assert result.read_bytes() == b"cached"

    @respx.mock
    def test_failure_returns_none(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(500))
        assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) is None


class TestDetectPosterMode:
    def test_explicit(self) -> None:
        assert detect_poster_mode("iterm") == "iterm"

    @patch.dict("os.environ", {"WT_SESSION": "1"}, clear=False)
    def test_auto_wt(self) -> None:
        assert detect_poster_mode("auto") == "iterm"
