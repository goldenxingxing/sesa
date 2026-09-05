"""A rule's **point of imposition** and its **point of announcement** must be paired
everywhere.

Background: when the `verified` bar landed, I changed the consensus assessment first and gave
the participants no way to comply — the prompt did not ask for that field at all. An audit
afterwards found the same error committed once on each of **five paths**. This file pins them
down one by one.

One general test: **wherever a participant can be downgraded, there has to be a road they can
take.**
"""

from __future__ import annotations

import inspect

from sesa import prompts
from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.stance import parse_verdict_lines
from sesa.state import DeliberationState, EvidenceRecord, RoundRecord, Turn
from sesa.types import ParticipantSpec, Stance, StanceOn, Verification

# --------------------------------------------------------------------------- # Hole 1: the degraded
# retry's line table has no room for verification
# --------------------------------------------------------------------------- #


def test_retry_prompt_offers_a_slot_for_verification():
    """A participant whose first parse failed is moved to the line table — and that format has to be
    able to express verification too.

    Otherwise its agree is doomed to be downgraded, and it **was never given a way to comply**.
    """
    from sesa import i18n

    # Both languages have to offer this slot. Missing one leaves that language's path with **the
    # rule imposed and no way given** — exactly what 14.25.1 exists to pin down.
    for lang, checked, unable in (
        ("en", "how you checked", "could not check"),
        ("zh", "核验", "查不了"),
    ):
        with i18n.scoped(lang):
            text = prompts.stance_retry_prompt(["bob"], "我刚才的发言")
        assert checked in text
        assert unable in text


def test_retry_lines_carry_verification_through():
    parsed = parse_verdict_lines(
        "confidence: 0.8\nbob: agree | | 跑了他的 pytest，与其所述一致\n", "a", 1, ["bob"]
    )
    assert parsed.stance_on["bob"].has_grounds


def test_retry_lines_accept_unable_as_an_honest_answer():
    parsed = parse_verdict_lines(
        "confidence: 0.8\nbob: agree | | 查不了：环境缺依赖\n", "a", 1, ["bob"]
    )
    on = parsed.stance_on["bob"]
    assert on.verified[0].how == "unable"
    assert not on.has_grounds  # honest, but with no foundation — both have to hold


# --------------------------------------------------------------------------- # Holes 2 / 3:
# verification records must reach the event stream and come back from a resume
# --------------------------------------------------------------------------- #


def test_verification_survives_a_round_trip_through_the_event_stream(tmp_path):
    """Without them, after a resume every agree has no foundation and is downgraded —
    **a resumed deliberation could never reach consensus**. The same illness as the truncation
    flag.
    """
    import dataclasses

    import sesa.events as ev
    from sesa.record import Recorder, load_state

    specs = [ParticipantSpec(id=p, adapter="cli") for p in ("a", "b")]
    recorder = Recorder(tmp_path, "run1")
    recorder.emit(
        ev.RunStart(
            run_id="run1", task="t", participants=["a", "b"], protocol="debate", max_rounds=2
        )
    )
    for pid in ("a", "b"):
        recorder.save_turn(Turn(pid, 0, 0, "draft", f"{pid} 的发言"))
    grounds = Verification(of="b 的主张", how="executed", result="reproduced", detail="跑过了")
    recorder.emit(
        ev.StanceEmit(
            round=0,
            participant="a",
            stance={
                "position": "p",
                "confidence": 0.9,
                "stance_on": {"b": "agree"},
                "verified": {"b": [dataclasses.asdict(grounds)]},
            },
        )
    )

    back = load_state(recorder.dir, participants=specs, max_rounds=2)
    restored = back.rounds[0].stances["a"].stance_on["b"]
    assert restored.has_grounds, "the verification records were lost on resume"
    assert restored.verified[0].detail == "跑过了"


def test_resume_does_not_invent_verification_from_malformed_records():
    """A record missing result is dropped whole — it must never be filled in as reproduced."""
    from sesa.record import _restore_stance_on

    on = _restore_stance_on("agree", "", [], [{"of": "x", "how": "executed"}])
    assert on.verified == [] and not on.has_grounds


# --------------------------------------------------------------------------- # Hole 4: the point of
# announcement must be the point of imposition
# --------------------------------------------------------------------------- #


def test_the_duty_is_announced_where_the_stance_is_requested():
    """The stance card is appended in one place shared by **every** protocol, while the evidence
    block is rendered only by the debate family.

    With the announcement left in each protocol's template, a participant under adversarial is
    downgraded by a rule they were never told and could not comply with — **and every new
    protocol commits it again**.
    """
    from sesa.engine import Engine

    source = inspect.getsource(Engine._run_move)
    assert "stance_instruction" in source
    assert "verification_duty" in source, (
        "the point of announcement came unhooked from the point of imposition"
    )


def test_duty_notice_names_where_to_go_and_looks_only_at_engine_facts():
    from sesa import i18n

    facts = [EvidenceRecord("bob", "pytest", 0, "34 passed", source="engine")]
    for lang, unable, free in (
        ("en", "could not check", "costs you nothing"),
        ("zh", "核验不了", "不扣分"),
    ):
        with i18n.scoped(lang):
            text = prompts.verification_duty(facts, ["bob"], {"bob": "sesa/run/bob"})
        assert "sesa/run/bob" in text, (
            "demanding verification without saying where to look is unreasonable"
        )
        assert unable in text and free in text

    claimed = [EvidenceRecord("bob", "pytest", 0, "我跑过了", source="claimed")]
    assert prompts.verification_duty(claimed, ["bob"], {}) == "", (
        "a self-report is not evidence and must not impose a verification duty on anyone else"
    )


def test_no_duty_notice_when_there_is_nothing_to_verify():
    """Printing "you must verify" in a pure design debate only invites invented verification
    records — which is worse than not verifying.
    """
    assert prompts.verification_duty([], ["bob"], {}) == ""


# --------------------------------------------------------------------------- # Hole 5: the bar may
# only rest on evidence the participant could see at the time
# --------------------------------------------------------------------------- #


def _round(index: int, ids: list[str], *, evidence=(), stances=()) -> RoundRecord:
    record = RoundRecord(index)
    record.turns = [Turn(p, index, 0, "draft", "话") for p in ids]
    record.evidence = list(evidence)
    record.stances = {s.participant: s for s in stances}
    return record


def test_round_zero_agreement_is_not_punished_for_unseen_evidence():
    """In round 0 nobody has seen anybody, and this round's evidence is executed only after everyone
    has spoken.

    Demanding verification of it punishes the impossible.
    """
    ids = ["a", "b"]
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=1
    )
    stances = [
        Stance(participant=p, round=0, confidence=0.9, stance_on={q: StanceOn(verdict="agree")})
        for p, q in (("a", "b"), ("b", "a"))
    ]
    state.rounds.append(
        _round(
            0,
            ids,
            evidence=[EvidenceRecord(p, "pytest", 0, "ok", source="engine") for p in ids],
            stances=stances,
        )
    )
    report = StanceMatrix().assess(state)
    assert report.unverified_agreements == []
    assert report.agreed == 2


def test_the_gate_bites_once_the_evidence_was_actually_visible():
    """The previous round's evidence is visible to the participants — not verifying it there should
    be downgraded.
    """
    ids = ["a", "b"]
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=2
    )
    state.rounds.append(
        _round(
            0, ids, evidence=[EvidenceRecord(p, "pytest", 0, "ok", source="engine") for p in ids]
        )
    )
    state.rounds.append(
        _round(
            1,
            ids,
            stances=[
                Stance(
                    participant=p, round=1, confidence=0.9, stance_on={q: StanceOn(verdict="agree")}
                )
                for p, q in (("a", "b"), ("b", "a"))
            ],
        )
    )
    report = StanceMatrix().assess(state)
    assert report.unverified_agreements == ["a → b", "b → a"]
