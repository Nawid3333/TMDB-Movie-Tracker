"""TMDB Movie Tracker — terminal menu entry point."""

import json
import logging
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta

import config.config as _config
from config.config import (
    DEFAULT_BATCH_FILE,
    GAPS_FILE,
    LOG_FILE,
    MIN_SHRINK_RATIO,
    MISMATCH_REPORT_FILE,
    bootstrap,
    ensure_env_file,
    setup_logging,
)
from src.changes import apply_changes, detect_changes
from src.enrich import run_full_scan as enrich_run_full_scan
from src.gaps import find_gaps, load_gaps
from src.index import load_index, now_iso, save_index
from src.list_fetcher import ListFetchError, fetch_list
from src.search import (
    SearchError,
    push_to_tmdb_list,
    resolve_movie_id_from_input,
    resolve_movie_ids_from_file,
)
from src.tmdb_api import TMDBClient
from src.ui import prompts, reports, term
from src.ui.reports import title_line

log = logging.getLogger(__name__)


def print_header() -> None:
    """Print the application banner."""
    print()
    for line in term.box(
        [
            term.style("TMDB Movie Tracker", term._T.BOLD, term._T.CYAN),
            "Track your watched films from a TMDB custom list",
        ],
        width=64,
    ):
        print(line)
    print()


def _format_host_rows(hosts):
    """Return a list of table-formatted host status lines.

    hosts is a list of (label, status, count, idx_count, compare_txt) tuples.
    Adapted from the aniworld scraper layout; the third column is Movies here.
    """
    if not hosts:
        return []

    term_w = max(shutil.get_terminal_size().columns, 80)
    arrow_gap = "  "

    labels = ["Host", "Status", "Movies", "Index", "Compare"]
    cols = {
        "host": max([len(str(label)) for label, *_ in hosts] + [len(labels[0])]),
        "status": max([len("OK" if status else "FAILED") for _, status, *_ in hosts] + [len(labels[1])]),
        "movies": max([len(f"{count:,}") if count is not None else 1 for _, _, count, *_ in hosts] + [len(labels[2])]),
        "index": max(
            [len(f"{idx_count:,}") if idx_count is not None else 1 for _, _, _, idx_count, *_ in hosts]
            + [len(labels[3])]
        ),
        "compare": max(
            [len(str(compare_txt)) if compare_txt is not None else 1 for *_, compare_txt in hosts] + [len(labels[4])]
        ),
    }

    total = sum(cols.values()) + len(labels) * len(arrow_gap)
    if total > term_w:
        excess = total - term_w
        trimmable = cols["host"] - len(labels[0]) + cols["compare"] - len(labels[4])
        if trimmable > 0:
            factor = min(excess / trimmable, 1.0)
            cols["host"] = max(
                len(labels[0]),
                int(cols["host"] - (cols["host"] - len(labels[0])) * factor),
            )
            cols["compare"] = max(
                len(labels[4]),
                int(cols["compare"] - (cols["compare"] - len(labels[4])) * factor),
            )

    def _trunc(text, width):
        text = str(text)
        return text if len(text) <= width else text[: width - 1] + "…"

    sep_parts = ["─" * cols["host"]] + ["─" * cols[key] for key in ["status", "movies", "index", "compare"]]

    lines = [
        arrow_gap
        + "  ".join(
            [
                f"{_trunc(labels[0], cols['host']):<{cols['host']}}",
                f"{labels[1]:<{cols['status']}}",
                f"{labels[2]:<{cols['movies']}}",
                f"{labels[3]:<{cols['index']}}",
                f"{labels[4]:<{cols['compare']}}",
            ]
        ),
        arrow_gap + "  ".join(sep_parts),
    ]

    for label, status, count, idx_count, compare_txt in hosts:
        status_txt = "OK" if status else "FAILED"
        count_txt = f"{count:,}" if count is not None else "-"
        idx_txt = f"{idx_count:,}" if idx_count is not None else "-"
        cmp_txt = compare_txt if compare_txt is not None else "-"
        lines.append(
            arrow_gap
            + "  ".join(
                [
                    f"{_trunc(label, cols['host']):<{cols['host']}}",
                    f"{status_txt:<{cols['status']}}",
                    f"{count_txt:<{cols['movies']}}",
                    f"{idx_txt:<{cols['index']}}",
                    f"{_trunc(cmp_txt, cols['compare']):<{cols['compare']}}",
                ]
            )
        )
    return lines


def show_menu() -> None:
    """Print the main menu."""
    print()
    print(term.style("Menu", term._T.BOLD, term._T.CYAN))
    print("  " + "─" * 52)
    print("  1. Full scan       — every detail (slow, accurate)")
    print("  2. Fast scan       — list membership only (quick)")
    print("  3. Franchise gaps  — connected films and TV you missed")
    print("  4. Push URL file   — push URLs/IDs to remote list without adding locally")
    print("  0. Exit")


def run_full_scan(client: TMDBClient) -> None:
    """Run a full enrichment scan over the local index."""
    log.debug("Full scan selected")
    print()
    print(term.style("→ Full scan", term._T.BOLD, term._T.CYAN))
    enrich_run_full_scan(client, force=False, resume=True)


def run_fast_scan(client: TMDBClient) -> None:
    """Fetch list membership, diff against index, confirm, and save."""
    log.debug("Starting fast scan...")
    print()
    print(term.style("→ Fast scan", term._T.BOLD, term._T.CYAN))
    index = load_index()

    try:
        items, incomplete = fetch_list(client, _config.TMDB_LIST_ID)
    except ListFetchError as exc:
        log.error("Fast scan failed: %s", exc)
        print(term.style("✗ Could not fetch the list:", term._T.RED), str(exc))
        return

    change_set = detect_changes(
        index.get("movies", {}),
        items,
        incomplete=incomplete,
        min_shrink_ratio=MIN_SHRINK_RATIO,
    )
    reports.render_change_report(change_set)

    if not change_set.additions and not change_set.removals:
        index["last_fast_scan"] = now_iso()
        save_index(index)
        log.debug("Fast scan complete; no changes")
        print(term.style("✓ Index is up to date.", term._T.GREEN))
        return

    approve_additions = False
    if change_set.additions:
        titles = [title_line(r) for r in change_set.additions.values()]
        approve_additions = prompts.confirm_category("Additions", titles, default=True)

    approve_removals = False
    if change_set.removals:
        titles = [title_line(r) for r in change_set.removals.values()]
        approve_removals = prompts.confirm_category("Removals", titles, default=False)

    if not approve_additions and not approve_removals:
        print()
        print(term.style("  ⚠ No changes approved.", term._T.YELLOW), "Index was not modified.")
        log.debug("Fast scan: user approved no changes")
        return

    print()
    print("Summary of changes to save:")
    if change_set.additions:
        print(f"  + Additions: {len(change_set.additions) if approve_additions else 0}")
    if change_set.removals:
        print(f"  - Removals:  {len(change_set.removals) if approve_removals else 0}")

    if not prompts.confirm("Save these changes?", default=False):
        print()
        print(term.style("  ⚠ Changes discarded.", term._T.YELLOW))
        log.debug("Fast scan: user cancelled save")
        return

    merged = apply_changes(
        index.get("movies", {}),
        change_set,
        approve_additions=approve_additions,
        approve_removals=approve_removals,
    )

    index["movies"] = merged
    index["last_fast_scan"] = now_iso()
    index["meta"]["incomplete_last_scan"] = change_set.incomplete
    save_index(index)

    added = len(change_set.additions) if approve_additions else 0
    removed = len(change_set.removals) if approve_removals else 0
    log.debug(
        "Fast scan saved: +%d additions, -%d removals, incomplete=%s",
        added,
        removed,
        change_set.incomplete,
    )
    print(term.style("✓ Index saved.", term._T.GREEN))
    if change_set.incomplete:
        print(term.style("  ⚠ The list fetch was incomplete.", term._T.YELLOW))
        print("    Removal proposals are blocked until a complete scan succeeds.")

    # Always show the mismatch counter after a fast scan.
    _render_mismatch_summary(index, items, set())

    # Backend vanished cleanup: prompt when local movies are missing from the live list.
    _prompt_clean_vanished(client, items, index)


def _prompt_clean_vanished(
    client: TMDBClient,
    items: list[dict],
    index: dict | None = None,
) -> None:
    """Detect index movies missing from the live list and ask to delete or rescrape."""
    remote_list_id = _config.TMDB_LIST_ID
    if not remote_list_id:
        return

    if index is None:
        index = load_index()
    local_movies = index.get("movies", {})
    live_ids = {int(item.get("id", 0)) for item in items if item.get("id")}
    local_ids = {int(mid) for mid in local_movies}
    missing_ids = sorted(local_ids - live_ids)

    if not missing_ids:
        return

    print()
    print(term.style(f"⚠ {len(missing_ids)} movie(s) in index but missing from the list:", term._T.YELLOW))
    for movie_id in missing_ids[:10]:
        movie = local_movies.get(str(movie_id), {})
        print(f"    - {title_line(movie)}")
    if len(missing_ids) > 10:
        print(f"    ... and {len(missing_ids) - 10} more")

    bulk = input("\nDelete all these vanished entries? (y/n): ").strip().lower()
    if bulk == "y":
        removed = 0
        for movie_id in missing_ids:
            key = str(movie_id)
            if key in local_movies:
                del local_movies[key]
                removed += 1
        index["movies"] = local_movies
        save_index(index)
        print(term.style(f"\n✓ Removed {removed} vanished movie(s) from the index.", term._T.GREEN))
        return
    if bulk == "n":
        print("  → Skipped vanish cleanup")
        return

    rescrape_ids: list[int] = []
    removed = 0
    for movie_id in missing_ids:
        movie = local_movies.get(str(movie_id), {})
        print(f"\n{title_line(movie)}")
        print("  1. Delete from local index")
        print("  2. Rescrape / re-add to TMDB list")
        print("  3. Skip")
        choice = input("Choice (1/2/3): ").strip()
        if choice == "1":
            local_movies.pop(str(movie_id), None)
            removed += 1
        elif choice == "2":
            rescrape_ids.append(movie_id)
        else:
            print("  → Skipped")

    if removed:
        index["movies"] = local_movies
        save_index(index)
        print(term.style(f"\n✓ Removed {removed} movie(s) from the index.", term._T.GREEN))

    if rescrape_ids:
        if not client.session_id:
            print(term.style("\n✗ No TMDB session; cannot re-add movies to the list.", term._T.RED))
            return
        print()
        print(f"  Re-adding {len(rescrape_ids)} movie(s) to TMDB list {remote_list_id}...")
        ok = 0
        failed = 0
        for movie_id in rescrape_ids:
            result = push_to_tmdb_list(client, remote_list_id, movie_id)
            if result["success"]:
                ok += 1
                membership = local_movies.get(str(movie_id), {})
                membership["remote_push"] = "rescraped"
            else:
                failed += 1
                print(f"  ⚠ Could not re-add {title_line(local_movies.get(str(movie_id), {}))}: {result}")
        save_index(index)
        print(f"  Re-add result: {ok} ok, {failed} failed")

        if prompts.confirm("\nVerify against the live TMDB list now?", default=True):
            _fetch_and_summarize_mismatches(client, added_ids=set(rescrape_ids))


def _save_mismatch_report(
    index: dict,
    items: list[dict],
    added_ids: set[int],
) -> None:
    """Write a mismatch report JSON, or remove it when no mismatch remains."""
    live_ids = {int(item.get("id", 0)) for item in items if item.get("id")}
    local_movies = index.get("movies", {})
    local_ids = {int(mid) for mid in local_movies}

    missing_ids = sorted(local_ids - live_ids)
    extra_ids = sorted(live_ids - local_ids)

    if not missing_ids and not extra_ids:
        if MISMATCH_REPORT_FILE.exists():
            MISMATCH_REPORT_FILE.unlink()
            log.debug("Mismatch report removed: all clear")
        return

    def _url(movie_id: int) -> str:
        return f"https://www.themoviedb.org/movie/{movie_id}"

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "list_id": _config.TMDB_LIST_ID,
        "index_total": len(local_ids),
        "list_total": len(live_ids),
        "matched": len(local_ids & live_ids),
        "missing_from_list_count": len(missing_ids),
        "extra_on_list_count": len(extra_ids),
        "missing_from_list": [
            {
                "id": movie_id,
                "title": title_line(local_movies.get(str(movie_id), {})),
                "url": _url(movie_id),
            }
            for movie_id in missing_ids
        ],
        "extra_on_list": [
            {
                "id": movie_id,
                "title": title_line(item),
                "url": _url(movie_id),
            }
            for movie_id in extra_ids
            for item in items
            if int(item.get("id", 0)) == movie_id
        ],
        "added_ids": sorted(added_ids),
    }

    try:
        MISMATCH_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MISMATCH_REPORT_FILE, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        log.debug(
            "Mismatch report saved: %d missing, %d extra",
            len(missing_ids),
            len(extra_ids),
        )
    except OSError as exc:
        log.warning("Could not write mismatch report: %s", exc)


def _render_mismatch_summary(
    index: dict,
    items: list[dict],
    added_ids: set[int],
) -> None:
    """Compare local index against the live TMDB list and print a mismatch counter."""
    _save_mismatch_report(index, items, added_ids)

    live_ids = {int(item.get("id", 0)) for item in items if item.get("id")}
    local_ids = {int(mid) for mid in index.get("movies", {})}

    missing_from_list = sorted(local_ids - live_ids)
    extra_on_list = sorted(live_ids - local_ids)
    matched = sorted(local_ids & live_ids)

    print()
    print(term.style("Mismatch report", term._T.BOLD, term._T.CYAN))
    print(f"  • Index total:  {len(local_ids)}")
    print(f"  • List total:   {len(live_ids)}")
    print(f"  • Matched:      {len(matched)}")
    print(f"  • Missing from list (in index, not on site): {len(missing_from_list)}")
    print(f"  • Extra on list (on site, not in index):     {len(extra_on_list)}")

    if added_ids:
        failed_adds = sorted(added_ids - live_ids)
        if failed_adds:
            print(term.style(f"\n⚠ {len(failed_adds)} of the batch were not found on the list:", term._T.YELLOW))
            for movie_id in failed_adds[:10]:
                movie = index.get("movies", {}).get(str(movie_id), {})
                print(f"    - {title_line(movie)}")
            if len(failed_adds) > 10:
                print(f"    ... and {len(failed_adds) - 10} more")
        else:
            print(term.style(f"\n✓ All {len(added_ids)} batch movie(s) confirmed on the list.", term._T.GREEN))

    if missing_from_list and not added_ids:
        print(term.style("\n⚠ Some local movies are not on the remote list.", term._T.YELLOW))


def _compare_text(count: int, idx_count: int) -> str:
    """Return 'match' or 'mismatch (±N)' for the startup table."""
    diff = idx_count - count
    if diff == 0:
        return "match"
    sign = "+" if diff > 0 else ""
    return f"mismatch ({sign}{diff})"


def _probe_tmdb_status(client: TMDBClient, index: dict) -> None:
    """Fetch the live TMDB list, compare counts with the local index, and print a table."""
    label = "tmdb.org"
    list_id = _config.TMDB_LIST_ID

    if not list_id:
        host_row = (label, False, None, len(index.get("movies", {})), "no list id")
        for line in _format_host_rows([host_row]):
            print(line)
        return

    try:
        items, incomplete = fetch_list(client, list_id)
    except ListFetchError as exc:
        log.error("Startup list probe failed: %s", exc)
        host_row = (label, False, None, len(index.get("movies", {})), "fetch failed")
        for line in _format_host_rows([host_row]):
            print(line)
        return

    live_count = len(items)
    idx_count = len(index.get("movies", {}))
    # `incomplete` is the only real failure signal here -- a complete fetch of a
    # genuinely empty list is a successful probe, not a failed one.
    status = not incomplete
    compare = _compare_text(live_count, idx_count)
    host_row = (label, status, live_count, idx_count, compare)

    for line in _format_host_rows([host_row]):
        print(line)

    _save_mismatch_report(index, items, set())


def _fetch_and_summarize_mismatches(client: TMDBClient, *, added_ids: set[int] | None = None) -> None:
    """Fetch the live list and print a mismatch counter."""
    if added_ids is None:
        added_ids = set()
    try:
        items, incomplete = fetch_list(client, _config.TMDB_LIST_ID)
    except ListFetchError as exc:
        log.error("Could not fetch list for mismatch summary: %s", exc)
        print(term.style("✗ Could not verify against the live list:", term._T.RED), str(exc))
        return

    index = load_index()
    _render_mismatch_summary(index, items, added_ids)
    if incomplete:
        print(term.style("  ⚠ List fetch was incomplete; mismatch counts may be low.", term._T.YELLOW))


def _gaps_report_fresh(generated_at: str, max_age_minutes: int = 5) -> bool:
    """Return True when ``generated_at`` is within ``max_age_minutes``."""
    try:
        dt = datetime.fromisoformat(generated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return datetime.now(UTC) - dt <= timedelta(minutes=max_age_minutes)
    except Exception:
        return False


def _select_batch_source() -> str:
    """Prompt for a batch source, defaulting to the configured file."""
    default = str(DEFAULT_BATCH_FILE)
    print("  • Paste a TMDB/IMDb URL or raw id → add a single movie")
    print("  • Enter filename → use that file")
    print(f"  • Press Enter → use default ({default})")
    print("  • Type 0 → back to main menu")
    user_input = input(f"\nEnter [default: {default}]: ").strip()
    if user_input == "0":
        return ""
    if not user_input:
        return default
    return user_input


def run_push_url_file_only(client: TMDBClient) -> None:
    """Push IDs/URLs from a text file to the remote list without touching the local index.

    Skips movies that are already on the live TMDB list before attempting any push.
    """
    log.debug("Push URL file only selected")
    print()
    print(term.style("→ Push URL file", term._T.BOLD, term._T.CYAN))

    remote_list_id = _config.TMDB_LIST_ID
    if not remote_list_id:
        print(term.style("✗ No TMDB_LIST_ID configured.", term._T.RED))
        return
    if not client.session_id:
        print(term.style("✗ No TMDB session available.", term._T.RED))
        print("  Restart and approve the TMDB session prompt at startup.")
        return

    source = _select_batch_source()
    if not source:
        print("  → Cancelled")
        return

    records: list[dict] = []
    skipped: list[tuple[int, str, str]] = []

    if os.path.exists(source):
        if os.path.isdir(source):
            print(term.style(f"✗ Path is a directory: {source}", term._T.RED))
            return
        try:
            records, skipped = resolve_movie_ids_from_file(client, source)
        except SearchError as exc:
            log.error("Failed to read URL file: %s", exc)
            print(term.style(f"✗ Failed to read file: {exc}", term._T.RED))
            return
    else:
        single = resolve_movie_id_from_input(client, source)
        if single is None:
            print(term.style(f"✗ Not a valid TMDB/IMDb URL or id: {source}", term._T.RED))
            return
        records = [single]

    if skipped:
        print(term.style(f"\n⚠ Skipped {len(skipped)} invalid line(s):", term._T.YELLOW))
        for line_num, raw, reason in skipped[:5]:
            print(f"  Line {line_num}: {raw[:80]!r} — {reason}")
        if len(skipped) > 5:
            print(f"  ... and {len(skipped) - 5} more")

    if not records:
        print(term.style("\n✗ No valid movies found in file.", term._T.RED))
        return

    print("\n  Checking the live TMDB list for existing entries...")
    try:
        live_items, incomplete = fetch_list(client, remote_list_id)
    except ListFetchError as exc:
        log.error("Could not fetch live list before push: %s", exc)
        print(term.style("✗ Could not fetch the live list:", term._T.RED), str(exc))
        return

    live_ids = {int(item.get("id", 0)) for item in live_items if item.get("id")}
    already_present: list[dict] = []
    to_push: list[dict] = []
    for record in records:
        if int(record["id"]) in live_ids:
            already_present.append(record)
        else:
            to_push.append(record)

    if already_present:
        print(
            term.style(
                f"⚠ {len(already_present)} movie(s) already on the list and will be skipped:",
                term._T.YELLOW,
            )
        )
        for record in already_present[:10]:
            print(f"    - {title_line(record)}")
        if len(already_present) > 10:
            print(f"    ... and {len(already_present) - 10} more")
        if incomplete:
            print(
                term.style(
                    "  (List fetch was incomplete; some already-present matches may have been missed.)",
                    term._T.YELLOW,
                )
            )

    if not to_push:
        print()
        print(term.style("✓ Nothing new to push.", term._T.GREEN))
        print("  → Run option 1 (Fast scan) to pull the list into your local index.")
        return

    print()
    print(term.style(f"Found {len(to_push)} movie(s) to push:", term._T.BOLD, term._T.CYAN))
    for record in to_push[:10]:
        print(f"  + {title_line(record)}")
    if len(to_push) > 10:
        print(f"  ... and {len(to_push) - 10} more")

    if not prompts.confirm(f"\nPush these {len(to_push)} movie(s) to remote list {remote_list_id}?", default=False):
        print("  → Cancelled")
        return

    push_ok = 0
    push_fail = 0
    duplicates = 0
    for record in to_push:
        movie_id = int(record["id"])
        result = push_to_tmdb_list(client, remote_list_id, movie_id)
        if result["remote_push"] == "ok":
            push_ok += 1
        elif result["remote_push"] == "duplicate":
            duplicates += 1
        else:
            push_fail += 1

    print()
    total_processed = len(to_push)
    print(
        term.style(
            f"Pushed {total_processed} movie(s): {push_ok} ok, {duplicates} already present, {push_fail} failed.",
            term._T.GREEN,
        )
    )
    if already_present:
        print(f"  {len(already_present)} already on the list were skipped before pushing.")
    print("  → Run option 1 (Fast scan) to pull the list into your local index.")


def run_clean_vanished(client: TMDBClient) -> None:
    """Find local index movies missing from the remote list and ask to delete or rescrape."""
    log.debug("Clean vanished selected")
    print()
    print(term.style("→ Clean vanished entries", term._T.BOLD, term._T.CYAN))

    remote_list_id = _config.TMDB_LIST_ID
    if not remote_list_id:
        print(term.style("✗ No TMDB_LIST_ID configured.", term._T.RED))
        return

    try:
        items, incomplete = fetch_list(client, remote_list_id)
    except ListFetchError as exc:
        log.error("Could not fetch list for vanished cleanup: %s", exc)
        print(term.style("✗ Could not fetch the live list:", term._T.RED), str(exc))
        return

    index = load_index()
    local_movies = index.get("movies", {})
    live_ids = {int(item.get("id", 0)) for item in items if item.get("id")}
    local_ids = {int(mid) for mid in local_movies}
    missing_ids = sorted(local_ids - live_ids)

    if not missing_ids:
        print(term.style("\n✓ No vanished entries. Every local movie is still on the list.", term._T.GREEN))
        return

    print()
    print(term.style(f"⚠ {len(missing_ids)} movie(s) in index but missing from the list:", term._T.YELLOW))
    for movie_id in missing_ids[:10]:
        movie = local_movies.get(str(movie_id), {})
        print(f"    - {title_line(movie)}")
    if len(missing_ids) > 10:
        print(f"    ... and {len(missing_ids) - 10} more")

    bulk = input("\nDelete all these entries? (y/n): ").strip().lower()
    if bulk == "y":
        removed = 0
        for movie_id in missing_ids:
            key = str(movie_id)
            if key in local_movies:
                del local_movies[key]
                removed += 1
        index["movies"] = local_movies
        save_index(index)
        print(term.style(f"\n✓ Removed {removed} vanished movie(s) from the index.", term._T.GREEN))
        return

    rescrape_ids: list[int] = []
    removed = 0
    for movie_id in missing_ids:
        movie = local_movies.get(str(movie_id), {})
        print(f"\n{title_line(movie)}")
        print("  1. Delete from local index")
        print("  2. Rescrape / re-add to TMDB list")
        print("  3. Skip")
        choice = input("Choice (1/2/3): ").strip()
        if choice == "1":
            local_movies.pop(str(movie_id), None)
            removed += 1
        elif choice == "2":
            rescrape_ids.append(movie_id)
        else:
            print("  → Skipped")

    if removed:
        index["movies"] = local_movies
        save_index(index)
        print(term.style(f"\n✓ Removed {removed} movie(s) from the index.", term._T.GREEN))

    if rescrape_ids:
        if not client.session_id:
            print(term.style("\n✗ No TMDB session; cannot re-add movies to the list.", term._T.RED))
            return
        print()
        print(f"  Re-adding {len(rescrape_ids)} movie(s) to TMDB list {remote_list_id}...")
        ok = 0
        failed = 0
        for movie_id in rescrape_ids:
            result = push_to_tmdb_list(client, remote_list_id, movie_id)
            if result["success"]:
                ok += 1
                membership = local_movies.get(str(movie_id), {})
                membership["remote_push"] = "rescraped"
            else:
                failed += 1
                print(f"  ⚠ Could not re-add {title_line(local_movies.get(str(movie_id), {}))}: {result}")
        save_index(index)
        print(f"  Re-add result: {ok} ok, {failed} failed")

        if prompts.confirm("\nVerify against the live TMDB list now?", default=True):
            _fetch_and_summarize_mismatches(client, added_ids=set(rescrape_ids))


def run_franchise_gaps(_client: TMDBClient) -> None:
    """Report connected films and TV not in the index."""
    log.debug("Franchise gaps selected")
    print()
    print(term.style("→ Franchise gaps", term._T.BOLD, term._T.CYAN))

    cached = load_gaps()
    if cached and _gaps_report_fresh(cached.get("generated_at", "")):
        log.debug("Reusing cached gaps report from %s", cached.get("generated_at"))
        print("  → Reusing cached report")
        gaps = cached
    else:
        gaps = find_gaps()

    print()
    print(term.style(f"Franchise gaps ({gaps['indexed_count']} films in index)", term._T.BOLD, term._T.CYAN))

    def _gap_url(item: dict) -> str:
        item_id = item.get("id")
        if item.get("source") == "keyword_tv":
            return f"https://www.themoviedb.org/tv/{item_id}"
        return f"https://www.themoviedb.org/movie/{item_id}"

    def _print_gap_table(items: list[dict], suffix: str = "") -> None:
        if not items:
            return
        idx_w = len(str(len(items)))
        title_w = max(
            (term.display_width(title_line(item) + suffix) for item in items),
            default=0,
        )
        print(f"    {'#':<{idx_w}}  {'Title':<{title_w}}  Link")
        print(f"    {'─' * idx_w}  {'─' * title_w}  {'─' * 40}")
        for i, item in enumerate(items, 1):
            label = title_line(item) + suffix
            print(f"    {i:<{idx_w}}  {label:<{title_w}}  {_gap_url(item)}")

    missing = gaps.get("missing_films", [])
    if missing:
        print()
        print(term.style(f"Missing franchise films: {len(missing)}", term._T.BOLD))
        _print_gap_table(missing)
    else:
        print()
        print(term.style("  ✓ No missing franchise films.", term._T.GREEN))

    tv = gaps.get("connected_tv", [])
    if tv:
        print()
        print(term.style(f"Connected TV series: {len(tv)}", term._T.BOLD))
        _print_gap_table(tv)
    else:
        print()
        print(term.style("  ✓ No connected TV series.", term._T.GREEN))

    # Persist / surface the gaps JSON path
    print()
    print(f"  Report saved to: {GAPS_FILE}")

    # Offer to export missing franchise film URLs to a text file (append-only, deduplicated).
    if missing and prompts.confirm("Export missing franchise film URLs to a text file?", default=False):
        default_target = str(DEFAULT_BATCH_FILE)
        target = input(f"Target file [default: {default_target}]: ").strip()
        if not target:
            target = default_target

        # Use an absolute path consistently for both reading and writing.
        target_path = os.path.abspath(target)

        existing_lines: list[str] = []
        if os.path.exists(target_path) and os.path.isfile(target_path):
            try:
                with open(target_path, encoding="utf-8") as fh:
                    existing_lines = [line.rstrip("\n") for line in fh]
            except OSError as exc:
                log.warning("Could not read existing target file %s: %s", target_path, exc)

        # Normalize URLs for deduplication while preserving the user's original lines.
        existing_normalized = {line.strip() for line in existing_lines}

        new_urls: list[str] = []
        for item in missing:
            if item.get("source") != "collection":
                continue
            url = _gap_url(item)
            if url.strip() in existing_normalized:
                continue
            existing_normalized.add(url.strip())
            new_urls.append(url)

        if not new_urls:
            print("  → No new URLs to add (all already present in file).")
        else:
            try:
                # Use a header comment to visually separate an export batch.
                timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                header = f"# Franchise gaps export — {timestamp} — {len(new_urls)} URL(s)"
                os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
                with open(target_path, "a", encoding="utf-8") as fh:
                    # Ensure a blank line before the header when appending to existing content.
                    if existing_lines and existing_lines[-1].strip():
                        fh.write("\n")
                    fh.write(header + "\n")
                    fh.write("\n".join(new_urls) + "\n")
                print(term.style(f"  ✓ Appended {len(new_urls)} URL(s) to {target_path}", term._T.GREEN))
            except OSError as exc:
                log.error("Could not append to %s: %s", target_path, exc)
                print(term.style(f"  ✗ Could not write to {target_path}: {exc}", term._T.RED))


def check_api_key() -> bool:
    """Fail fast if no API key is configured."""
    if not _config.TMDB_API_KEY:
        log.error("TMDB_API_KEY is not set. Add it to your .env file.")
        print(term.style("✗ TMDB_API_KEY is not set.", term._T.RED))
        print("  Add it to .env in the project root and try again.")
        return False
    print(term.style("✓ API key configured", term._T.GREEN))
    return True


def main() -> None:
    bootstrap()
    global log
    log = setup_logging()
    print_header()

    if not check_api_key():
        return

    index = load_index()
    movie_count = len(index.get("movies", {}))
    log.debug("Loaded index with %d movies", movie_count)

    print(f"  → Index loaded: {movie_count} movie{'s' if movie_count != 1 else ''}")
    if _config.TMDB_LIST_ID:
        print(f"  → List ID: {_config.TMDB_LIST_ID}")

    actions = {
        "1": run_full_scan,
        "2": run_fast_scan,
        "3": run_franchise_gaps,
        "4": run_push_url_file_only,
    }

    with TMDBClient() as client:
        # Resolve (or create) a TMDB session at startup. Once a session is approved
        # in the browser, TMDB does not expire it, so this is a one-time step.
        client.ensure_session()
        if client.session_id:
            print(term.style("  ✓ TMDB session available", term._T.GREEN))
        else:
            print("  → No TMDB session; writes that require auth are disabled")

        # Probe the live list and compare its size to the local index at startup.
        _probe_tmdb_status(client, index)

        while True:
            show_menu()
            try:
                choice = input("Enter your choice (0-4): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                log.info("Goodbye!")
                break

            if choice == "0":
                print()
                print(term.style("✓ Goodbye!", term._T.GREEN))
                log.debug("Goodbye!")
                break

            action = actions.get(choice)
            if action is None:
                print(term.style("✗ Invalid choice.", term._T.RED), "Please enter a number between 0 and 4.")
                continue

            try:
                action(client)
            except KeyboardInterrupt:
                print()
                log.warning("Option %s interrupted by the user", choice)
                print(term.style("  ⚠ Stopped.", term._T.YELLOW), "Any partly written data has been discarded.")
            except Exception as exc:
                log.error("Option %s failed: %s", choice, exc, exc_info=True)
                print()
                print(f"{term.style('✗ That option did not finish:', term._T.RED)} {exc}")
                print(f"  Full detail is in {LOG_FILE}")

    print()
    print(term.style("✓ Done!", term._T.GREEN))
    log.debug("Done!")


def _run_cli() -> int:
    """Run main() and return a process exit code.

    Separate from main() so tests can reach it.
    """
    # A fresh install has no .env anywhere, so write the template out rather than
    # leaving the user a filename to hunt for. Deliberately non-fatal: the
    # credential check further in reports what still needs filling in.
    created = ensure_env_file()
    if created:
        print("")
        print("Created a credentials file at:")
        print(f"    {created}")
        print("Fill in your details there, then run this again.")
        print("")
    try:
        main()
    except KeyboardInterrupt:
        print()
        log.info("Interrupted.")
        return 130
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        log.critical("Unexpected error: %s", exc, exc_info=True)
        print(f"\n{term.style('✗ Unexpected error:', term._T.RED)} {exc}")
        print(f"  This is a bug. Full detail is in {LOG_FILE}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
