"""The .env template: where it lives, and the copy written on first run.

Two things are pinned here. ``.env.example`` in the repo root must match
``ENV_TEMPLATE`` in config, because they are two faces of one thing -- the file
a reader opens has to be the file a fresh install actually receives, or the
instructions drift away from the behaviour. And ``ensure_env_file`` must create
that file when none exists while never touching one that does: it runs on every
CLI start, so a mistake there would overwrite real credentials.

Style note for future edits
---------------------------
The bootstrap runs in a subprocess with TMDB_HOME pointed at tmp_path,
because config resolves ENV_FILE at import time. Calling it in-process would
resolve against the checkout and write a .env into the repo instead.
Comparisons normalise line endings: git may check the example out with CRLF
while ENV_TEMPLATE is a Python string with LF, and that gap is not a defect.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

HOME_VAR = "TMDB_HOME"
CONFIG_MODULE = "config.config"
SENTINEL = "SECRET=already-here\n"


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _run(home: Path, snippet: str) -> str:
    """Import config with the project home pointed at ``home`` and run ``snippet``."""
    env = dict(os.environ)
    env[HOME_VAR] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", f"import {CONFIG_MODULE} as c;" + snippet],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    # config prints a banner on import in some of these repos, so the value is
    # tagged rather than assumed to be the whole of stdout.
    marker = "<<RESULT>>"
    line = next(x for x in proc.stdout.splitlines() if x.startswith(marker))
    return line[len(marker) :]


def _example() -> str:
    # No newline= argument: Path.read_text only grew one in 3.13, and this
    # project supports 3.11. The default already translates CRLF to LF, which
    # is exactly the normalisation the comparisons want.
    return _norm((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))


class TestTheShippedExample:
    def test_it_matches_the_template_the_program_writes(self):
        """If these drift, the documented file stops describing the real one."""
        printed = _run(REPO_ROOT, "print('<<RESULT>>' + repr(c.ENV_TEMPLATE))").strip()
        assert _example() == _norm(ast.literal_eval(printed))

    def test_it_sits_at_the_repo_root(self):
        """Root is where dotenv, editors and every README convention look."""
        assert (REPO_ROOT / ".env.example").is_file()
        assert not (REPO_ROOT / "config" / ".env.example").exists()


class TestFirstRunBootstrap:
    def test_it_writes_the_template_when_no_env_exists(self, tmp_path):
        written = _run(tmp_path, "print('<<RESULT>>' + str(c.ensure_env_file()))").strip()
        assert Path(written) == tmp_path / ".env"
        assert _norm((tmp_path / ".env").read_text(encoding="utf-8")) == _example()

    def test_it_leaves_an_existing_env_untouched(self, tmp_path):
        """It runs on every start, so overwriting here would destroy credentials."""
        existing = tmp_path / ".env"
        existing.write_text(SENTINEL, encoding="utf-8")
        assert "None" in _run(tmp_path, "print('<<RESULT>>' + str(c.ensure_env_file()))")
        assert existing.read_text(encoding="utf-8") == SENTINEL

    def test_env_file_resolves_to_the_project_home_itself(self, tmp_path):
        """Not a config/ subfolder -- the home stays a flat folder the user owns."""
        resolved = Path(_run(tmp_path, "print('<<RESULT>>' + str(c.ENV_FILE))").strip())
        assert resolved == tmp_path / ".env"


class TestTheTemplatesPlaceholders:
    """The second run, with the template written but not filled in yet.

    Its placeholders were loaded as real values: "API key configured", then
    int("your_list_id_here") crashed startup as "Unexpected error ... This is
    a bug".
    """

    @pytest.fixture(autouse=True)
    def _no_inherited_credentials(self, monkeypatch):
        """Importing config loaded the developer's own .env into os.environ; the child must not see it."""
        for name in [k for k in os.environ if k.startswith("TMDB_") and k != HOME_VAR]:
            monkeypatch.delenv(name)

    def test_placeholders_read_as_unset(self, tmp_path):
        _run(tmp_path, "c.ensure_env_file(); print('<<RESULT>>')")
        assert "TMDB_LIST_ID=your_list_id_here" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert _run(tmp_path, "print('<<RESULT>>' + repr((c.TMDB_API_KEY, c.TMDB_LIST_ID)))") == "('', '')"

    def test_real_values_are_kept(self, tmp_path):
        (tmp_path / ".env").write_text("TMDB_API_KEY=abc123\nTMDB_LIST_ID=8678795\n", encoding="utf-8")
        assert _run(tmp_path, "print('<<RESULT>>' + repr((c.TMDB_API_KEY, c.TMDB_LIST_ID)))") == "('abc123', '8678795')"

    @pytest.mark.parametrize(
        "value",
        [
            "https://www.themoviedb.org/list/8678795",
            "https://www.themoviedb.org/list/8678795-my-watched-films?page=2",
            "www.themoviedb.org/list/8678795",
        ],
    )
    def test_a_pasted_list_url_gives_its_number(self, tmp_path, value):
        (tmp_path / ".env").write_text(f"TMDB_LIST_ID={value}\n", encoding="utf-8")
        assert _run(tmp_path, "print('<<RESULT>>' + c.TMDB_LIST_ID)") == "8678795"

    def test_the_second_run_explains_instead_of_crashing(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TMDB_")}
        env.update({HOME_VAR: str(tmp_path), "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"})
        runs = [
            subprocess.run(
                [sys.executable, str(REPO_ROOT / "main.py")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=str(tmp_path),
                stdin=subprocess.DEVNULL,
                timeout=60,
            )
            for _ in range(2)
        ]
        first, second = runs
        assert "Created a credentials file" in first.stdout
        assert second.returncode == 0, second.stdout + second.stderr
        assert "TMDB_API_KEY is not set" in second.stdout
        assert "This is a bug" not in second.stdout
