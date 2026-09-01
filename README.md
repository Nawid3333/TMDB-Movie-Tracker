# TMDB Movie Tracker

A small, terminal-driven Python tool that keeps a local mirror of a TMDB custom list, enriched with cast, crew, collections, keywords, certifications, recommendations and connected TV shows. It is built for people who want fast offline lookups, franchise-gap discovery and safe, approved-only updates.

> **Status:** personal project, tested on Windows with Python 3.14. It builds as
> a wheel and installs as a `movie-tracker` command; running it from a clone is
> still the path most people should take. Contributions are welcome.

---

## What it does

- **Fast scan** — fetches the current TMDB list, diffs it against the local index and lets you approve additions/removals before saving. This is the quickest way to keep the index in sync.
- **Full scan** — re-enriches every movie with full TMDB details (credits, keywords, release dates, watch providers, recommendations, similar, collection info, connected TV).
- **Franchise gaps** — finds films in shared collections and connected TV series via keywords that are not yet in your index.
- **Search & add** — accepts a title, TMDB URL/ID or IMDb URL/ID and adds the movie locally (and optionally pushes it to the remote TMDB list when a session is available).
- **Poster rendering** — downloads and renders inline posters for iTerm2, Kitty, Windows Terminal or block-art terminals (configurable, falls back to off).
- **Atomic writes & backups** — every JSON write is atomic via temp-file + `fsync` + `os.replace`, with automatic `.bak1`/`.bak2`/`.bak3` rotation.

---

## Quick start

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd "TMDB Movie Tracker"   # the folder name contains a space
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` only pins runtime dependencies:

- `httpx>=0.27.0`
- `python-dotenv>=1.0.0`

Tests additionally need `pytest` and `respx`. They can be installed with:

```bash
pip install -r requirements.txt
pip install pytest respx ruff
```

### 2. Configure your environment

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

```dotenv
TMDB_API_KEY=your_tmdb_api_key_here
TMDB_LIST_ID=your_list_id_here

# Optional — needed for private lists and remote list writes.
TMDB_SESSION_ID=
TMDB_V4_ACCESS_TOKEN=
TMDB_USERNAME=
TMDB_PASSWORD=

# Region / language defaults
TMDB_LANGUAGE=de-DE
TMDB_FALLBACK_REGION=DE
```

- `TMDB_API_KEY` is required. Get one from <https://www.themoviedb.org/settings/api>.
- `TMDB_LIST_ID` is the numeric id of the TMDB custom list you want to track.
- Leave auth fields empty if you only need public-list reads. The app will fall back to an interactive browser approval flow when a session is needed, and will print the new session id for you to paste into `.env`.

### 3. Run

```bash
python main.py
```

```text
╔════════════════════════════════════════════════════════════════╗
║ TMDB Movie Tracker                                             ║
║ Track your watched films from a TMDB custom list               ║
╚════════════════════════════════════════════════════════════════╝

✓ API key configured
  → Index loaded: 74 movies
  → List ID: 8678795
  ✓ TMDB session available

Menu
  ────────────────────────────────────────────────────
  1. Fast scan       — list membership only (quick)
  2. Full scan       — every detail (slow, accurate)
  3. Franchise gaps  — connected films and TV you missed
  0. Exit
Enter your choice (0-3):
```

---

### 4. Optional: install it as a command

Building a wheel puts a `movie-tracker` command on your PATH:

```bash
pip install build
python -m build
pip install dist/tmdb_movie_tracker-1.0.0-py3-none-any.whl
```

Two things are worth knowing before you do.

**Give each program its own virtual environment.** This project ships its code
as the top-level modules `main`, `src` and `config`, as do its sibling projects.
Install two of them into the same environment and the second overwrites the
first — the command still exists, but it silently runs the other program.
`pipx` creates an isolated environment per application and avoids this entirely:

```bash
pipx install .
```

**Tell it where to keep your files.** Once installed, the package lives inside
`site-packages`, which is no place to keep a `.env` you have to edit by hand.
Point `TMDB_HOME` at a folder you own, and `.env`, `data/` (index, details,
posters, caches) and `logs/` all move there:

```bash
export TMDB_HOME=~/movie-tracker              # Linux / macOS
$env:TMDB_HOME = "$HOME\movie-tracker"        # Windows (PowerShell)

mkdir -p ~/movie-tracker
cp .env.example ~/movie-tracker/.env
```

`TMDB_HOME` has to be a real environment variable. It cannot be set inside
`.env`, because it is what tells the program where to find that file in the
first place. Left unset it resolves to the checkout, which is why running from
a clone needs no configuration at all.

## How auth works

When a private-list read or remote list write is needed, the client tries, in order:

1. Already-resolved session id.
2. `TMDB_SESSION_ID` from `.env`.
3. `TMDB_V4_ACCESS_TOKEN` conversion.
4. Username + password login.
5. Interactive browser approval (opens `https://www.themoviedb.org/authenticate/<token>` and waits for Enter).

Once approved, TMDB sessions are long-lived. The new session id is printed to the terminal so you can add it to `.env` as `TMDB_SESSION_ID=<id>` and reuse it across runs. Sessions are no longer stored in a separate JSON file.

---

## Project layout

```text
TMDB Movie Tracker/
├── config/
│   └── config.py              # Environment, paths, tunables
├── src/
│   ├── atomic_io.py           # Atomic JSON writes with backups
│   ├── changes.py             # Diff + approved merge for fast scan
│   ├── enrich.py              # Full-scan enrichment engine
│   ├── gaps.py                # Franchise / connected-TV discovery
│   ├── index.py               # Index + details persistence helpers
│   ├── list_fetcher.py        # Paginated TMDB list fetch + cache
│   ├── posters.py             # Poster download / terminal rendering
│   ├── search.py              # Search / add / push to TMDB list
│   ├── tmdb_api.py            # TMDB client, rate limiter, auth ladder
│   └── ui/                    # Terminal widgets, prompts, reports
├── tests/                     # pytest suite
├── data/                      # Runtime data (ignored by git)
│   ├── index.json             # Membership records
│   ├── details.json           # Enriched details
│   └── posters/               # Downloaded poster cache
├── logs/                      # Rotating logs (ignored by git)
├── main.py                    # CLI entry point
├── .env.example               # Template for secrets
└── README.md                  # You are here
```

---

## Tests

The test suite uses captured real TMDB fixtures. Because those fixtures contain copyrighted TMDB data, they are **not committed**; the repository stores only the scripts that generate them.

### Generate fixtures (requires a real TMDB API key)

```bash
python tests/probe_tmdb.py
python tests/capture_fixtures.py
```

This writes captured responses to `tests/fixtures/generated/` and probe data to `tests/data/`.

### Run the suite

```bash
python -m pytest tests/ -q
```

### Lint and format

```bash
python -m ruff check .
python -m ruff format --check .
```

---

## Configuration tunables

All values below can be set via `.env`:

| Variable                       | Default | Purpose                                              |
| ------------------------------ | ------- | ---------------------------------------------------- |
| `TMDB_LANGUAGE`                | `de-DE` | Primary language for titles and metadata             |
| `TMDB_FALLBACK_REGION`         | `DE`    | Certification fallback region                        |
| `TMDB_DETAIL_WORKERS`          | `16`    | Parallel enrichment workers                          |
| `TMDB_MAX_REQUESTS_PER_SECOND` | `30`    | Global request rate limit                            |
| `TMDB_READ_MAX_RETRIES`        | `3`     | Retries for transient API failures                   |
| `TMDB_WARM_DAYS`               | `90`    | Re-enrich "warm" movies after this many days         |
| `TMDB_COOL_DAYS`               | `730`   | Days until a released movie becomes "cool"           |
| `TMDB_COLD_REENRICH_DAYS`      | `90`    | Re-enrich "cold" movies after this many days         |
| `TMDB_MIN_SHRINK_RATIO`        | `0.5`   | Shrink sanity gate for fast-scan removals            |
| `POSTER_MODE`                  | `auto`  | `auto`, `iterm`, `kitty`, `sixel`, `blocks` or `off` |
| `POSTER_SIZE`                  | `w342`  | TMDB poster size to download                         |

---

One variable is deliberately not in that table: `TMDB_HOME` decides where
`.env`, `data/` and `logs/` live, so it cannot itself be read from `.env`. Unset,
it resolves to this checkout; set it when you install the package so those do not
land in site-packages.

## Privacy & security notes

- **Never commit `.env`.** It is already ignored by `.gitignore`.
- **No real credentials are in the source code.** `config/config.py` reads everything from environment variables.
- **No personal data is in the repository.** Commit history uses a GitHub no-reply email (`102778966+Nawid3333@users.noreply.github.com`) and project files contain no real names, addresses or private keys.
- **Runtime data is ignored.** `data/`, `logs/`, `tests/data/`, `tests/fixtures/` and `*.json` are excluded from git.
- **Password auth is optional.** If you do not want to store `TMDB_USERNAME`/`TMDB_PASSWORD`, leave them empty and use the browser approval flow.
- **Sessions come from `.env`.** `TMDB_SESSION_ID` is read from `.env` at import time. If you create a new session via the browser approval flow, the terminal prints the session id for you to paste into `.env`.

---

## Public-readiness checklist

Before publishing or sharing the repository, make sure:

- [x] A `LICENSE` file is present (GPLv3).
- [x] `.env` is not committed and `.env.example` contains only placeholder values.
- [x] `data/`, `logs/`, `tests/data/` and `tests/fixtures/generated/` are empty of personal data before pushing.
- [x] Git history does not contain real emails or secrets (`git log --format='%ae'`).
- [x] You are comfortable sharing the TMDB list id shown in screenshots or logs, or you redact it.
- [x] (Optional) Add a `pyproject.toml` or `setup.py` to make the project installable.

---

## License

This project is licensed under the **GNU General Public License v3.0 or later** — the same license used for the rest of this repository's Python tools.
See [LICENSE](LICENSE) for the full text.

---

## Roadmap / known limitations

- The tool is currently a single-user, local CLI. Multi-user or web use would require a real database and auth backend.
- Poster rendering relies on terminal emulator support.
- Packaging (`pip install`) is implemented via `pyproject.toml`; run `pip install -e .` to install the `movie-tracker` console script.
- CI/CD, automated releases and pre-built binaries are not provided.
- Franchise-gap detection is heuristic-based; false positives happen for generic keywords.

---

Happy tracking! 🎬
