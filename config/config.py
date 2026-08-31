"""Configuration for the TMDB Movie Tracker.

Loads environment variables, sets paths, and provides tunables.
"""

import contextlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv


def _configure_console() -> None:
    """Make arrow/box-drawing output safe on any code page."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


_configure_console()

# Load environment variables from .env file at import time.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ==================== CREDENTIALS ====================
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_LIST_ID = os.getenv("TMDB_LIST_ID", "").strip()
TMDB_SESSION_ID = os.getenv("TMDB_SESSION_ID", "").strip()
TMDB_V4_ACCESS_TOKEN = os.getenv("TMDB_V4_ACCESS_TOKEN", "").strip()
TMDB_USERNAME = os.getenv("TMDB_USERNAME", "").strip()
TMDB_PASSWORD = os.getenv("TMDB_PASSWORD", "").strip()

# ==================== REGION / LANGUAGE ====================
TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "de-DE").strip() or "de-DE"
TMDB_FALLBACK_REGION = os.getenv("TMDB_FALLBACK_REGION", "DE").strip() or "DE"

# ==================== DIRECTORIES ====================
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
POSTERS_DIR = DATA_DIR / "posters"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
POSTERS_DIR.mkdir(parents=True, exist_ok=True)

# ==================== FILE PATHS ====================
INDEX_FILE = DATA_DIR / "index.json"
DETAILS_FILE = DATA_DIR / "details.json"
SESSION_FILE = DATA_DIR / "session.json"
COLLECTION_CACHE_FILE = DATA_DIR / "collection_cache.json"
KEYWORD_TV_CACHE_FILE = DATA_DIR / "keyword_tv_cache.json"
GAPS_FILE = DATA_DIR / "gaps.json"
ENRICH_CHECKPOINT_FILE = DATA_DIR / "enrich_checkpoint.json"
LOG_FILE = LOGS_DIR / "movie_tracker.log"

# ==================== API SETTINGS ====================
TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
TMDB_READ_MAX_RETRIES = int(os.getenv("TMDB_READ_MAX_RETRIES", "3"))
TMDB_READ_RETRY_DELAY = float(os.getenv("TMDB_READ_RETRY_DELAY", "5"))
TMDB_MAX_REQUESTS_PER_SECOND = float(os.getenv("TMDB_MAX_REQUESTS_PER_SECOND", "30"))
TMDB_DETAIL_WORKERS = int(os.getenv("TMDB_DETAIL_WORKERS", "16"))
TMDB_LIST_PAGE_WORKERS = int(os.getenv("TMDB_LIST_PAGE_WORKERS", "4"))
TMDB_MAX_CONNECTIONS = int(os.getenv("TMDB_MAX_CONNECTIONS", "24"))
TMDB_HTTP_TIMEOUT = float(os.getenv("TMDB_HTTP_TIMEOUT", "30"))

# ==================== ENRICHMENT FRESHNESS ====================
# Enrichment freshness
WARM_DAYS = int(os.getenv("TMDB_WARM_DAYS", "90"))
COOL_DAYS = int(os.getenv("TMDB_COOL_DAYS", "730"))
COLD_REENRICH_DAYS = int(os.getenv("TMDB_COLD_REENRICH_DAYS", "90"))
MIN_SHRINK_RATIO = float(os.getenv("TMDB_MIN_SHRINK_RATIO", "0.5"))

# ==================== FRANCHISE KEYWORD HEURISTIC ====================
FRANCHISE_KEYWORD_MIN_MOVIES = int(os.getenv("TMDB_FRANCHISE_KEYWORD_MIN_MOVIES", "2"))

# ==================== POSTER RENDERING ====================
POSTER_MODE = os.getenv("POSTER_MODE", "auto").strip().lower()
POSTER_SIZE = os.getenv("POSTER_SIZE", "w342").strip() or "w342"


# ==================== LOGGING ====================
def setup_logging() -> logging.Logger:
    """Configure rotating file + console logging."""
    logger = logging.getLogger("movie_tracker")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger
