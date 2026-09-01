"""Terminal helpers: colors, styles, display width, boxes."""

import os
import re
import shutil
import unicodedata


class _T:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"


def _color_enabled() -> bool:
    """Respect NO_COLOR and dumb terminals."""
    if os.getenv("NO_COLOR"):
        return False
    return os.getenv("TERM") != "dumb"


_COLOR = _color_enabled()


def style(text: str, *codes: str) -> str:
    """Wrap text in ANSI style codes, disabled when NO_COLOR is set."""
    if not _COLOR:
        return text
    return "".join(codes) + text + _T.RESET


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes."""
    return _ANSI_RE.sub("", text)


def display_width(text: str) -> int:
    """How many terminal columns `text` occupies, ignoring ANSI codes."""
    plain = strip_ansi(text)
    width = 0
    for index, char in enumerate(plain):
        if char in ("\ufe0f", "\ufe0e") or unicodedata.combining(char):
            continue
        wide = unicodedata.east_asian_width(char) in ("W", "F")
        emoji_presentation = plain[index + 1 : index + 2] == "\ufe0f"
        width += 2 if (wide or emoji_presentation) else 1
    return width


def _terminal_width(default: int = 80) -> int:
    """Best-effort terminal width for wrapping."""
    try:
        columns, _ = shutil.get_terminal_size()
        return max(columns, 40)
    except OSError:
        return default


def wrap(text: str, width: int | None = None) -> list[str]:
    """Wrap text to fit the terminal, preserving existing line breaks."""
    if width is None:
        width = _terminal_width() - 4
    width = max(width, 20)
    lines: list[str] = []
    for raw in text.splitlines():
        current = raw.rstrip()
        while display_width(current) > width:
            cut = width
            while cut > 0 and current[cut] != " ":
                cut -= 1
            if cut == 0:
                cut = width
            lines.append(current[:cut].rstrip())
            current = current[cut:].lstrip()
        lines.append(current)
    return lines


def box(lines: list[str], width: int | None = None) -> list[str]:
    """Return a list of box-drawn lines, accounting for ANSI codes."""
    if width is None:
        width = max(64, min(_terminal_width() - 4, 80))
    inner = max((display_width(line) for line in lines), default=0)
    width = max(width, inner)
    out = ["╔" + "═" * width + "╗"]
    for line in lines:
        pad = max(width - display_width(line) - 1, 0)
        out.append("║ " + line + " " * pad + "║")
    out.append("╚" + "═" * width + "╝")
    return out


def hr(width: int | None = None) -> str:
    """Return a horizontal rule."""
    if width is None:
        width = _terminal_width()
    return "─" * max(width, 20)
