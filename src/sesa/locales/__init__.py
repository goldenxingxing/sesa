"""Translation catalogues, one module per locale.

A catalogue maps the **English source string** to its translation. Keys are the
literal text passed to :func:`sesa.i18n.t`, which keeps the source readable and
means an untranslated string degrades to readable English rather than a key.
"""

from __future__ import annotations

import ast
import pathlib


def source_strings() -> list[str]:
    """Every literal passed to ``t(...)`` anywhere in the package.

    Read out of the AST rather than kept in a hand-maintained list: a list
    someone has to remember to update is a list that goes stale, and a stale
    one makes the coverage test pass while strings quietly go untranslated.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if path.parent.name == "locales":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file is its own error
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "t" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return sorted(found)
