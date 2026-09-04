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

from src.ui import term

# ==================== DIRECTORIES ====================
# Every path this program reads or writes -- .env, data/, logs/, posters/ --
# hangs off BASE_DIR, so there is a single thing to point somewhere else.
#
# Unset, TMDB_HOME leaves this as the repo checkout exactly as it always was,
# so running from a clone is byte-for-byte unchanged. Setting it is what makes
# an installed copy usable: in a venv this file sits inside site-packages,
# where no user can reasonably find a .env to edit.
_DEFAULT_HOME = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("TMDB_HOME") or _DEFAULT_HOME).resolve()
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
POSTERS_DIR = DATA_DIR / "posters"

# ==================== FILE PATHS ====================
INDEX_FILE = DATA_DIR / "index.json"
DETAILS_FILE = DATA_DIR / "details.json"
COLLECTION_CACHE_FILE = DATA_DIR / "collection_cache.json"
KEYWORD_TV_CACHE_FILE = DATA_DIR / "keyword_tv_cache.json"
GAPS_FILE = DATA_DIR / "gaps.json"
ENRICH_CHECKPOINT_FILE = DATA_DIR / "enrich_checkpoint.json"
LOG_FILE = LOGS_DIR / "movie_tracker.log"

# Mismatch report written when the local index differs from the live TMDB list.
MISMATCH_REPORT_FILE = DATA_DIR / "mismatch_report.json"

# Default batch file for adding movies from a list of URLs/IDs.
DEFAULT_BATCH_FILE = BASE_DIR / "movie_urls.txt"

# Dedicated export file for franchise gaps, so gap URLs do not mix with the
# manual push queue in DEFAULT_BATCH_FILE.
FRANCHISE_GAPS_EXPORT_FILE = DATA_DIR / "franchise_gaps_urls.txt"


# Load the environment file before reading any settings so credentials and
# tunables are available as soon as this module is imported. This matches the
# pattern used in the other scraper projects and removes the bootstrap-order
# bug where values were copied before ``.env`` was loaded.
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)


# The credentials template, written on first run by ensure_env_file(). This is
# the single source of truth for it: tests/test_env_bootstrap.py asserts that
# .env.example matches, so the shipped example cannot drift from what someone
# installing the package actually receives.
ENV_TEMPLATE = """# Fill in the values below. This file is never committed.
TMDB_API_KEY=your_tmdb_api_key_here
TMDB_LIST_ID=your_list_id_here

# Optional: session allows private list reads and list writes.
TMDB_SESSION_ID=
TMDB_V4_ACCESS_TOKEN=
TMDB_USERNAME=
TMDB_PASSWORD=

# Language/region defaults
TMDB_LANGUAGE=de-DE
TMDB_FALLBACK_REGION=DE
"""


def ensure_env_file():
    """Write ENV_TEMPLATE to ENV_FILE if no .env exists there yet.

    Returns the path written, or None when a file was already present -- an
    existing .env is never read, altered or overwritten. Called from the CLI
    entry point rather than at import time, because importing this module must
    stay free of side effects: the test suite imports it constantly.
    """
    if ENV_FILE.exists():
        return None
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    return ENV_FILE


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


# ==================== LOGGING ====================
def setup_logging() -> logging.Logger:
    """Configure rotating file + console logging."""
    logger = logging.getLogger("movie_tracker")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # PlainFormatter strips any colour a call site added for the console, so
    # the log file stays greppable.
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(term.PlainFormatter("%(asctime)s [%(levelname)s] %(message)s"))

    # Console: warnings yellow, errors red, criticals magenta.
    # Route to stdout so log lines and print()/input() share one ordered
    # stream; stderr would interleave with the menu prompt and echo input
    # mid-scroll.
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(term.ColorFormatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger


def bootstrap() -> None:
    """Reconfigure stdout/stderr for UTF-8.

    Called once from ``main.py``. The environment file is already loaded at
    module import time, so this function only normalises the console encoding
    and can be called safely from tests without side effects.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
