"""Render change reports and other summary output to the terminal."""

from collections.abc import Iterable
from datetime import UTC, datetime

from src.changes import ChangeSet
from src.ui.term import cprint as print
from src.ui.term import danger, dim, display_width, link, step, success, warn


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


def movie_url(movie_id: int | str) -> str:
    """Return a movie's TMDB page URL."""
    return f"https://www.themoviedb.org/movie/{movie_id}"


def title_link(record: dict, *, show_upcoming: bool = False) -> str:
    """Render `title_line` with its TMDB page link appended, dash-separated.

    "Title (year) — https://www.themoviedb.org/movie/id", with the URL
    itself as a clickable OSC 8 hyperlink -- readable as plain text even in
    a terminal that can't render hyperlinks, and clickable in one that can.
    Falls back to a plain title_line when the record has no id (nothing to
    link to).
    """
    label = title_line(record, show_upcoming=show_upcoming)
    movie_id = record.get("id")
    if not movie_id:
        return label
    url = movie_url(movie_id)
    return f"{label} — {link(url, url)}"


def title_link_rows(records: Iterable[dict], *, show_upcoming: bool = False) -> list[str]:
    """Render `title_link` for a batch of records with the link column aligned.

    Every title is padded to the widest title in the batch so the "—" and
    the URLs line up in a column, the way a table would, instead of each
    link trailing wherever its own title happens to end.
    """
    records = list(records)
    labels = [title_line(r, show_upcoming=show_upcoming) for r in records]
    width = max((display_width(label) for label in labels), default=0)

    rows: list[str] = []
    for record, label in zip(records, labels, strict=True):
        movie_id = record.get("id")
        if not movie_id:
            rows.append(label)
            continue
        pad = " " * max(width - display_width(label), 0)
        url = movie_url(movie_id)
        rows.append(f"{label}{pad} — {link(url, url)}")
    return rows


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
        for line in title_link_rows(change_set.additions.values()):
            print(f"  + {line}")

    if change_set.removals:
        print()
        print(danger(f"Removals: {len(change_set.removals)}"))
        for line in title_link_rows(change_set.removals.values()):
            print(f"  - {line}")

    if not change_set.additions and not change_set.removals:
        print()
        print(dim("  No changes to review."))
