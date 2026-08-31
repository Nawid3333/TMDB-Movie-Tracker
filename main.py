"""TMDB Movie Tracker — terminal menu entry point."""

import sys

from config.config import LOG_FILE, MIN_SHRINK_RATIO, SESSION_FILE, TMDB_API_KEY, setup_logging
from src.changes import apply_changes, detect_changes
from src.enrich import run_full_scan as enrich_run_full_scan
from src.gaps import find_gaps
from src.index import load_details, load_index, now_iso, save_index
from src.list_fetcher import ListFetchError, fetch_list
from src.tmdb_api import TMDBClient
from src.ui import prompts, reports, term
from src.ui.reports import title_line

log = setup_logging()


def print_header() -> None:
    """Print the application banner."""
    lines = [
        term.style("TMDB Movie Tracker", term._T.BOLD, term._T.CYAN),
        "Track your watched films from a TMDB custom list",
    ]
    print()
    for line in term.box(lines):
        print(line)
    print()


def show_menu() -> None:
    """Print the main menu."""
    print()
    print(term.style("Menu", term._T.BOLD, term._T.CYAN))
    print("  1. Full scan       — every detail (slow, accurate)")
    print("  2. Fast scan       — list membership only (quick)")
    print("  3. Franchise gaps  — connected films and TV you missed")
    print("  0. Exit")
    print()


def run_full_scan(client: TMDBClient) -> None:
    """Run a full enrichment scan over the local index."""
    log.info("Full scan selected")
    _ensure_session(client)
    enrich_run_full_scan(client, force=False, resume=True)


def _ensure_session(client: TMDBClient) -> None:
    """Acquire a TMDB session when one of the write paths is about to run."""
    if client.session_id:
        return
    session_id = client.ensure_session()
    if session_id:
        log.info("TMDB session available")
    else:
        log.info("Running without TMDB session; writes that require a session are disabled")


def run_fast_scan(client: TMDBClient) -> None:
    """Fetch list membership, diff against index, confirm, and save."""
    log.info("Starting fast scan...")
    index = load_index()
    _ = load_details()

    try:
        items, incomplete = fetch_list(client)
    except ListFetchError as exc:
        log.error("Fast scan failed: %s", exc)
        print(term.style("✗ Could not fetch the list:", term._T.RED), exc)
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
        log.info("Fast scan complete; no changes")
        return

    approve_additions = False
    if change_set.additions:
        titles = [title_line(r) for r in change_set.additions.values()]
        approve_additions = prompts.confirm_category("Additions", titles, default=True)

    approve_removals = False
    if change_set.removals:
        titles = [title_line(r) for r in change_set.removals.values()]
        approve_removals = prompts.confirm_category("Removals", titles, default=False)

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
    log.info(
        "Fast scan saved: +%d additions, -%d removals, incomplete=%s",
        added,
        removed,
        change_set.incomplete,
    )
    print("Index saved.")
    if change_set.incomplete:
        print("  Note: the list fetch was incomplete. Removals were not proposed.")


def run_franchise_gaps(_client: TMDBClient) -> None:
    """Report connected films and TV not in the index."""
    log.info("Franchise gaps selected")
    gaps = find_gaps()
    print()
    print(term.style(f"Franchise gaps ({gaps['indexed_count']} films in index)", term._T.BOLD, term._T.CYAN))

    def _gap_title(record: dict) -> str:
        title = record.get("title") or record.get("name") or "(untitled)"
        year = ""
        date = record.get("release_date") or record.get("first_air_date") or ""
        if date and len(date) >= 4:
            year = f" ({date[:4]})"
        upcoming = " (upcoming)" if record.get("upcoming") else ""
        return f"{title}{year}{upcoming}"

    missing = gaps.get("missing_films", [])
    if missing:
        print()
        print(term.style(f"Missing franchise films: {len(missing)}", term._T.BOLD))
        for item in missing:
            print(f"  • {_gap_title(item)}")
    else:
        print()
        print("  No missing franchise films.")

    tv = gaps.get("connected_tv", [])
    if tv:
        print()
        print(term.style(f"Connected TV series: {len(tv)}", term._T.BOLD))
        for item in tv:
            via = item.get("via_keyword", "")
            suffix = f" (via {via})" if via else ""
            print(f"  • {_gap_title(item)}{suffix}")
    else:
        print()
        print("  No connected TV series.")


def check_api_key() -> bool:
    """Fail fast if no API key is configured."""
    if not TMDB_API_KEY:
        log.error("TMDB_API_KEY is not set. Add it to your .env file.")
        print(term.style("✗ TMDB_API_KEY is not set.", term._T.RED))
        print("  Add it to .env in the project root and try again.")
        return False
    return True


def main() -> None:
    print_header()

    if not check_api_key():
        return

    index = load_index()
    _ = load_details()
    log.debug("Loaded index with %d movies", len(index.get("movies", {})))

    client = TMDBClient(SESSION_FILE)

    actions = {
        "1": run_full_scan,
        "2": run_fast_scan,
        "3": run_franchise_gaps,
    }

    try:
        while True:
            show_menu()
            try:
                choice = input("Enter your choice (0-3): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                log.info("Goodbye!")
                break

            if choice == "0":
                log.info("Goodbye!")
                break

            action = actions.get(choice)
            if action is None:
                print(term.style("✗ Invalid choice.", term._T.RED), "Please enter a number between 0 and 3.")
                continue

            try:
                action(client)
            except KeyboardInterrupt:
                print()
                log.warning("Option %s interrupted by the user", choice)
                print("  Stopped. Any partly written data has been discarded.")
            except Exception as exc:
                log.error("Option %s failed: %s", choice, exc, exc_info=True)
                print(f"\n{term.style('✗ That option did not finish:', term._T.RED)} {exc}")
                print(f"  Full detail is in {LOG_FILE}")
    finally:
        client.close()

    log.info("Done!")


def _run_cli() -> int:
    """Run main() and return a process exit code.

    Separate from main() so tests can reach it.
    """
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
