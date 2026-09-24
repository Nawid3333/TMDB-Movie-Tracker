"""Check the live TMDB API for changes that would break this tracker.

Run monthly by .github/workflows/site-check.yml, which opens an issue when a
check fails, comments on it while it stays open, and closes it once a run
passes again. Also runnable by hand from the project root:

    python tests/site_check.py [--report FILE]

Every check runs this tracker's own code or reads exactly what it reads: the
fields _enrich_one takes from a movie's details, the certification picker,
the collection lookup the gaps report is built on, the poster CDN, and
fetch_list on your list. So a check fails when the tracker itself would
quietly store less, not merely when a response looks different.

Needs TMDB_API_KEY; without it every check is skipped. TMDB_LIST_ID adds the
list check. Nothing is ever changed: every request is a read.

The report carries check names, status codes and counts only. It never
includes an exception's text either, because an httpx error can carry the
request URL, and that URL carries the API key.

Exit status: 0 every check passed; 1 a check failed, so the API changed in a
way this tracker depends on; 2 nothing failed, but something could not be
checked (API down, or this network blocked); 3 the check itself crashed.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.config as _config  # noqa: E402
from src import enrich, list_fetcher, posters  # noqa: E402
from src.tmdb_api import TMDBClient, pick_certification  # noqa: E402

PASS, FAIL, UNREACHABLE, SKIPPED = "pass", "fail", "unreachable", "skipped"
_MARK = {PASS: "✅ pass", FAIL: "❌ fail", UNREACHABLE: "⚠️ unreachable", SKIPPED: "➖ skipped"}

# The Lord of the Rings: The Fellowship of the Ring. Credited, certified in
# many regions, and part of a collection, so one lookup exercises every
# field _enrich_one reads, and the collection check has a collection to load.
PROBE_MOVIE = 120
KEY_REFUSED = "HTTP 401: the API key was refused -- check the TMDB_API_KEY secret"


@dataclass
class Result:
    check: str
    status: str
    detail: str


def http_failure(check: str, exc: httpx.HTTPError) -> Result:
    """A failed request, described without its text (which may carry the key)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or code >= 500:
            return Result(check, UNREACHABLE, f"HTTP {code}")
        return Result(check, FAIL, f"HTTP {code}")
    return Result(check, UNREACHABLE, type(exc).__name__)


def status_failure(check: str, resp: httpx.Response) -> Result | None:
    """A response the tracker would not read, or None if it would."""
    if resp.status_code == 401:
        return Result(check, FAIL, KEY_REFUSED)
    if resp.status_code >= 400:
        return Result(check, FAIL, f"HTTP {resp.status_code}")
    return None


def movie_field_problems(movie: object) -> list[str]:
    """What _enrich_one would silently store as empty from these details."""
    if not isinstance(movie, dict):
        return [f"details are a {type(movie).__name__}, not an object"]
    problems = []
    for field in ("title", "release_date", "status", "poster_path"):
        if not isinstance(movie.get(field), str) or not movie.get(field):
            problems.append(f"{field} missing")
    genres = movie.get("genres")
    if not isinstance(genres, list) or not all(isinstance(g, dict) and g.get("name") for g in genres) or not genres:
        problems.append("genres missing or without names")
    credits = movie.get("credits")
    crew = credits.get("crew") if isinstance(credits, dict) else None
    cast = credits.get("cast") if isinstance(credits, dict) else None
    if not isinstance(cast, list) or not cast:
        problems.append("credits.cast missing, so no cast would be stored")
    if not isinstance(crew, list) or not any(isinstance(p, dict) and p.get("job") == "Director" for p in crew):
        problems.append("no Director in credits.crew, so no director would be stored")
    keywords = movie.get("keywords")
    if not isinstance(keywords, dict) or not isinstance(keywords.get("keywords"), list) or not keywords["keywords"]:
        problems.append("keywords.keywords missing, so the gaps report would find no franchises")
    external = movie.get("external_ids")
    if not isinstance(external, dict) or not external.get("imdb_id"):
        problems.append("external_ids.imdb_id missing")
    release_dates = movie.get("release_dates")
    results = release_dates.get("results") if isinstance(release_dates, dict) else None
    countries = [c.get("iso_3166_1") for c in movie.get("production_countries") or [] if isinstance(c, dict)]
    if not isinstance(results, list) or pick_certification(results, countries[0] if countries else None) is None:
        problems.append("no certification could be picked from release_dates")
    collection = movie.get("belongs_to_collection")
    if not isinstance(collection, dict) or not collection.get("id"):
        problems.append("belongs_to_collection missing, so no collection gaps would be found")
    return problems


def check_movie(client: TMDBClient) -> tuple[list[Result], dict | None]:
    params = {"language": _config.TMDB_LANGUAGE, "append_to_response": enrich._APPEND_TO_RESPONSE}
    try:
        resp = client.get(f"/movie/{PROBE_MOVIE}", params=params, auth=False)
    except httpx.HTTPError as exc:
        return [http_failure("movie details", exc)], None
    failure = status_failure("movie details", resp)
    if failure:
        return [failure], None
    try:
        movie = resp.json()
    except ValueError:
        return [Result("movie details", FAIL, "response is not JSON")], None
    problems = movie_field_problems(movie)
    if problems:
        return [Result("movie details", FAIL, "; ".join(problems))], None
    return [Result("movie details", PASS, f"movie {PROBE_MOVIE}: every field the enrichment reads")], movie


def check_collection(client: TMDBClient, movie: dict) -> Result:
    collection_id = movie["belongs_to_collection"]["id"]
    # _fetch_collection swallows every failure into None, as the enrichment
    # wants; ask once directly first so a down API is not called a change.
    try:
        resp = client.get(f"/collection/{collection_id}")
    except httpx.HTTPError as exc:
        return http_failure("collection", exc)
    failure = status_failure("collection", resp)
    if failure:
        return failure
    collection = enrich._fetch_collection(client, collection_id)
    parts = (collection or {}).get("parts") or []
    if not any(p.get("id") == PROBE_MOVIE for p in parts):
        return Result("collection", FAIL, f"{len(parts)} part(s), and the probe movie is not among them")
    return Result("collection", PASS, f"{len(parts)} part(s)")


def check_poster(movie: dict) -> Result:
    url = posters._poster_url(movie["poster_path"])
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as http:
            resp = http.get(url)
    except httpx.HTTPError as exc:
        return Result("poster", UNREACHABLE, type(exc).__name__)
    if resp.status_code == 429 or resp.status_code >= 500:
        return Result("poster", UNREACHABLE, f"HTTP {resp.status_code}")
    if resp.status_code >= 400 or not resp.headers.get("content-type", "").startswith("image/"):
        return Result("poster", FAIL, f"HTTP {resp.status_code}, {resp.headers.get('content-type') or 'no type'}")
    return Result("poster", PASS, f"{len(resp.content) // 1024} KB image")


def check_list(client: TMDBClient, list_id: str) -> Result:
    # fetch_list turns every failure into incomplete=True, as the fast scan
    # wants; read the first page directly first to tell a down API apart.
    try:
        resp = client.get(f"/list/{list_id}", params={"page": 1}, auth=False)
    except httpx.HTTPError as exc:
        return http_failure("list", exc)
    if resp.status_code in (401, 404):
        return Result("list", FAIL, f"HTTP {resp.status_code}: the list is private, gone, or the id changed")
    failure = status_failure("list", resp)
    if failure:
        return failure
    with tempfile.TemporaryDirectory() as tmp:
        items, incomplete = list_fetcher.fetch_list(
            client, list_id, cache_path=Path(tmp, "list.json"), use_session_on_private=False
        )
    if incomplete:
        return Result("list", FAIL, f"fetch incomplete after {len(items)} item(s): no removal would ever be offered")
    if not items:
        return Result("list", FAIL, "no items read")
    unread = [i for i in items if not (isinstance(i, dict) and i.get("id") and i.get("media_type"))]
    if unread:
        return Result("list", FAIL, f"{len(unread)} of {len(items)} item(s) without id or media_type")
    return Result("list", PASS, f"{len(items)} item(s), complete")


def run_checks() -> list[Result]:
    if not _config.TMDB_API_KEY:
        return [Result("API", SKIPPED, "set TMDB_API_KEY (and TMDB_LIST_ID) to check the API")]
    with TMDBClient(api_key=_config.TMDB_API_KEY) as client:
        results, movie = check_movie(client)
        if results[0].detail == KEY_REFUSED:
            # Every other request would be refused the same way.
            return results
        if movie is not None:
            results.append(check_collection(client, movie))
            results.append(check_poster(movie))
        if _config.TMDB_LIST_ID:
            results.append(check_list(client, _config.TMDB_LIST_ID))
        else:
            results.append(Result("list", SKIPPED, "set TMDB_LIST_ID to check your list"))
    return results


def exit_code(results: list[Result]) -> int:
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return 1
    if UNREACHABLE in statuses:
        return 2
    return 0


def render(results: list[Result], today: date) -> str:
    lines = [
        f"### Site check: api.themoviedb.org, {today.isoformat()}",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.check} | {_MARK[r.status]} | {r.detail.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the live TMDB API for changes that would break this tracker.")
    ap.add_argument("--report", help="also write the markdown report to this file")
    args = ap.parse_args(argv)
    try:
        results = run_checks()
        report, code = render(results, date.today()), exit_code(results)
    except Exception:  # noqa: BLE001 -- reported as a crash, not mistaken for a failed check
        traceback.print_exc()
        report, code = "### Site check crashed\n\nSee the workflow log for the traceback.\n", 3
    print(report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main_cli())
