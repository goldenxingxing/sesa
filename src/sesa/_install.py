"""Tell the user how to install an optional dependency — **with a line that works today**.

There used to be 7 places (error messages, the wizard, report footnotes, the README)
all saying ``pip install 'sesa[tui]'``. But sesa **is not published to PyPI yet**, and
that line returns ``No matching distribution found for sesa`` for every user.

That hurts more than the same problem in the README, because these are **runtime error
messages**: the user is already stuck on "keyring is not installed", follows the hint,
and now has two problems — the second of which looks like the project cannot be
installed at all.

The wording lives in one place. Seven separately maintained strings guarantee that a
change misses one.
"""

from __future__ import annotations

from .i18n import t


def install_hint(extra: str) -> str:
    """How to install one optional extra.

    Both forms are given: from PyPI (later) and from a source checkout (now). No cleverness
    about detecting how the user installed it — detection gets things wrong, and a wrong
    detection is one more false instruction.
    """
    # **This returns a bare string and does not escape for rich.**
    # It goes both into exception messages (`SemanticUnavailable`, `CredentialError` — printed
    # verbatim by Python) and into rich-rendered terminal output. And rich eats `[tui]` as markup,
    # leaving the user with `pip install 'sesa'` — a command that installs no extra at all
    # (measured: I walked into it the same day I wrote this hint).
    # The fix is not a backslash here — that would leak `sesa\[tui]` into the exception message.
    # **Escaping belongs to the rendering layer**: wrap it in `escape()` at the rich call sites and
    # use it verbatim on the exception path.
    return t(
        "pip install 'sesa[{extra}]' (from a source checkout: pip install '.[{extra}]')",
        extra=extra,
    )
