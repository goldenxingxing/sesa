"""Localisation.

**English is the source language.** Every user-facing string is written in
English in the code, and translations live in per-locale catalogues keyed by
that English text. The alternative — Chinese in the source with an English
catalogue — puts the *default* locale behind a lookup and makes a missing key
fall back to Chinese for an English-speaking user. That is backwards.

Resolution order, first hit wins:

1. ``SESA_LANG`` environment variable — for one-off overrides and for tests
2. ``language:`` in the config file
3. the system locale, when it names a language we have a catalogue for
4. English

A missing translation returns the English text unchanged. **Never a key, never
an empty string** — a user hitting an untranslated corner should see readable
English, not ``ui.error.no_participants``.
"""

from __future__ import annotations

import locale
import os
from typing import Any

#: Locales we ship. English needs no catalogue: it is the source.
SUPPORTED = ("en", "zh")

_catalogues: dict[str, dict[str, str]] = {}
_active: str | None = None


def _load(lang: str) -> dict[str, str]:
    if lang in _catalogues:
        return _catalogues[lang]
    catalogue: dict[str, str] = {}
    if lang == "zh":
        from .locales.zh import CATALOGUE

        catalogue = CATALOGUE
    _catalogues[lang] = catalogue
    return catalogue


def _from_system() -> str | None:
    """The system locale, if it names a language we support."""
    try:
        tag = locale.getlocale()[0] or os.environ.get("LANG") or ""
    except (ValueError, TypeError):
        return None
    tag = tag.lower().replace("-", "_")
    for lang in SUPPORTED:
        if tag.startswith(lang):
            return lang
    return None


def resolve(configured: str | None = None) -> str:
    """Work out which locale to use. See the module docstring for the order."""
    override = os.environ.get("SESA_LANG", "").strip().lower()
    if override:
        # An unknown value is a typo, not a request for a language we lack. Falling back silently
        # would leave the user staring at English while their config says otherwise, with nothing to
        # explain it.
        return override if override in SUPPORTED else "en"
    if configured and configured.lower() in SUPPORTED:
        return configured.lower()
    return _from_system() or "en"


def use(lang: str | None) -> str:
    """Set the active locale for this process. Returns what was actually set."""
    global _active
    _active = resolve(lang)
    return _active


def active() -> str:
    global _active
    if _active is None:
        _active = resolve(None)
    return _active


def t(text: str, /, **fields: Any) -> str:
    """Translate ``text`` into the active locale, then interpolate ``fields``.

    ``text`` is the English source string and doubles as the catalogue key.
    Interpolation happens **after** lookup so translators can reorder fields:
    ``t("{a} before {b}", a=1, b=2)``.
    """
    rendered = _load(active()).get(text, text)
    return rendered.format(**fields) if fields else rendered


class scoped:
    """Temporarily switch locale inside a ``with`` block.

    The deliverable (``RESULT.md``) is not interface chrome: it is the
    deliberation written down, and its scaffolding should match the language
    the participants actually spoke. An English "## Conclusion" over Chinese
    prose is a document nobody asked for.

    So the report renders in the **task's** language while the surrounding CLI
    stays in the interface language. Those are genuinely different questions:
    the interface serves whoever is driving, the deliverable serves whoever
    will read it.
    """

    def __init__(self, lang: str | None) -> None:
        self._wanted = lang
        self._previous: str | None = None

    def __enter__(self) -> str:
        global _active
        self._previous = active()
        _active = self._wanted if self._wanted in SUPPORTED else self._previous
        return _active

    def __exit__(self, *_exc: object) -> None:
        global _active
        _active = self._previous


def missing_keys(lang: str) -> list[str]:
    """Source strings with no translation in ``lang``. For the coverage test."""
    from .locales import source_strings

    catalogue = _load(lang)
    return [s for s in source_strings() if s not in catalogue]
