"""Poster download into the local cache (data/posters/)."""

import hashlib
import logging
from pathlib import Path

import httpx

from config.config import POSTER_SIZE
from src.atomic_io import atomic_write_bytes

logger = logging.getLogger(__name__)

__all__ = [
    "download_poster",
    "poster_cache_path",
]


def _poster_url(poster_path: str, size: str = POSTER_SIZE) -> str:
    return f"https://image.tmdb.org/t/p/{size}{poster_path}"


def poster_cache_path(movie_id: int, poster_path: str, posters_dir: Path | None = None) -> Path:
    """Return the cache path for a poster, hashing the TMDB path into the filename."""
    if posters_dir is None:
        from config.config import POSTERS_DIR

        posters_dir = POSTERS_DIR
    short_hash = hashlib.sha1(poster_path.encode("utf-8")).hexdigest()[:8]
    return posters_dir / f"{movie_id}_{short_hash}.jpg"


def download_poster(
    client: httpx.Client,
    movie_id: int,
    poster_path: str | None,
    *,
    posters_dir: Path | None = None,
    skip_existing: bool = True,
) -> Path | None:
    """Download a poster from TMDB's image CDN.

    Returns the local cache path, or None if there is no poster_path or the download failed.
    """
    if not poster_path:
        return None
    dest = poster_cache_path(movie_id, poster_path, posters_dir)
    # An empty file is not a cached poster. Writes used to go straight to
    # the final name, so a run stopped mid-write left a short file there, and
    # this check -- "it exists" -- then kept it for good.
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return dest

    url = _poster_url(poster_path)
    try:
        resp = client.get(url, timeout=30)
        if resp.status_code >= 400:
            logger.warning("Poster fetch failed for %s: HTTP %s", movie_id, resp.status_code)
            return None
        if not resp.content:
            logger.warning("Poster fetch for %s returned an empty body", movie_id)
            return None
        # Atomic: the poster appears under its final name complete or not at all.
        atomic_write_bytes(dest, resp.content)
        return dest
    except Exception as exc:
        logger.warning("Poster download failed for %s: %s", movie_id, exc)
        return None
