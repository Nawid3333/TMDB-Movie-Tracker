"""All user prompts and confirmations live here — no input() elsewhere."""

from src.ui.term import bold, step
from src.ui.term import cinput as input
from src.ui.term import cprint as print

_PREVIEW_LIMIT = 20
_DEFAULT_PAGE_SIZE = 20


def paginate_list(items: list[str], page_size: int = _DEFAULT_PAGE_SIZE) -> None:
    """Show a long list in pages, Enter = next page, q = skip remaining."""
    if not items:
        return
    total = len(items)
    idx = 0
    while idx < total:
        end = min(idx + page_size, total)
        for item in items[idx:end]:
            print(f"  • {item}")
        idx = end
        if idx < total:
            choice = input(f"  ({idx}/{total}) Enter = more, q = skip: ").strip().lower()
            if choice == "q":
                print(f"  ... skipped {total - idx} remaining")
                break


def ask_choice(prompt: str, options: list[str], default: str = "") -> str:
    """Ask the user to pick one of a list of options.

    Returns the selected option string.
    """
    opt_text = "/".join(step(opt.upper()) if opt == default else opt for opt in options)
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
    """Confirm a category of changes with paginated preview."""
    print(bold(f"\n[{category}]"))
    paginate_list(items, page_size=_DEFAULT_PAGE_SIZE)
    return confirm("Approve these changes?", default=default)
