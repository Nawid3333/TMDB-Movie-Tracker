"""Render change reports and other summary output to the terminal."""

from datetime import UTC, datetime

from src.changes import ChangeSet
from src.ui.term import cprint as print
from src.ui.term import danger, dim, step, success, warn


def _is_upcoming(date_str: str) -> bool:
    if not date_str:
        return False
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d").date()
        return date > datetime.now(UTC).date()
    except ValueError:
        return False


def title_line(record: dict, *, show_upcoming: bool = False) -> str:
    """Render a stable `title (year)` label for reports and menus."""
    title = record.get("title") or record.get("name") or "(untitled)"
    year = ""
    release = record.get("release_date") or record.get("first_air_date") or ""
    if release and len(release) >= 4:
        year = f" ({release[:4]})"
    upcoming = " (upcoming)" if show_upcoming and _is_upcoming(release) else ""
    return f"{title}{year}{upcoming}"


def render_change_report(change_set: ChangeSet) -> None:
    """Print a human-readable summary of proposed changes."""
    print()
    print(step("Fast scan results"))
    print(f"  Current index: {change_set.current_count} movies")
    print(f"  TMDB list:      {change_set.proposed_count} movies")

    if change_set.incomplete:
        print()
        print(warn("  ⚠ Scan was incomplete or failed the shrink gate."))
        print("    Removal proposals are blocked until a complete scan succeeds.")

    if change_set.additions:
        print()
        print(success(f"Additions: {len(change_set.additions)}"))
        for record in change_set.additions.values():
            print(f"  + {title_line(record)}")

    if change_set.removals:
        print()
        print(danger(f"Removals: {len(change_set.removals)}"))
        for record in change_set.removals.values():
            print(f"  - {title_line(record)}")

    if not change_set.additions and not change_set.removals:
        print()
        print(dim("  No changes to review."))
