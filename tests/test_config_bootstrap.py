"""Regression tests for .env bootstrap order.

Guards against the bug where config values were copied at import time before
``.env`` was loaded.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_env_values_visible_after_bootstrap(tmp_path: Path) -> None:
    """Credentials are loaded at config import time and read at use time.

    Runs in a fresh subprocess so we can control the import order and the
    ``.env`` location without affecting the pytest process.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TMDB_API_KEY=fake_api_key\nTMDB_LIST_ID=fake_list_id\nTMDB_SESSION_ID=fake_session\n",
        encoding="utf-8",
    )

    script = tmp_path / "check_bootstrap.py"
    script.write_text(
        f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})

import os
os.environ["TMDB_API_KEY"] = "fake_api_key"
os.environ["TMDB_LIST_ID"] = "fake_list_id"
os.environ["TMDB_SESSION_ID"] = "fake_session"

# Loading config.config must read the environment values immediately.
import config.config as cfg

# Import modules before bootstrap; any import-time copies would be stale.
import main
import src.tmdb_api as tmdb_api

# bootstrap() no longer loads .env, but it must not break credentials either.
cfg.bootstrap()

assert main.check_api_key(), "API key from .env was not detected after bootstrap"
assert main._config.TMDB_LIST_ID == "fake_list_id"

client = tmdb_api.TMDBClient(session_file={str(tmp_path / "session.json")!r})
try:
    assert client.api_key == "fake_api_key", f"got {{client.api_key!r}}"
finally:
    client.close()

print("BOOTSTRAP_REGRESSION_OK")
""",
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if not k.startswith("TMDB_")}
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "BOOTSTRAP_REGRESSION_OK" in result.stdout


def test_module_loggers_reach_the_log_file_and_stdout(tmp_path: Path) -> None:
    """Every src module logs via getLogger(__name__) -- "src.enrich" and so on.

    setup_logging() used to put its handlers on a logger named "movie_tracker",
    which none of those descend from, so their records hit Python's last-resort
    handler: warnings unformatted on stderr, INFO dropped, the log file empty.
    Runs in a subprocess so the handlers it installs on the root logger do not
    leak into the rest of the suite.
    """
    script = tmp_path / "check_logging.py"
    script.write_text(
        f"""
import logging
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})

import config.config as cfg

cfg.setup_logging()
cfg.setup_logging()  # a second call must not add a second pair of handlers
logging.getLogger("src.enrich").warning("module warning reached")
logging.getLogger("src.list_fetcher").info("module info reached")
logging.getLogger("httpx").info("HTTP Request: GET https://example.invalid/?api_key=SECRET")
logging.shutdown()
""",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMDB_")}
    env["TMDB_HOME"] = str(tmp_path / "home")
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)

    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert result.stderr == ""
    assert result.stdout.count("module warning reached") == 1
    assert "module info reached" in result.stdout
    log_text = (tmp_path / "home" / "logs" / "movie_tracker.log").read_text(encoding="utf-8")
    assert "[WARNING] module warning reached" in log_text
    assert "[INFO] module info reached" in log_text
    assert "SECRET" not in log_text + result.stdout
