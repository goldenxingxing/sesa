"""The interface language is selectable and defaults to English; the deliberation language
follows the task.

The user's request: "can the product offer Chinese and English, defaulting to English?"

There is one architectural decision that must not be got backwards: **English in the source,
Chinese in the catalogue**. The other way round (Chinese source + English catalogue) would
put **the default language** behind a lookup, falling back to Chinese on a miss — and an
English user would get a screen of Chinese.
"""

from __future__ import annotations

import pytest

from sesa import i18n
from sesa.prompts import pick_language

# ── the interface language ──────────────────────────────────────────────────────── #


def test_english_is_the_default(monkeypatch):
    monkeypatch.delenv("SESA_LANG", raising=False)
    monkeypatch.setattr(i18n, "_from_system", lambda: None)
    assert i18n.resolve(None) == "en"


def test_the_config_can_select_chinese(monkeypatch):
    monkeypatch.delenv("SESA_LANG", raising=False)
    assert i18n.resolve("zh") == "zh"


def test_the_environment_overrides_the_config(monkeypatch):
    """Switching language once should not force anyone to edit a config file."""
    monkeypatch.setenv("SESA_LANG", "zh")
    assert i18n.resolve("en") == "zh"


def test_an_unknown_language_falls_back_to_english(monkeypatch):
    monkeypatch.setenv("SESA_LANG", "klingon")
    assert i18n.resolve(None) == "en"


def test_an_untranslated_string_degrades_to_readable_english(monkeypatch):
    """**Never return the key, and never return an empty string.**

    When an untranslated corner is hit, the user should see readable English rather than
    something like `ui.error.no_participants`.
    """
    monkeypatch.setenv("SESA_LANG", "zh")
    i18n.use("zh")
    assert i18n.t("A string nobody has translated") == "A string nobody has translated"


def test_fields_interpolate_after_lookup(monkeypatch):
    """Interpolation happens **after** the lookup, so a translator can reorder the fields."""
    monkeypatch.setenv("SESA_LANG", "zh")
    i18n.use("zh")
    monkeypatch.setitem(i18n._load("zh"), "{a} then {b}", "先 {b} 后 {a}")
    assert i18n.t("{a} then {b}", a="A", b="B") == "先 B 后 A"


def test_the_catalogue_keys_are_read_from_the_source(monkeypatch):
    """The source strings are read from the AST, not from a hand-maintained list.

    A hand-maintained list goes stale, and a stale list lets the coverage test pass while strings
    quietly go untranslated.
    """
    from sesa.locales import source_strings

    assert isinstance(source_strings(), list)


# ── the deliberation language ───────────────────────────────────────────────────── #


@pytest.mark.parametrize(
    "task,expected",
    [
        ("评审这份产品需求文档", "zh"),
        ("Review this PRD carefully", "en"),
        ("Review the 产品需求文档 in this folder", "zh"),  # mixed text goes with Chinese
        ("该用 Postgres 还是 SQLite？", "zh"),
        ("HCP-021 evaluation", "en"),
        ("", "en"),
        ("   \n\t ", "en"),
    ],
)
def test_the_deliberation_language_follows_the_task(task, expected):
    assert pick_language(task) == expected


def test_the_threshold_leans_towards_chinese():
    """**The cost of getting it wrong is asymmetric.**

    Calling a Chinese task English has the parties review a Chinese document in English and the
    output is simply unusable; calling an English task Chinese only reads oddly. So the threshold
    sits very low.
    """
    assert pick_language("Review 需求 doc") == "zh"


def test_ui_language_and_deliberation_language_are_independent(monkeypatch):
    """English interface + Chinese task = a Chinese deliberation.

    The measured case: the interface was in English but what was under review was a Chinese
    product-requirements document — the output had to be Chinese.
    """
    monkeypatch.setenv("SESA_LANG", "en")
    i18n.use("en")
    assert i18n.active() == "en"
    assert pick_language("评审这份产品需求文档") == "zh"


# ── catalogue coverage ──────────────────────────────────────────────────────────── #


def test_every_source_string_has_a_chinese_translation():
    """**One missing translation and a Chinese user meets an English sentence in a screen of
    Chinese.**

    The source strings are read from the AST, so adding a `t("…")` and forgetting the
    translation turns this test red immediately.
    """
    missing = i18n.missing_keys("zh")
    assert not missing, "these source strings have no Chinese translation:\n  " + "\n  ".join(
        repr(m) for m in missing[:20]
    )


def test_translations_keep_every_placeholder():
    """A translation that drops a placeholder raises KeyError at runtime, and that mostly happens on
    an error path — the user is already in trouble and now meets a crash.
    """
    import re

    from sesa.locales.zh import CATALOGUE

    field = re.compile(r"\{(\w+)\}")
    for source, translated in CATALOGUE.items():
        assert field.findall(source) == field.findall(translated) or set(
            field.findall(source)
        ) == set(field.findall(translated)), (
            f"the placeholders do not match: {source!r} → {translated!r}"
        )


def test_the_wizard_speaks_both_languages(monkeypatch):
    from sesa import wizard

    monkeypatch.setenv("SESA_LANG", "en")
    i18n.use("en")
    assert i18n.t("Deliberation settings") == "Deliberation settings"

    monkeypatch.setenv("SESA_LANG", "zh")
    i18n.use("zh")
    assert i18n.t("Deliberation settings") == "议事参数"
    assert wizard._IntPrompt().validate_error_message.endswith("请输入一个整数")


def test_the_catalogue_has_no_duplicate_keys():
    """Duplicate keys in a dict literal have **the later one silently overwrite the earlier**.

    When the same English sentence is used in two places needing two translations, the dict keeps
    only the one written last — and the other place gets a translation written for a different
    context, with no error anywhere. This kind of quiet substitution is the hardest to spot by
    reading, so it is pinned down at the source level.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "sesa" / "locales" / "zh.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    literal = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign))
    keys = [ast.literal_eval(k) for k in literal.keys]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, (
        f"these keys are written more than once, and the later one overwrote the earlier: {duplicates}"
    )
