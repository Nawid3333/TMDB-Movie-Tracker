"""Poster download and terminal rendering helpers."""

import hashlib
import logging
import os
from pathlib import Path

import httpx

from config.config import POSTER_MODE, POSTER_SIZE, POSTERS_DIR

logger = logging.getLogger(__name__)


def _poster_url(poster_path: str, size: str = POSTER_SIZE) -> str:
    return f"https://image.tmdb.org/t/p/{size}{poster_path}"


def poster_cache_path(movie_id: int, poster_path: str, posters_dir: Path = POSTERS_DIR) -> Path:
    """Return the cache path for a poster, hashing the TMDB path into the filename."""
    short_hash = hashlib.sha1(poster_path.encode("utf-8")).hexdigest()[:8]
    return posters_dir / f"{movie_id}_{short_hash}.jpg"


def download_poster(
    client: httpx.Client,
    movie_id: int,
    poster_path: str | None,
    *,
    posters_dir: Path = POSTERS_DIR,
    skip_existing: bool = True,
) -> Path | None:
    """Download a poster from TMDB's image CDN.

    Returns the local cache path, or None if there is no poster_path or the download failed.
    """
    if not poster_path:
        return None
    dest = poster_cache_path(movie_id, poster_path, posters_dir)
    if skip_existing and dest.exists():
        return dest

    url = _poster_url(poster_path)
    try:
        resp = client.get(url, timeout=30)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(resp.content)
        return dest
    except Exception as exc:
        logger.warning("Poster download failed for %s: %s", movie_id, exc)
        return None


def detect_poster_mode(mode: str = POSTER_MODE) -> str:
    """Return the effective poster rendering mode for this terminal."""
    mode = mode.lower()
    if mode != "auto":
        return mode

    import os

    if os.getenv("WT_SESSION"):
        return "iterm"
    if os.getenv("TERM_PROGRAM") == "iTerm.app" or os.getenv("TERM_PROGRAM") == "WezTerm":
        return "iterm"
    if os.getenv("KITTY_WINDOW_ID"):
        return "kitty"
    return "blocks"


def render_poster(path: Path | str, mode: str = POSTER_MODE) -> str:
    """Return a terminal escape-string for the poster, or a placeholder."""
    effective = detect_poster_mode(mode)
    if effective == "off" or not path or not os.path.exists(str(path)):
        return ""

    data = Path(path).read_bytes()
    if effective == "iterm":
        return _render_iterm(data)
    if effective == "kitty":
        return _render_kitty(data)
    if effective == "sixel":
        return ""
    if effective == "blocks":
        return ""
    return ""


def _render_iterm(data: bytes) -> str:
    """Render an inline JPEG using the iTerm2 image protocol."""
    import base64

    b64 = base64.b64encode(data).decode("ascii")
    return f"\033]1337;File=inline=1:{b64}\007"


def _render_kitty(data: bytes) -> str:
    """Render an inline image using the Kitty graphics protocol (placeholder)."""
    return ""


def prefetch_missing_posters(
    client: httpx.Client,
    movies: dict[str, dict],
    *,
    posters_dir: Path = POSTERS_DIR,
) -> dict[int, Path | None]:
    """Download all missing posters for a set of movies.

    Returns a mapping of movie id -> cache path or None.
    """
    results: dict[int, Path | None] = {}
    for record in movies.values():
        movie_id = record.get("id")
        poster_path = record.get("poster_path")
        if not movie_id or not poster_path:
            continue
        results[movie_id] = download_poster(client, movie_id, poster_path, posters_dir=posters_dir)
    return results
