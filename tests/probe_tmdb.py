"""Probe live TMDB API to collect fixture data. Run once, not part of test suite.

This module is intentionally NOT collected by pytest. It hits the live TMDB API
and should only be run manually when fixtures need refreshing.
"""

import json
import logging
from pathlib import Path

from config.config import TMDB_API_KEY, TMDB_LIST_ID
from src.tmdb_api import TMDBClient

logger = logging.getLogger(__name__)

TESTS_DATA_DIR = Path(__file__).resolve().parent / "data"
TESTS_DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = TESTS_DATA_DIR / "session.json"
APPEND = "credits,keywords,external_ids,release_dates,videos,watch/providers,recommendations,similar"


def main() -> None:
    logger.warning("probe_tmdb.py is a manual fixture-capture script, not a pytest test.")
    client = TMDBClient(SESSION_FILE, api_key=TMDB_API_KEY)

    # 1. List probe
    resp = client.get(f"/list/{TMDB_LIST_ID}", params={"page": 1}, auth=False)
    data = resp.json()
    (TESTS_DATA_DIR / f"list_{TMDB_LIST_ID}_probe.json").write_text(
        json.dumps(
            {"status": resp.status_code, "headers": dict(resp.headers), "body": data},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("list status", resp.status_code)

    ids = []
    for item in data.get("items", [])[:10]:
        m = item if item.get("media_type") == "movie" else item
        ids.append(int(m["id"]))
    if not ids:
        ids = [550, 27205, 155, 13, 671]

    for mid in ids[:5]:
        try:
            r = client.get(
                f"/movie/{mid}",
                params={"language": "de-DE", "append_to_response": APPEND},
                auth=False,
            )
            (TESTS_DATA_DIR / f"movie_{mid}_probe.json").write_text(
                json.dumps(
                    {"status": r.status_code, "headers": dict(r.headers), "body": r.json()},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print("movie", mid, r.status_code)
        except Exception as exc:
            print("movie", mid, "err", exc)

    # 3. Search probe
    r = client.get(
        "/search/movie",
        params={"query": "Inception", "language": "de-DE", "page": 1, "include_adult": "false"},
        auth=False,
    )
    (TESTS_DATA_DIR / "search_inception_probe.json").write_text(
        json.dumps(
            {"status": r.status_code, "headers": dict(r.headers), "body": r.json()},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("search inception", r.status_code)

    # 4. Find imdb id
    r = client.get(
        "/find/tt1375666",
        params={"external_source": "imdb_id", "language": "de-DE"},
        auth=False,
    )
    (TESTS_DATA_DIR / "find_imdb_probe.json").write_text(
        json.dumps(
            {"status": r.status_code, "headers": dict(r.headers), "body": r.json()},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("find imdb", r.status_code)

    # 5. Collection probes
    coll_ids = []
    for item in data.get("items", [])[:20]:
        m = item if item.get("media_type") == "movie" else item
        if m.get("id"):
            try:
                rr = client.get(f"/movie/{m['id']}", params={"language": "de-DE"}, auth=False)
                coll = rr.json().get("belongs_to_collection")
                if coll:
                    coll_ids.append(coll["id"])
            except Exception:
                pass

    for cid in list(dict.fromkeys(coll_ids))[:2]:
        try:
            r = client.get(f"/collection/{cid}", auth=False)
            (TESTS_DATA_DIR / f"collection_{cid}_probe.json").write_text(
                json.dumps(
                    {"status": r.status_code, "headers": dict(r.headers), "body": r.json()},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print("collection", cid, r.status_code)
        except Exception as exc:
            print("collection", cid, "err", exc)

    print("done")


if __name__ == "__main__":
    main()
