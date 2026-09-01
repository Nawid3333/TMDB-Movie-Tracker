"""Movie detail card rendering for the terminal."""

from src.ui.reports import title_line
from src.ui.term import _T, style, wrap


def _comma_list(items: list[str]) -> str:
    return ", ".join(str(x) for x in items if x)


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    return plural if plural else f"{singular}s" if count > 1 else singular


def render_detail_card(membership: dict, detail: dict) -> None:
    """Print a text detail card for a movie."""
    lines = [style(title_line(membership), _T.BOLD, _T.CYAN)]

    tagline = detail.get("tagline")
    if tagline:
        lines.append(style(tagline, _T.DIM))

    status = membership.get("status") or detail.get("status")
    runtime = detail.get("runtime")
    if status or runtime:
        parts = []
        if status:
            parts.append(f"Status: {status}")
        if runtime:
            parts.append(f"Runtime: {runtime} min")
        lines.append("  ".join(parts))

    genres = detail.get("genres", [])
    if genres:
        lines.append(f"Genres: {_comma_list(genres)}")

    directors = detail.get("directors", [])
    if directors:
        label = _pluralize(len(directors), "Director")
        lines.append(f"{label}: {_comma_list(directors)}")

    cast = detail.get("cast", [])[:8]
    if cast:
        lines.append("Cast:")
        for person in cast:
            name = person.get("name", "")
            character = person.get("character", "")
            if character:
                lines.append(f"  {name} as {character}")
            else:
                lines.append(f"  {name}")

    collection = detail.get("collection") or membership.get("collection")
    if collection and collection.get("name"):
        lines.append(f"Collection: {collection['name']}")

    connected_tv = detail.get("connected_tv", [])
    if connected_tv:
        lines.append("Connected TV:")
        for item in connected_tv[:5]:
            lines.append(f"  {item.get('name', '')} (via {item.get('via_keyword', '')})")

    overview = detail.get("overview")
    if overview:
        lines.append("")
        lines.extend(wrap(overview))

    movie_id = membership.get("id")
    if movie_id:
        lines.append("")
        lines.append(f"https://www.themoviedb.org/movie/{movie_id}")

    for line in lines:
        print(line)
