"""The tests for parsing model output have to work **in both languages**.

These marker tables are not interface copy: they follow **the deliberation's language**,
which the task decides (`prompts.pick_language`). With the interface set to English and the
task in Chinese, the model speaks Chinese; and the other way round. So both languages'
markers have to be on the list at once and must not switch with the interface language.

Checking the English coverage while migrating to i18n turned up three real defects — **none
of them translation problems; the tests themselves were missing entries or inverted**.
"""

from __future__ import annotations

import pytest

from sesa.consensus.stance import (
    _coerce_verdict,
    _reads_like_reservation,
    _verification_from_note,
)

# ── agreement with reservations: missed on the English side ─────────────────────── #


@pytest.mark.parametrize(
    "note",
    [
        "但我仍不接受他的成本估算",
        "Agreed, though the timeline worries me",
        "Agree, albeit with one caveat",
        "Agree provided that we can take downtime",
        "My only concern is cost",
        "I agree, except for the deployment assumption",
        "唯一的疑虑是成本",
    ],
)
def test_a_qualified_agreement_is_recognised_in_both_languages(note):
    """**Missing one has real consequences**: agreement with reservations gets taken for agreement
    without, which is exactly what default-deny exists to prevent. The English side used to hold
    only three words.
    """
    assert _reads_like_reservation(note)


@pytest.mark.parametrize(
    "note",
    [
        "完全同意，没有保留",
        "完全同意，毫无保留",
        "I agree with no reservations",
        "Agree without reservation",
        "Fully agree",
        "完全同意",
    ],
)
def test_explicitly_denying_reservations_is_not_a_reservation(note):
    """The mirror-image error, and it costs just as much.

    "No reservation" contains "reservation", so **a clean agreement is downgraded**, throwing
    away a valid consensus for nothing. The denials have to be checked before the markers.
    """
    assert not _reads_like_reservation(note)


# ── verification notes: counter evidence read as supporting ─────────────────────── #


@pytest.mark.parametrize(
    "note",
    [
        "ran it, output differs from his claim",
        "could not reproduce it",
        "the result contradicts what he said",
        "运行失败，与其所述不符",
    ],
)
def test_a_refutation_is_not_read_as_a_reproduction(note):
    """Measured: `ran it, output differs from his claim` was judged **reproduced** — a piece of
    counter evidence taken as supporting, exactly backwards.
    """
    got = _verification_from_note("B", note)[0]
    assert got.result == "refuted"
    assert not got.grounds_agreement


def test_a_genuine_reproduction_still_grants_grounds():
    """The fix must not disable the normal path along with it."""
    assert _verification_from_note("B", "ran his pytest, matches what he said")[0].grounds_agreement
    assert _verification_from_note("B", "跑了他的 pytest，与其所述一致")[0].grounds_agreement


# ── synonyms for taking a position ──────────────────────────────────────────────── #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Agreed", "agree"),
        ("Concur", "agree"),
        ("Yes", "agree"),
        ("同意", "agree"),
        ("No", "disagree"),
        ("Disagree", "disagree"),
        ("反对", "disagree"),
        ("Partially", "partial"),
        ("Mostly", "partial"),
        ("部分同意", "partial"),
    ],
)
def test_free_form_verdicts_are_understood_in_both_languages(raw, expected):
    """Unrecognised, it is recorded under default-deny as no position taken — **a valid position
    silently thrown away**.
    """
    assert _coerce_verdict(raw) == expected


def test_the_marks_do_not_follow_the_interface_language(monkeypatch):
    """The interface language and the deliberation language are two things: with an English
    interface and a Chinese task the model speaks Chinese, and the tests have to recognise it
    all the same.
    """
    from sesa import i18n

    monkeypatch.setenv("SESA_LANG", "en")
    i18n.use("en")
    assert _reads_like_reservation("但我仍不接受他的成本估算")
    assert _coerce_verdict("同意") == "agree"
