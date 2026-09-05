"""Verification of the README's four bottom lines at the delivery layer.

**I did not write this file.** It comes from a Sesa deliberation: the participant claude read
this repository's source in its own git worktree and wrote these tests, 8 of which failed
against the code at the time. Verified mechanically one by one, **all held**, corresponding
to 8 defects (see DESIGN.md 14.20).

The most serious: `sesa run` crashed for certain in **a real terminal** (TypeError). The
whole test suite and every manual check of mine redirected output to a file, which takes the
JSON branch — so this path, which a new user hits at step one, had never once been executed.

The turn that wrote these tests was ultimately marked failed (claude hit its session quota),
but its working copy had already been committed to the branch — **the output survived, the
turn did not**.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sesa.consensus import rapporteur as rap
from sesa.consensus.matrix import StanceMatrix
from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from sesa.record import Recorder, new_run_id
from sesa.state import DeliberationState, RoundRecord
from sesa.types import Outcome, ParticipantSpec, Stance, StanceOn

FAKE = str(Path(__file__).parent / "fake_agent.py")


def participant(pid: str, **env) -> ParticipantSpec:
    return ParticipantSpec(
        id=pid,
        adapter="cli",
        role=f"{pid} 的立场倾向",
        options={
            "command": [sys.executable, FAKE],
            "prompt": "stdin",
            "timeout": 30,
            "env": {"FAKE_ID": pid, **{k: str(v) for k, v in env.items()}},
        },
    )


def _state(ids, stances, index=1):
    st = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ids],
        max_rounds=9,
    )
    rec = RoundRecord(index=index)
    rec.stances.update(stances)
    st.rounds.append(rec)
    return st


# ═══════════════════════════════════════════════════════════════════════════ #
# 1. sesa run cannot finish one round in a real terminal
# ═══════════════════════════════════════════════════════════════════════════ #


def test_consensus_matrix_renders_in_tty_mode():
    """cli.py:387 passes ``unresolved`` as a keyword argument, and it is a read-only property.

    ``stream_json = as_json or not sys.stdout.isatty()`` (cli.py:211) — pytest captures stdout ⇒
    isatty() is false ⇒ all 264 existing tests take the JSON branch and never execute this.
    A person running ``sesa run`` in a terminal gets a TypeError at the end of round 0.
    """
    import sesa.events as ev
    from sesa.cli import render_matrix_from_event

    event = ev.ConsensusUpdate(
        round=0,
        unresolved=2,
        min_confidence=0.9,
        matrix={"a": {"b": "unknown"}, "b": {"a": "unknown"}},
        state="open",
    )
    render_matrix_from_event(event)  # currently raises TypeError


# ═══════════════════════════════════════════════════════════════════════════ #
# 2. Bottom line 2: coverage must be delivered with the outcome (types.py:310's own words)
# ═══════════════════════════════════════════════════════════════════════════ #


async def _partial_coverage_run(tmp_path):
    """Two assess each other agree and the third hands in no stance card — partial_coverage_consensus."""
    run_id = new_run_id()
    engine = Engine(
        [participant("claude"), participant("kimi"), participant("mute", FAKE_MODE="no_stance")],
        build_protocol("debate"),
        matrix=StanceMatrix(stability_window=2),
        recorder=Recorder(tmp_path, run_id),
        max_rounds=2,
    )
    events = [e async for e in engine.run("该用 Postgres 还是 SQLite？")]
    return run_id, events, next(e for e in events if e.t == "verdict.final")


async def test_partial_coverage_outcome_is_reachable(tmp_path):
    """The premise: this path is not dead code, which is what makes the next two mean anything."""
    _, _, verdict = await _partial_coverage_run(tmp_path)
    assert verdict.outcome == Outcome.PARTIAL_COVERAGE_CONSENSUS.value


async def test_delivered_coverage_is_not_a_lie(tmp_path):
    """The outcome says "partial coverage" while result.json's coverage is 1.0.

    engine.py:733 constructs ``Result`` without passing coverage / unmeasured_cells, so they stay
    at the dataclass defaults of 1.0 / []. A missing value would at least be visible; 1.0 is
    inverted.
    """
    run_id, _, verdict = await _partial_coverage_run(tmp_path)
    assert verdict.outcome == Outcome.PARTIAL_COVERAGE_CONSENSUS.value
    data = json.loads((tmp_path / "runs" / run_id / "result.json").read_text("utf-8"))
    assert data["coverage"] < 1.0, (
        f"the delivered coverage is {data['coverage']} while the outcome says partial coverage"
    )
    assert data["unmeasured_cells"], "not one unmeasured cell was delivered"


async def test_result_md_warns_that_some_cells_were_never_measured(tmp_path):
    """RESULT.md is the version people read and has to state "some cells were not measured"."""
    run_id, _, _ = await _partial_coverage_run(tmp_path)
    text = (tmp_path / "runs" / run_id / "RESULT.md").read_text("utf-8")
    banner = text.splitlines()[2]
    assert banner != "partial_coverage_consensus", (
        f"the banner degraded to a bare enum value: {banner!r}"
    )
    assert "未测到" in text or "覆盖" in text, (
        "nowhere in the document does it say anything was not measured"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 3. Bottom lines 2/3: not_measured must not be written up as "not settled"
# ═══════════════════════════════════════════════════════════════════════════ #


async def test_not_measured_is_not_reported_as_failure_to_agree(tmp_path):
    """engine.py:646's comment says this is exactly what bottom line 2 exists to prevent — and it
    only stopped the enum value.

    report.py:111's grades have no NOT_MEASURED, so RESULT.md still writes "this deliberation did
    not reach consensus"; OUTCOME_BANNER has none either, and the banner degrades to a bare
    ``not_measured``. reflect has nobody seeing anybody and structurally cannot peer-assess, and
    "not measured" was written up as "not settled".
    """
    run_id = new_run_id()
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("reflect"),
        recorder=Recorder(tmp_path, run_id),
        max_rounds=2,
    )
    events = [e async for e in engine.run("该用 Postgres 还是 SQLite？")]
    assert next(e for e in events if e.t == "verdict.final").outcome == Outcome.NOT_MEASURED.value

    text = (tmp_path / "runs" / run_id / "RESULT.md").read_text("utf-8")
    assert text.splitlines()[2] != "not_measured", "the banner degraded to a bare enum value"
    assert "并未达成共识" not in text, (
        "wrote 'this protocol does not measure consensus' as 'they did not settle it'"
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# 4. Bottom line 2: "someone objected" and "not measured" merged on the fallback path
# ═══════════════════════════════════════════════════════════════════════════ #


def test_reconcile_does_not_call_unmeasured_cells_explicit_opposition():
    """rapporteur.py:162 uses ``report.unresolved`` = opposed + unmeasured.

    With not one disagree cell, reconcile still fires, and what goes into the event stream is
    "the matrix shows N explicit oppositions" — while ``_disagreements_from_matrix`` moves only
    disagree cells ⇒ what it fills in is an empty list. It both misreports the cause and promises
    a backfill that never happened.
    """
    state = _state(
        ["a", "b"],
        {
            "a": Stance("a", 1, confidence=0.9, stance_on={"b": StanceOn("agree")}),
            "b": Stance.as_unknown("b", 1),  # the stance card failed to parse: not
            # measured, not opposition
        },
    )
    report = StanceMatrix().assess(state)
    assert report.opposed == 0 and report.unmeasured == 1

    draft = rap.reconcile(
        {"conclusion": "x", "grounds": [], "disagreements": [], "minority": {}}, state, report
    )
    note = draft.get("reconciled", "")
    assert "明确反对" not in note, f"described 1 unmeasured cell as explicit opposition: {note!r}"
    if note:
        assert draft["disagreements"], (
            "claims to have backfilled mechanically, and what it filled in is empty"
        )


# ═══════════════════════════════════════════════════════════════════════════ #
# 5. Bottom line 2: with not one agree cell it should not be called consensus
#    (types.py:245's own words)
# ═══════════════════════════════════════════════════════════════════════════ #


def test_zero_agreed_cells_is_never_any_kind_of_consensus():
    """decide_outcome's structural floor checks coverage, not agreed.

    a's stance on b is a partial with residuals and b's stance card did not parse ⇒ zero agrees in
    the whole run, and the outcome is still ``partial_coverage_consensus``.
    """
    state = _state(
        ["a", "b"],
        {
            "a": Stance(
                "a", 1, confidence=0.9, stance_on={"b": StanceOn("partial", "r", ["残差"])}
            ),
            "b": Stance.as_unknown("b", 1),
        },
    )
    report = StanceMatrix().assess(state)
    assert report.agreed == 0
    outcome = StanceMatrix().decide_outcome(report, rounds_left=0)
    assert outcome not in (
        Outcome.CONSENSUS,
        Outcome.CONSENSUS_WITH_RESERVATIONS,
        Outcome.PARTIAL_COVERAGE_CONSENSUS,
    ), f"not one agree cell was measured and the outcome is {outcome}"


def test_single_participant_cannot_be_a_consensus():
    """An empty matrix ⇒ no blockers ⇒ converged=True ⇒ consensus, with agreed at 0.

    The Engine has a ``len(participants) < 2`` guard so this cannot be reached end to end; but
    ``StanceMatrix`` is a public class that can be imported on its own, and the hole is real as a
    matter of contract.
    """
    state = _state(["solo"], {"solo": Stance("solo", 1, confidence=0.9)})
    report = StanceMatrix().assess(state)
    assert report.agreed == 0
    assert not report.converged, "zero agree cells, and it is judged converged"


# ═══════════════════════════════════════════════════════════════════════════ #
# 6. Bottom line 3: reword the residuals and deadlock detection never fires
# ═══════════════════════════════════════════════════════════════════════════ #


@pytest.mark.parametrize("churn", [False, True])
def test_rewording_a_residual_cannot_defer_deadlock_forever(churn):
    """matrix.py:161 takes "the set of residuals changed" as an objective signal of movement, while
    the residuals are self-reported too.

    The substantive state is identical in both groups: b disagrees every round, a is partial every
    round. The only difference is whether a rewords the same residual.
    """
    matrix = StanceMatrix(stability_window=2)
    state = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ("a", "b")],
        max_rounds=99,
    )
    outcomes = []
    for n in range(5):
        residual = (
            f"我还没接受你关于扩容的那一条（第 {n} 次重述）"
            if churn
            else "我还没接受你关于扩容的那一条"
        )
        record = RoundRecord(index=n)
        record.stances["a"] = Stance(
            "a", n, confidence=0.9, stance_on={"b": StanceOn("partial", "r", [residual])}
        )
        record.stances["b"] = Stance(
            "b", n, confidence=0.9, stance_on={"a": StanceOn("disagree", "不行")}
        )
        state.rounds.append(record)
        record.consensus = matrix.assess(state)
        outcomes.append(matrix.decide_outcome(record.consensus, rounds_left=99 - n))

    assert Outcome.DEADLOCK in outcomes, (
        "five consecutive rounds without substantive movement and deadlock detection never fired"
        f" (churn={churn}, stalled stayed at {state.rounds[-1].consensus.stalled_rounds})"
    )
