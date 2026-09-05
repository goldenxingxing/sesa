"""Verification of the README's four bottom lines (round four: rescanning modules already
reviewed).

**I did not write this file.** It comes from the fourth Sesa deliberation. This round
deliberately rescanned the same modules the first round had reviewed (engine / matrix /
stance / adoption / judge / evaluate), to answer "has this way of reviewing exhausted the
same ground yet".

The answer is **no**: 19 assertions failed against the code at the time, of which 18 were
verified as real and 1 partly held (adjusted by the author on review, with the reasoning in
that test).

The most embarrassing was **created by my own fix an hour earlier**: to fix one monotonicity
inversion (a weaker agreement buying a better outcome), I added `if report.min_confidence and`
to the confidence bar — and 0.0 is falsy, so a confidence of 0.00 was judged "consensus with
reservations" while 0.01 was judged "unfinished".
**A second inversion of the same shape, in the same function, within an hour.**
"""

from __future__ import annotations

import sys
from pathlib import Path

from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.stance import parse_verdict_lines
from sesa.types import Outcome, Stance, StanceOn

sys.path.insert(0, str(Path(__file__).parent))
from test_bottom_lines import _state

#: the same ordering table as round3: the README's "six grades of outcome", strongest to weakest
RANK = {
    Outcome.CONSENSUS: 0,
    Outcome.CONSENSUS_WITH_RESERVATIONS: 1,
    Outcome.PARTIAL_COVERAGE_CONSENSUS: 2,
    Outcome.DEADLOCK: 3,
    Outcome.EXHAUSTED: 3,
}


def _stance(pid, others, verdict, conf, residuals=None):
    return Stance(
        participant=pid,
        round=1,
        position=f"{pid} 的立场",
        confidence=conf,
        stance_on={
            o: StanceOn(verdict=verdict, reason="理由", residuals=list(residuals or []))
            for o in others
        },
    )


def _outcome(conf, verdict="partial", residuals=("尚未接受的具体点",)):
    matrix = StanceMatrix()
    state = _state(
        ["a", "b"],
        {
            "a": _stance("a", ["b"], verdict, conf, residuals),
            "b": _stance("b", ["a"], verdict, conf, residuals),
        },
    )
    report = matrix.assess(state)
    return matrix.decide_outcome(report, rounds_left=0), report


# ═══════════════════════════════════════════════════════════════════════════ #
# 1. Lower confidence, better outcome — the monotonicity inversion, second entrance
# ═══════════════════════════════════════════════════════════════════════════ #


def test_lower_confidence_must_not_yield_a_better_outcome():
    """``matrix.py:237`` ``if report.min_confidence and report.min_confidence < threshold``.

    ``0.0`` is falsy, so **the bar is skipped exactly when the confidence is at its lowest**.

    Measured, over the same stance cards with confidence as the only variable:

    * ``confidence=0.01`` → ``exhausted`` (⏳ discussion unfinished)
    * ``confidence=0.00`` → ``consensus_with_reservations`` (🟡 consensus with reservations)

    Less certainty bought a better banner. This is exactly the property round three's
    ``test_a_weaker_agreement_must_not_yield_a_better_outcome`` was meant to plug, and that test
    used 0.10 as its parameter, missing the one value at which the gate it fixed fails.
    """
    worse, _ = _outcome(0.00)
    better, _ = _outcome(0.01)

    assert RANK[worse] >= RANK[better], (
        f"confidence 0.00 is judged {worse} while 0.01 is judged {better} — "
        "less certainty buying a better outcome. matrix.py:237 asks 'is there a confidence' with a truthiness test, "
        "and 0.0 is both the missing-value sentinel and a legitimate value."
    )


def test_the_confidence_gate_holds_at_zero():
    """The confidence bar has to hold on **every** downgrade path — including when the value is 0.0.

    This is the previous item stated directly: the report itself wrote "lowest confidence 0.00
    is below the threshold 0.60" into the blockers, while the outcome came out as a consensus
    grade.
    """
    outcome, report = _outcome(0.00)
    assert report.min_confidence < StanceMatrix().confidence_threshold
    assert outcome not in (
        Outcome.CONSENSUS,
        Outcome.CONSENSUS_WITH_RESERVATIONS,
        Outcome.PARTIAL_COVERAGE_CONSENSUS,
    ), (
        f"lowest confidence {report.min_confidence} is below the threshold and it is judged {outcome}; blockers={report.blockers}"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 2. The same root cause's second outlet: the report contradicts itself
# ═══════════════════════════════════════════════════════════════════════════ #


def test_the_report_must_not_both_claim_consensus_and_no_stance_cards():
    """``matrix.py:120`` and ``matrix.py:237`` are driven by the same ``> 0.0`` test.

    ``known_conf`` (matrix.py:98) discards a whole stance card whose confidence is 0.0, so
    REPORT.md's "why it did not converge" says "there is not a single usable stance card" while
    RESULT.md's banner says "🟡 consensus with reservations". The two deliverables contradict
    each other.
    """
    outcome, report = _outcome(0.00)
    claims_no_cards = any("没有任何可用的立场卡" in b for b in report.blockers)
    claims_consensus = outcome in (
        Outcome.CONSENSUS,
        Outcome.CONSENSUS_WITH_RESERVATIONS,
        Outcome.PARTIAL_COVERAGE_CONSENSUS,
    )
    assert not (claims_no_cards and claims_consensus), (
        f"the blockers say 'there is not a single usable stance card' while the outcome is {outcome}. "
        f"And unknown_participants={report.unknown_participants} — "
        "both cards parsed successfully; their confidence is merely 0.0."
    )


def test_a_parsed_stance_card_is_not_reported_as_missing():
    """A stance card parsed successfully ⇒ you cannot say "there is not a single usable stance card".

    "Could not be parsed" and "parsed, and the certainty is 0" are two things — precisely the
    sort of two things bottom line 2 requires to be accounted separately.
    """
    _, report = _outcome(0.00)
    assert not report.unknown_participants, (
        "the premise does not hold: every stance card should have parsed"
    )
    assert not any("没有任何可用的立场卡" in b for b in report.blockers), (
        "both stance cards parsed successfully (unknown_participants is empty) "
        f"and it reports 'there is not a single usable stance card': {report.blockers}"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 3. This is not an edge case: the engine's T2 degraded path produces 0.0 by default
# ═══════════════════════════════════════════════════════════════════════════ #


def test_the_t2_fallback_path_does_not_silently_disable_the_gate():
    """A confidence omitted on the line-table degraded path must not thereby bypass the confidence
    bar.

    The original assertion said "an omitted confidence should be 0.0" — which was exactly the
    defect: 0.0 doubling as the sentinel for "not reported" and as the legitimate value "I am
    very unsure". An omission now parses to ``None`` and default-deny blocks consensus, rather
    than disguising itself as a specific low score.
    """
    from sesa.consensus.stance import parse_verdict_lines

    stance = parse_verdict_lines("b: partial | 我还没接受他的成本估算", "a", 1, ["b"])

    assert stance is not None
    assert stance.confidence is None, "omitted means not reported, and must not pass for 0.0"
    assert stance.stance_on["b"].verdict == "partial"


def test_an_agree_carrying_residuals_is_not_an_unconditional_agree():
    """``stance.py:161`` checks only ``partial``, and an ``agree`` marches straight through with
    its residuals.

    The participant wrote "I broadly agree, but I still do not accept his cost estimate", which
    by stance.py:109-114's rule should land in partial or be treated as no position taken.
    """
    from sesa.consensus.stance import parse_stance

    text = (
        "我基本同意，但有一条没接受。\n\n"
        "```json\n"
        '{"position":"a 的立场","confidence":0.9,'
        '"stance_on":{"b":{"verdict":"agree","reason":"大体认可",'
        '"residuals":["但我仍不接受他的成本估算"]}}}\n'
        "```"
    )
    stance = parse_stance(text, "a", 1, ["b"])
    assert stance is not None
    on = stance.stance_on["b"]
    assert on.residuals, "the premise does not hold: the residuals should have been parsed"
    assert on.verdict != "agree", (
        f"an agree carrying the residual '{on.residuals[0]}' is recorded as an agree without reservation — "
        "while default-deny requires 'resolved' if and only if there is an explicit agreement **without reservation**."
    )


def test_residuals_attached_to_an_agree_are_not_silently_dropped():
    """``matrix.py:87`` ``if on.verdict == "partial" and on.residuals``.

    Residuals are collected only under ``partial``. A residual hanging off an ``agree`` enters
    neither ``report.residuals`` nor the ``reservations`` count — it **disappears entirely**
    from the deliverable, while bottom line 4 requires that an open disagreement come with a way
    out.
    """
    from sesa.consensus.stance import parse_stance

    def card(pid, other):
        return parse_stance(
            "```json\n"
            f'{{"position":"{pid} 的立场","confidence":0.9,'
            f'"stance_on":{{"{other}":{{"verdict":"agree","reason":"大体认可",'
            f'"residuals":["我仍不接受 {other} 的成本估算"]}}}}}}\n```',
            pid,
            1,
            [other],
        )

    matrix = StanceMatrix()
    report = matrix.assess(_state(["a", "b"], {"a": card("a", "b"), "b": card("b", "a")}))
    outcome = matrix.decide_outcome(report, rounds_left=0)

    assert report.residuals, (
        f"two residuals are in black and white in the stance card and report.residuals is empty: {report.residuals}; "
        f"reservations={report.reservations}, outcome={outcome}"
    )


def test_a_conditional_agreement_does_not_outrank_an_honest_partial():
    """Hanging the reservations off an ``agree`` buys a better outcome than filling in ``partial``
    honestly.

    The same two reservations:
    * filled in as ``partial`` → ``consensus_with_reservations`` (🟡 consensus with reservations)
    * filled in as ``agree`` with the reservations stuffed into ``residuals`` → ``consensus``
      (✅ full consensus)

    So a choice of format changed the outcome while the substance was identical — **saying you
    agree really counting as agreement**, precisely what default-deny exists to prevent.
    """
    from sesa.consensus.stance import parse_stance

    def card(pid, other, verdict):
        return parse_stance(
            "```json\n"
            f'{{"position":"{pid} 的立场","confidence":0.9,'
            f'"stance_on":{{"{other}":{{"verdict":"{verdict}","reason":"大体认可",'
            f'"residuals":["我仍不接受 {other} 的成本估算"]}}}}}}\n```',
            pid,
            1,
            [other],
        )

    matrix = StanceMatrix()

    def outcome_for(verdict):
        state = _state(["a", "b"], {"a": card("a", "b", verdict), "b": card("b", "a", verdict)})
        return matrix.decide_outcome(matrix.assess(state), rounds_left=0)

    honest = outcome_for("partial")
    conditional = outcome_for("agree")

    assert RANK[conditional] >= RANK[honest], (
        f"the same reservations, filled in as partial, are judged {honest}, "
        f"while filled in as agree with the reservations stuffed into residuals they are judged {conditional}."
    )


def test_the_t2_line_format_also_drops_a_conditional_agreement():
    """``stance.py:249`` ``residuals=[note] if verdict == "partial" else []``.

    The second outlet of the same error as the JSON entrance. The degraded retry's prompt
    (``prompts.py:159``) says only "for partial you must write down what you have not accepted"
    and does not forbid writing ``| but I do not accept …`` after ``agree`` — and the model did
    exactly that, whereupon the sentence was demoted to a ``reason`` and entered no accounting
    at all.
    """
    stance = parse_verdict_lines(
        "confidence: 0.9\na: agree | 但我仍不接受他的成本估算", "b", 1, ["a"]
    )
    assert stance is not None
    on = stance.stance_on["a"]
    assert on.verdict != "agree" or on.residuals, (
        "'agree | but I still do not accept his cost estimate' is recorded as an agree without reservation, "
        f"and that reservation survives only in reason={on.reason!r}, entering neither residuals nor the reservation count."
    )
