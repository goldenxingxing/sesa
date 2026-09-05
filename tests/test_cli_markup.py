"""A participant's output must never be taken as terminal markup syntax.

Models discuss markup, array indices and regexes in their prose all the time — `[/dim]`,
`arr[0]`, `[a-z]` are all common output. rich takes them for markup: at best it eats the
brackets, at worst the whole CLI crashes.

Two measured consequences:
* `pip install 'sesa[keyring]'` rendered as `pip install 'sesa'` — following it installs the
  version without keyring, so the instruction itself stops working
* a `[/dim]` in the prose raises MarkupError outright, cutting off a deliberation's output
"""

from __future__ import annotations

import pytest
from rich.console import Console

from sesa.cli import E


@pytest.mark.parametrize(
    "text",
    [
        "请执行 `pip install 'sesa[keyring]'`",
        "模型说：这里有个 [未闭合 标记",
        "[/dim] 提前闭合",
        "正则是 [a-z]+ 而下标是 arr[0]",
    ],
)
def test_external_text_survives_rich_rendering(text):
    console = Console(record=True, width=200)
    console.print(f"[dim]{E(text)}[/dim]")  # passing means not raising


def test_the_extras_hint_keeps_its_brackets():
    """An install command that has lost its brackets is no command at all."""
    console = Console(record=True, width=200)
    hint = "请执行 `pip install 'sesa[keyring]'`"
    console.print(f"[dim]{E(hint)}[/dim]")

    assert "sesa[keyring]" in console.export_text()


def test_unescaped_text_would_have_crashed():
    """Keep the counterexample: it really does blow up without escaping, which is what makes this
    test mean anything.
    """
    console = Console(record=True, width=200)

    with pytest.raises(Exception, match="closing tag"):
        console.print("[dim][/dim] 提前闭合[/dim]")
