"""The line telling the user how to install an optional dependency has to work today.

How it started: the user asked "can it be pip-installed now?" The answer was no — and the
code held 7 places saying `pip install 'sesa[tui]'`, 6 of them **runtime error messages**.
The user is already stuck on "keyring is not installed", follows the hint, and gets
`No matching distribution found for sesa` — so they now have two problems, and the second
looks like the project cannot be installed at all.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from sesa._install import install_hint


def test_the_hint_always_offers_a_command_that_works_today():
    """Not published to PyPI yet, so the source-checkout form has to be given alongside."""
    hint = install_hint("tui")
    assert "from a source checkout" in hint
    assert ".[tui]" in hint


def test_the_hint_is_plain_so_exceptions_do_not_leak_escape_characters():
    r"""It goes into `SemanticUnavailable` / `CredentialError` messages, printed verbatim by
    Python. A backslash inside the string would leak `sesa\[tui]` into the traceback —
    **escaping belongs to the rendering layer, not to the string itself**.
    """
    assert "\\" not in install_hint("semantic")


def test_the_extra_name_survives_rich_rendering():
    """rich eats `[tui]` as markup and the user sees `pip install 'sesa'` — **a command that
    installs no extra at all**. Measured: I walked into it the same day I wrote this hint.

    So every rich call site has to escape.
    """
    console = Console(width=200, no_color=True, markup=True)
    with console.capture() as captured:
        console.print(escape(install_hint("tui")))
    rendered = captured.get()
    assert "sesa[tui]" in rendered, f"rich ate the brackets: {rendered!r}"
    assert ".[tui]" in rendered


def test_every_rich_call_site_escapes_the_hint():
    """Miss one and that one's users get a command that installs nothing."""
    import inspect
    import re

    from sesa import cli, wizard

    for module in (cli, wizard):
        source = inspect.getsource(module)
        for line in source.splitlines():
            if "install_hint(" not in line:
                continue
            assert re.search(r"(E|escape)\(\s*install_hint\(", line), (
                f"this line in {module.__name__} is not escaped: {line.strip()}"
            )


def test_exception_paths_do_not_escape():
    """Exception messages are printed verbatim by Python, where escaping would expose the
    backslash instead.
    """
    import inspect
    import re

    from sesa import credentials, semantic

    for module in (credentials, semantic):
        for line in inspect.getsource(module).splitlines():
            if "install_hint(" in line:
                assert not re.search(r"(E|escape)\(\s*install_hint\(", line), (
                    f"{module.__name__}'s exception path must not escape: {line.strip()}"
                )
