"""All user prompts and confirmations live here — no input() elsewhere."""

from src.ui.term import _T, style


def ask_choice(prompt: str, options: list[str], default: str = "") -> str:
    """Ask the user to pick one of a list of options.

    Returns the selected option string.
    """
    opt_text = "/".join(style(opt.upper(), _T.BOLD, _T.CYAN) if opt == default else opt for opt in options)
    while True:
        answer = input(f"{prompt} [{opt_text}]: ").strip().lower()
        if not answer and default:
            return default
        if answer in [opt.lower() for opt in options]:
            return answer
        print("  Please enter one of:", ", ".join(options))


def confirm(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question."""
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{suffix}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n")


def confirm_category(category: str, items: list[str], default: bool = False) -> bool:
    """Confirm a category of changes with a bounded preview."""
    print(style(f"\n[{category}]", _T.BOLD))
    for item in items[:20]:
        print(f"  • {item}")
    if len(items) > 20:
        print(f"  ... and {len(items) - 20} more")
    return confirm("Approve these changes?", default=default)
