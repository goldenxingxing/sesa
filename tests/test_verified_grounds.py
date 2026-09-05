"""default-deny extended from "stance parsing" to "evidence".

It started with run 20260901-103359: one party wrote in ``premises`` that "my execution
happened before they wrote the test file, so I cannot verify it myself and am relying on their
reported output", while conceding in its position. The protocol **had no slot for saying "I
did not check"**, so the consensus assessment could not see it at all — I found it by reading
the premises by hand.
"""

from __future__ import annotations

import pytest

from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.stance import parse_stance
from sesa.state import DeliberationState, EvidenceRecord, RoundRecord, Turn
from sesa.types import ParticipantSpec, Stance, StanceOn, Verification


def _state(*stances: Stance, evidence: list[EvidenceRecord] | None = None) -> DeliberationState:
    """Set up a deliberation: the evidence is in **the previous round**, the position in this one.

    That order is part of the behaviour under test. This round's evidence is executed by the
    engine only after everyone has spoken, while the stance card is written before that — so the
    bar can rest only on the previous round's evidence, or it demands that a participant verify
    something that did not exist when they wrote the card.
    """
    ids = sorted({s.participant for s in stances})
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=2
    )
    earlier = RoundRecord(0)
    earlier.turns = [Turn(pid, 0, 0, "draft", "上一轮的发言") for pid in ids]
    earlier.evidence = evidence or []
    state.rounds.append(earlier)

    record = RoundRecord(1)
    record.turns = [Turn(s.participant, 1, 0, "revise", "说了话") for s in stances]
    record.stances = {s.participant: s for s in stances}
    state.rounds.append(record)
    return state


def _agree(who: str, whom: str, *, verified: bool) -> Stance:
    grounds = (
        [Verification(of=f"{whom} 的主张", how="executed", result="reproduced", detail="跑过了")]
        if verified
        else []
    )
    return Stance(
        participant=who,
        round=1,
        confidence=0.9,
        stance_on={whom: StanceOn(verdict="agree", verified=grounds)},
    )


def _engine_evidence(who: str) -> EvidenceRecord:
    return EvidenceRecord(participant=who, cmd="pytest", exit_code=0, summary="ok", source="engine")


# --------------------------------------------------------------------------- # The bar itself
# --------------------------------------------------------------------------- #


def test_unverified_agreement_does_not_resolve_the_cell():
    """The other side produced checkable evidence and you said agree without checking any of it —
    that is not agreement.
    """
    state = _state(
        _agree("a", "b", verified=False),
        _agree("b", "a", verified=True),
        evidence=[_engine_evidence("a"), _engine_evidence("b")],
    )
    report = StanceMatrix().assess(state)
    assert report.matrix["a"]["b"] == "unknown"
    assert report.matrix["b"]["a"] == "agree"
    assert report.unverified_agreements == ["a → b"]


def test_unverified_agreement_is_not_recorded_as_opposition():
    """The downgrade goes to "not measured", not to "opposed" — the two mean opposite things for what
    to do next.
    """
    state = _state(
        _agree("a", "b", verified=False),
        _agree("b", "a", verified=False),
        evidence=[_engine_evidence("a"), _engine_evidence("b")],
    )
    report = StanceMatrix().assess(state)
    assert report.opposed == 0
    assert report.unmeasured == 2
    assert "agreements with no verification submitted" in report.describe_unresolved()


def test_verified_agreement_still_resolves():
    """The fix must not disable the normal path with it."""
    state = _state(
        _agree("a", "b", verified=True),
        _agree("b", "a", verified=True),
        evidence=[_engine_evidence("a"), _engine_evidence("b")],
    )
    report = StanceMatrix().assess(state)
    assert report.unresolved == 0 and report.agreed == 2
    assert report.unverified_agreements == []


# --------------------------------------------------------------------------- # The bar's boundary:
# it must not shut pure design debates out
# --------------------------------------------------------------------------- #


def test_agreement_stands_when_the_other_offered_nothing_to_verify():
    """In a deliberation with no executable evidence (a pure design debate) nobody can verify anybody.

    Demanding verification unconditionally would make such deliberations **permanently unable to
    reach consensus** — and those are exactly the occasions that most need several parties. The
    bar has to be conditional on the other side really having produced something checkable.
    """
    state = _state(_agree("a", "b", verified=False), _agree("b", "a", verified=False))
    report = StanceMatrix().assess(state)
    assert report.unresolved == 0 and report.agreed == 2


def test_self_claimed_evidence_does_not_trigger_the_duty_to_verify():
    """ "I ran it, it passed" is a claim awaiting verification, not evidence.

    Treating it as checkable evidence lets someone impose a verification duty on everyone else by
    mere assertion, over an "evidence" that was never executed.
    """
    claimed = EvidenceRecord(
        participant="b", cmd="pytest", exit_code=0, summary="我跑过了", source="claimed"
    )
    state = _state(
        _agree("a", "b", verified=False), _agree("b", "a", verified=False), evidence=[claimed]
    )
    assert StanceMatrix().assess(state).unresolved == 0


@pytest.mark.parametrize("verdict", ["partial", "disagree"])
def test_the_gate_only_applies_to_clean_agreement(verdict):
    """A reservation or an objection carries its reasons already and needs no verification as a
    foundation.
    """
    residuals = ["还没接受的点"] if verdict == "partial" else []
    state = _state(
        Stance(
            participant="a",
            round=1,
            confidence=0.9,
            stance_on={"b": StanceOn(verdict=verdict, reason="理由", residuals=residuals)},
        ),
        _agree("b", "a", verified=True),
        evidence=[_engine_evidence("a"), _engine_evidence("b")],
    )
    assert StanceMatrix().assess(state).matrix["a"]["b"] == verdict


# --------------------------------------------------------------------------- # Parsing: no defaults
# filled in on the model's behalf
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "item",
    [
        '{"of": "x", "how": "executed"}',  # result missing
        '{"of": "x", "result": "reproduced"}',  # how missing
        '{"how": "executed", "result": "reproduced"}',  # of missing: no idea which
        # claim was checked
        '{"of": "x", "how": "跑了跑了", "result": "reproduced"}',  # how is not in the
        # vocabulary
    ],
)
def test_malformed_verification_is_dropped_not_defaulted(item):
    """A missing result must never become reproduced — that manufactures an "I checked it" out of
    nothing, and the whole foundation of an agreement rests on it.
    """
    card = '{"position":"p","stance_on":{"b":{"verdict":"agree","verified":[' + item + "]}}}"
    stance = parse_stance(card, "a", 0, ["b"])
    assert stance is not None
    assert stance.stance_on["b"].verified == []
    assert not stance.stance_on["b"].has_grounds


def test_claiming_unable_but_reporting_a_result_is_treated_as_unable():
    """Saying "could not check" while reporting a result contradicts itself."""
    card = (
        '{"position":"p","stance_on":{"b":{"verdict":"agree","verified":'
        '[{"of":"x","how":"unable","result":"reproduced","detail":"环境缺依赖"}]}}}'
    )
    stance = parse_stance(card, "a", 0, ["b"])
    assert stance.stance_on["b"].verified[0].result == "unable"
    assert not stance.stance_on["b"].has_grounds


def test_refuted_verification_cannot_ground_agreement():
    """Counter evidence cannot be used to support an agreement."""
    card = (
        '{"position":"p","stance_on":{"b":{"verdict":"agree","verified":'
        '[{"of":"x","how":"executed","result":"refuted","detail":"跑出来跟他说的不一样"}]}}}'
    )
    stance = parse_stance(card, "a", 0, ["b"])
    assert not stance.stance_on["b"].has_grounds


def test_unable_is_a_respectable_answer_and_is_preserved():
    """ "Could not check" stays in the record: it is what the reader judges the conclusion's quality
    by, not noise.
    """
    card = (
        '{"position":"p","stance_on":{"b":{"verdict":"partial","residuals":["待核"],"verified":'
        '[{"of":"x","how":"unable","result":"unable","detail":"他没给出处"}]}}}'
    )
    stance = parse_stance(card, "a", 0, ["b"])
    v = stance.stance_on["b"].verified[0]
    assert (v.how, v.result, v.detail) == ("unable", "unable", "他没给出处")


def test_a_non_compliant_participant_fails_loudly_not_silently():
    """A model that does not fill in verified will visibly fail to reach consensus — **that cost is
    deliberate**.

    If the downgrade went to partial (consensus with reservations), two unverified parties would
    get a "consensus" badge; and that is the hole this project keeps falling into: taking "not
    measured" for "a weaker agreement". Better to fail visibly than to succeed suspiciously.
    """
    state = _state(
        _agree("a", "b", verified=False),
        _agree("b", "a", verified=False),
        evidence=[_engine_evidence("a"), _engine_evidence("b")],
    )
    report = StanceMatrix().assess(state)
    assert report.converged is False
    assert report.coverage == 0.0
    # and the report has to say it is "an agreement with no verification", not leave a bare unknown:
    # the reader would think the other party took no position, when in fact they did and it merely
    # has no foundation.
    assert report.unverified_agreements == ["a → b", "b → a"]
    assert "agreements with no verification submitted" in report.describe_unresolved()


def test_an_earlier_rounds_evidence_still_imposes_the_duty():
    """Evidence does not expire just because a round has passed.

    This was found on the spot by deepseek in the 13th round of self-review, landing on my fix from
    the day before. I had changed the test from "this round's evidence" to "the immediately
    preceding round's evidence", fixing one extreme (demanding verification of something that did
    not exist when the card was written) and swinging to the other:

        round 0: both a and b produce engine evidence
        round 1: only b produces new evidence
        round 2: both agree without reservation and neither submits a verification
        → b's agreement with a sails straight through

    **"I produced no new evidence last round" thus became a ready-made way around the verification
    duty**, while a's round-0 evidence had held all along, in plain sight of everyone.
    Evidence is invalidated for exactly one reason: the code changed (``stale``).
    """
    from sesa.state import EvidenceRecord, RoundRecord, Turn

    ids = ["a", "b"]
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=3
    )

    def _round(index, evidence=(), stances=()):
        record = RoundRecord(index)
        record.turns = [Turn(p, index, 0, "draft", "话") for p in ids]
        record.evidence = list(evidence)
        record.stances = {s.participant: s for s in stances}
        return record

    def _fact(pid):
        return EvidenceRecord(pid, "pytest", 0, "ok", source="engine")

    state.rounds.append(_round(0, evidence=[_fact("a"), _fact("b")]))
    state.rounds.append(_round(1, evidence=[_fact("b")]))  # a produced no new evidence this
    # round
    state.rounds.append(
        _round(
            2,
            stances=[
                Stance(
                    participant=p, round=2, confidence=0.9, stance_on={q: StanceOn(verdict="agree")}
                )
                for p, q in (("a", "b"), ("b", "a"))
            ],
        )
    )

    report = StanceMatrix().assess(state)
    assert sorted(report.unverified_agreements) == ["a → b", "b → a"]


def test_stale_evidence_stops_imposing_the_duty_even_if_it_is_cumulative():
    """Cumulative is not permanent: when the code changes the old evidence should expire, or it turns
    back into "demanding a reproduction of a result that no longer holds".
    """
    from sesa.state import EvidenceRecord, RoundRecord, Turn

    ids = ["a", "b"]
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=2
    )
    earlier = RoundRecord(0)
    earlier.turns = [Turn(p, 0, 0, "draft", "话") for p in ids]
    earlier.evidence = [
        EvidenceRecord(p, "pytest", 0, "ok", source="engine", stale=True) for p in ids
    ]
    state.rounds.append(earlier)

    later = RoundRecord(1)
    later.turns = [Turn(p, 1, 0, "revise", "话") for p in ids]
    later.stances = {
        p: Stance(participant=p, round=1, confidence=0.9, stance_on={q: StanceOn(verdict="agree")})
        for p, q in (("a", "b"), ("b", "a"))
    }
    state.rounds.append(later)

    assert StanceMatrix().assess(state).unverified_agreements == []
