"""Tests for src.posters."""

from pathlib import Path
from unittest.mock import patch

import httpx
import respx

from src.posters import (
    _poster_url,
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
        assert result is not None
        assert result == dest
        assert result.read_bytes() == b"cached"

    @respx.mock
    def test_failure_returns_none(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(500))
        assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) is None


class TestDownloadPosterEdges:
    def test_no_poster_path_returns_none_without_a_request(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        """A movie with no artwork must not cost a round trip."""
        assert download_poster(httpx_client, 550, None, posters_dir=tmp_path) is None
        assert download_poster(httpx_client, 550, "", posters_dir=tmp_path) is None

    @respx.mock
    def test_skip_existing_false_refetches_over_the_cached_file(
        self, tmp_path: Path, httpx_client: httpx.Client
    ) -> None:
        dest = poster_cache_path(550, "/abc.jpg", tmp_path)
        dest.write_bytes(b"stale")
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(200, content=b"fresh"))
        result = download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path, skip_existing=False)
        assert result is not None
        assert result.read_bytes() == b"fresh"

    @respx.mock
    def test_a_404_returns_none_and_writes_nothing(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(404))
        assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) is None
        assert not poster_cache_path(550, "/abc.jpg", tmp_path).exists()


class TestPosterWritesAreAtomic:
    """Posters were written straight to their final name, and "it exists" was the cache check.

    A run stopped mid-write left a short file there, and it was kept forever.
    """

    @respx.mock
    def test_a_write_that_fails_leaves_nothing_under_the_final_name(
        self, tmp_path: Path, httpx_client: httpx.Client
    ) -> None:
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(200, content=b"x" * 64))
        with patch("src.atomic_io.os.replace", side_effect=OSError("disk full")):
            assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) is None
        assert not poster_cache_path(550, "/abc.jpg", tmp_path).exists()
        assert not list(tmp_path.glob("*.tmp"))

    @respx.mock
    def test_an_empty_cached_file_is_fetched_again(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        dest = poster_cache_path(550, "/abc.jpg", tmp_path)
        dest.write_bytes(b"")
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(200, content=b"fresh"))
        assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) == dest
        assert dest.read_bytes() == b"fresh"

    @respx.mock
    def test_an_empty_body_is_not_cached(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(return_value=httpx.Response(200, content=b""))
        assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) is None
        assert not poster_cache_path(550, "/abc.jpg", tmp_path).exists()

    def test_a_transport_error_returns_none_rather_than_raising(
        self, tmp_path: Path, httpx_client: httpx.Client
    ) -> None:
        """A poster is decoration; losing the network must not fail the run."""
        with patch.object(httpx_client, "get", side_effect=httpx.ConnectError("no route")):
            assert download_poster(httpx_client, 550, "/abc.jpg", posters_dir=tmp_path) is None

    @respx.mock
    def test_a_missing_cache_directory_is_created(self, tmp_path: Path, httpx_client: httpx.Client) -> None:
        nested = tmp_path / "does" / "not" / "exist"
        respx.get("https://image.tmdb.org/t/p/w342/abc.jpg").mock(
            return_value=httpx.Response(200, content=b"poster-data")
        )
        dest = download_poster(httpx_client, 550, "/abc.jpg", posters_dir=nested)
        assert dest is not None and dest.exists()


class TestPosterCachePathEdges:
    def test_different_artwork_for_one_movie_gets_different_files(self, tmp_path: Path) -> None:
        """The hash is what stops a replaced poster from serving a stale image."""
        first = poster_cache_path(550, "/one.jpg", tmp_path)
        second = poster_cache_path(550, "/two.jpg", tmp_path)
        assert first != second

    def test_the_configured_posters_dir_is_used_when_none_is_given(self, tmp_path: Path) -> None:
        with patch("config.config.POSTERS_DIR", tmp_path):
            assert poster_cache_path(550, "/abc.jpg").parent == tmp_path
