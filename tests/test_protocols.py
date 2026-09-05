"""How the protocol layer schedules.

Two easily-broken things get the attention:
* **rotation** happens between rounds, not by swapping the proposer inside one round
* **deferred rendering** really can see what earlier phases of this round produced
"""

from __future__ import annotations

import pytest

from sesa.protocols import available, build
from sesa.state import DeliberationState, EvidenceRecord, RoundRecord, Turn
from sesa.types import Outcome, ParticipantSpec

IDS = ("claude", "kimi", "gpt5")


def make_state(**kw) -> DeliberationState:
    return DeliberationState(
        task="选 Postgres 还是 SQLite",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in IDS],
        max_rounds=4,
        **kw,
    )


def commit_round(state: DeliberationState, index: int, turns: list[Turn]) -> RoundRecord:
    """Simulate the Engine: push this round's record after plan()."""
    record = RoundRecord(index, turns=turns)
    state.rounds.append(record)
    return record


def test_builtin_protocols():
    assert available() == ["adversarial", "council", "debate", "ensemble", "reflect"]


def test_unknown_protocol_lists_alternatives():
    with pytest.raises(ValueError, match="debate"):
        build("nope")


def test_round_zero_is_always_parallel_independent_drafts():
    """Independent drafts are the only source of diversity, and no protocol and no turn_taking may
    break that.
    """
    for turn_taking in ("parallel", "sequential"):
        phases = build("debate", turn_taking=turn_taking).plan(make_state())
        assert len(phases) == 1
        assert [m.participant for m in phases[0].moves] == list(IDS)
        assert all(m.kind == "draft" for m in phases[0].moves)


def test_debate_parallel_round_is_one_phase():
    state = make_state()
    commit_round(state, 0, [Turn(i, 0, 0, "draft", f"{i} 的初稿") for i in IDS])
    phases = build("debate").plan(state)
    assert len(phases) == 1
    assert [m.participant for m in phases[0].moves] == list(IDS)


def test_sequential_rotates_speaking_order_each_round():
    """In sequential mode, position bias is cancelled out by round-robin rotation."""
    state = make_state()
    orders = []
    for r in range(3):
        commit_round(state, r, [Turn(i, r, 0, "draft", "x") for i in IDS])
        phases = build("debate", turn_taking="sequential").plan(state)
        orders.append([p.moves[0].participant for p in phases])
    assert orders[0][0] != orders[1][0] != orders[2][0]
    assert all(sorted(o) == sorted(IDS) for o in orders)


def test_council_forces_parallel():
    """All-see-all semantics require everyone to speak from the same snapshot."""
    state = make_state()
    commit_round(state, 0, [Turn(i, 0, 0, "draft", "x") for i in IDS])
    phases = build("council", turn_taking="sequential").plan(state)
    assert len(phases) == 1


def test_ensemble_is_single_round():
    state = make_state()
    assert build("ensemble").plan(state)
    commit_round(state, 0, [Turn(i, 0, 0, "draft", "x") for i in IDS])
    assert build("ensemble").plan(state) == []


def test_adversarial_input_mode_has_no_proposer():
    """When the thing under review is the task input, everyone attacks and the last phase becomes a
    cross-check.
    """
    protocol = build("adversarial", proposer="input")
    assert protocol.resolve_proposer(make_state()) is None
    phases = protocol.plan(make_state())
    assert [p.label for p in phases] == ["Attacks in parallel", "Cross-check"]
    assert [m.participant for m in phases[0].moves] == list(IDS)


def test_adversarial_attack_phase_emits_no_stance():
    """In the attack phase there is no "position" yet, only accusations."""
    phases = build("adversarial", proposer="input").plan(make_state())
    assert all(not m.expects_stance for m in phases[0].moves)


def test_adversarial_proposer_stays_fixed_within_a_round():
    """One whole propose→attack→respond cycle is one round, and the proposer must not change inside
    it.
    """
    protocol = build("adversarial", proposer="rotate")
    state = make_state()
    proposer = protocol.resolve_proposer(state)
    phases = protocol.plan(state)
    assert phases[0].label == f"Proposal: {proposer}"
    assert phases[2].label == f"Response: {proposer}"
    assert proposer not in [m.participant for m in phases[1].moves]


def test_adversarial_rotates_proposer_between_rounds():
    protocol = build("adversarial", proposer="rotate")
    state = make_state()
    seen = []
    for r in range(3):
        seen.append(protocol.resolve_proposer(state))
        commit_round(state, r, [Turn(seen[-1], r, 0, "draft", "x")])
    assert seen == list(IDS)  # over N rounds everyone has been proposer once


def test_adversarial_lazy_prompt_sees_this_round_output():
    """The response phase's prompt has to see this round's proposal and everyone's attacks."""
    protocol = build("adversarial", proposer="rotate")
    state = make_state()
    proposer = protocol.resolve_proposer(state)
    phases = protocol.plan(state)

    record = commit_round(state, 0, [Turn(proposer, 0, 0, "draft", "提案正文ABC")])
    for pid in IDS:
        if pid != proposer:
            record.turns.append(Turn(pid, 0, 1, "attack", f"{pid} 的攻击XYZ"))

    rendered = phases[2].moves[0].render(state)
    assert "提案正文ABC" in rendered
    assert "的攻击XYZ" in rendered


def test_adversarial_rejects_bad_proposer():
    with pytest.raises(ValueError, match="is not a valid setting"):
        build("adversarial", proposer="nobody").resolve_proposer(make_state())


def test_residuals_are_fed_back_to_the_other_side_by_default():
    """The residuals are the whole substance of a partial. Without feeding them back, "the opposing
    side is the natural auditor" does not exist.
    """
    from sesa.types import Stance, StanceOn

    state = make_state()
    record = commit_round(state, 0, [Turn(i, 0, 0, "draft", f"{i} 的初稿") for i in IDS])
    record.stances["kimi"] = Stance(
        participant="kimi",
        round=0,
        position="kimi 的立场",
        confidence=0.8,
        stance_on={
            "claude": StanceOn(verdict="partial", reason="理由", residuals=["尚未接受的点甲"])
        },
    )
    rendered = build("debate").plan(state)[0].moves[0].render(state)
    assert "尚未接受的点甲" in rendered


def test_residual_feedback_can_be_turned_off():
    """Feeding them back costs more content, so it can be turned off — but it is on by default."""
    from sesa.types import Stance, StanceOn

    state = make_state(share_residuals=False)
    record = commit_round(state, 0, [Turn(i, 0, 0, "draft", f"{i} 的初稿") for i in IDS])
    record.stances["kimi"] = Stance(
        participant="kimi",
        round=0,
        position="kimi 的立场",
        confidence=0.8,
        stance_on={
            "claude": StanceOn(verdict="partial", reason="理由", residuals=["尚未接受的点甲"])
        },
    )
    rendered = build("debate").plan(state)[0].moves[0].render(state)
    assert "尚未接受的点甲" not in rendered
    assert "理由" in rendered  # reason is still rendered


def test_reflect_participants_never_see_each_other():
    """The no-speaker control group: everyone sees only their own last round.

    The literature (2026): reflection alone produces about 37% change of position, and once a
    no-speaker control is added to existing peer-pressure benchmarks most of the "conformity" is
    still there — they over-attributed the change to social influence. Without this baseline the
    sentence "the debate changed X% of the participants' positions" does not hold.
    """
    state = make_state()
    commit_round(state, 0, [Turn(i, 0, 0, "draft", f"{i} 的独有内容") for i in IDS])
    rendered = build("reflect").plan(state)[0].moves[0].render(state)
    speaker = build("reflect").plan(state)[0].moves[0].participant
    assert f"{speaker} 的独有内容" in rendered
    for other in IDS:
        if other != speaker:
            assert f"{other} 的独有内容" not in rendered


def test_reflect_asks_for_no_stance_card():
    """Never seeing the others means never being able to take a position on them — demanding one
    anyway only forces invented judgements.
    """
    state = make_state()
    assert all(not m.expects_stance for m in build("reflect").plan(state)[0].moves)
    commit_round(state, 0, [Turn(i, 0, 0, "draft", "x") for i in IDS])
    assert all(not m.expects_stance for m in build("reflect").plan(state)[0].moves)


def test_reflect_shows_own_evidence_but_never_the_others():
    """Whether your own tests passed **is not social information**.

    Withholding it too would have the control group measuring not "no peers" but "nothing at
    all", and the difference could not be attributed to anything; it makes no product sense
    either — running a code task under reflect while unable to see your own test results.
    """
    state = DeliberationState(
        task="修个 bug",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ("alice", "bob")],
        max_rounds=2,
    )
    first = RoundRecord(0)
    first.turns = [
        Turn("alice", 0, 0, "draft", "我的方案"),
        Turn("bob", 0, 0, "draft", "我的方案"),
    ]
    first.evidence = [
        EvidenceRecord(participant="alice", cmd="pytest -q", exit_code=0, summary="11 passed"),
        EvidenceRecord(participant="bob", cmd="pytest -q", exit_code=1, summary="3 failed"),
    ]
    state.rounds = [first]

    moves = {
        m.participant: m.render(state)
        for phase in build("reflect").plan(state)
        for m in phase.moves
    }

    assert "11 passed" in moves["alice"]
    assert "3 failed" not in moves["alice"], "reflect has nobody seeing anybody throughout"
    assert "3 failed" in moves["bob"]
    assert "11 passed" not in moves["bob"]


async def test_reflect_reports_not_measured_rather_than_a_failed_consensus(tmp_path):
    """ "Not measured" is not "not settled".

    reflect has nobody seeing anybody and structurally cannot produce peer assessment, so every
    cell is necessarily unknown. Carrying on with default-deny reports it as "the rounds ran out
    with disagreements still open" — labelling missing data as disagreement, exactly what this
    project's second bottom line exists to prevent. The concrete consequence is specific too:
    `sesa run --protocol reflect` would then always exit 2, and wiring it into CI is a permanent
    failure.
    """
    from sesa.engine import Engine
    from sesa.record import Recorder, new_run_id
    from tests.test_engine import drive, final, participant

    engine = Engine(
        [participant("alice"), participant("bob")],
        build("reflect"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=2,
    )
    verdict = final(await drive(engine))

    assert verdict.outcome == Outcome.NOT_MEASURED.value
    assert verdict.outcome != Outcome.EXHAUSTED.value, "not measured is not the same as not settled"


async def test_debate_still_reports_a_real_failure_to_agree(tmp_path):
    """The counterexample: a protocol that does peer-assess still has to report deadlock honestly
    when they cannot agree.
    """
    from sesa.consensus.matrix import StanceMatrix
    from sesa.engine import Engine
    from sesa.record import Recorder, new_run_id
    from tests.test_engine import drive, final, participant

    engine = Engine(
        [
            participant("alice", FAKE_VERDICT="disagree"),
            participant("bob", FAKE_VERDICT="disagree"),
        ],
        build("debate"),
        matrix=StanceMatrix(stability_window=2),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=6,
    )
    verdict = final(await drive(engine))

    assert verdict.outcome == Outcome.DEADLOCK.value
    assert verdict.unresolved > 0
