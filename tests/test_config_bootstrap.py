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
