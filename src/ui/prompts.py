"""All user prompts and confirmations live here — no input() elsewhere.

Every question takes only the answers it offers: y or n (either case), or one
of its listed options. Anything else -- a typo, a synonym, Enter alone -- is
asked again with a line saying what is allowed. There are no defaults: an
answer is never assumed. End of input, or MAX_UNRECOGNIZED unusable answers in
a row, gives the prompt's safe answer -- the one that changes nothing -- so an
unattended run (a piped feed, a scripted test) cannot loop forever or decide
something by accident.
"""

from collections.abc import Iterable

from src.ui.term import bold
from src.ui.term import cinput as input
from src.ui.term import cprint as print

_PREVIEW_LIMIT = 20
_DEFAULT_PAGE_SIZE = 20

# How many unusable answers in a row one prompt takes before it stops asking
# and returns its safe answer. A few are typos; an endless run is an
# unattended feed that would otherwise be asked forever.
MAX_UNRECOGNIZED = 5


def read_line(text: str) -> str | None:
    """Read one stripped line of free text; None at end of input.

    For the prompts whose answer is not a fixed option -- a file name, a URL
    -- and which therefore validate it themselves. They still owe the same
    rules as ask(): re-ask what they cannot use, and stop after
    MAX_UNRECOGNIZED tries.
    """
    try:
        return input(text).strip()
    except EOFError:
        return None


def ask(text: str, choices: Iterable[str], *, safe: str, hint: str = "") -> str:
    """Ask *text* until the answer is one of *choices*; return it lowercased.

    Only what the prompt offers is accepted -- no synonyms, no guessing. A
    mistyped answer used to fall into whichever branch caught "anything
    else": at the vanished-movie prompt that walked every movie one by one,
    and at "Choice (1/2/3)" it skipped the movie. It is asked again now.

    There are no defaults: Enter alone is asked again like any other unusable
    answer. End of input, or MAX_UNRECOGNIZED unusable answers in a row, give
    *safe*: the answer that changes nothing.
    """
    valid = {choice.lower() for choice in choices}
    hint = hint or "type one of " + ", ".join(sorted(valid))
    for _ in range(MAX_UNRECOGNIZED):
        answer = read_line(text)
        if answer is None:
            print(f"  → No input available; answering {safe!r}.")
            return safe
        answer = answer.lower()
        if answer in valid:
            return answer
        print(f"  ⚠ {repr(answer) + ' is not an option' if answer else 'No answer'} - {hint}.")
    print(f"  ⚠ No usable answer after {MAX_UNRECOGNIZED} tries; answering {safe!r}.")
    return safe


def confirm(text: str) -> bool:
    """Ask a y/n question until it is answered y or n (either case).

    Nothing else counts, Enter included: this used to take Enter as the
    default, so "Approve these changes? [Y/n]" approved additions on a stray
    keypress. End of input, or a run of unusable answers, counts as no.
    """
    return ask(f"{text} (y/n): ", ("y", "n"), safe="n", hint="type y or n") == "y"


def wait_for_enter(text: str) -> bool:
    """Wait for Enter as a "continue" key; False at end of input.

    Enter here does not answer a question -- it only says "I am done in the
    browser" -- so it is the one key accepted. Typed text is asked again (it
    is usually an answer meant for some other prompt), and end of input or a
    run of typed answers returns False so the caller can skip the step
    rather than act on an approval nobody gave.
    """
    for _ in range(MAX_UNRECOGNIZED):
        answer = read_line(text)
        if answer is None:
            print("  → No input available; skipping this step.")
            return False
        if not answer:
            return True
        print("  ⚠ Just press Enter once you are done in the browser.")
    print(f"  ⚠ No usable answer after {MAX_UNRECOGNIZED} tries; skipping this step.")
    return False


def paginate_list(items: list[str], page_size: int = _DEFAULT_PAGE_SIZE) -> None:
    """Show a long list in pages, Enter = more, q = skip remaining.

    Enter is a pager key here, not an answer: it turns the page. Anything
    other than Enter or q is asked again, and end of input skips the rest
    (the question that follows the list is asked either way).
    """
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
            choice = ask(
                f"  ({idx}/{total}) Enter = more, q = skip: ",
                ("", "q"),
                safe="q",
                hint="press Enter for more or type q",
            )
            if choice == "q":
                print(f"  ... skipped {total - idx} remaining")
                break


def confirm_category(category: str, items: list[str]) -> bool:
    """Confirm a category of changes with paginated preview."""
    print(bold(f"\n[{category}]"))
    paginate_list(items, page_size=_DEFAULT_PAGE_SIZE)
    return confirm("Approve these changes?")
