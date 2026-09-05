"""When the budget runs out, the results already achieved must not go with it.

The first real user's second deliberation (wall-clock limit 2000s):

    round 0  claude 8223 chars / kimi 3283 / dsh 5499
    round 1  7,000–10,000 chars each, **unresolved=0** (all three partial on each other)
    round 2  4,500–11,000 chars each, still unresolved=0
    round 3  309 seconds each, **0 characters produced**, all failed
    outcome  exhausted

With the wall clock at 1691/2000 a round 3 was started anyway, and each call's timeout was
``min(declared, max(1.0, remaining))`` — squeezed to **1 second** when the remainder was too
small, dooming that round. Then "every participant failed this round" went straight to
EXHAUSTED, **burying all three earlier rounds**.

What the user lost was a "consensus with reservations" that should have been theirs.
"""

from __future__ import annotations

import pytest

import sesa.events as ev
from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from sesa.types import Outcome
from sesa.workspace import LocalWorkspace
from tests.test_engine import participant


def test_a_round_that_cannot_finish_is_never_started(tmp_path):
    """If the remaining budget cannot support a round, do not start it — starting it only burns
    time and buries the results already in hand.
    """
    engine = Engine(
        [participant("alice"), participant("bob")],
        build_protocol("debate"),
        max_rounds=4,
        workspace=LocalWorkspace(tmp_path),
    )
    engine.budget.max_wall_seconds = 5.0  # far below MIN_ROUND_SECONDS
    engine.budget.started_at -= 4.0  # only 1s left

    assert engine._insufficient_budget() is not None
    assert "not enough for another round" in engine._insufficient_budget()
    assert "is not a failure" in engine._insufficient_budget(), (
        "it has to say the earlier results are still there — otherwise the user thinks the whole run was wasted"
    )


def test_plenty_of_budget_does_not_trip_the_guard(tmp_path):
    engine = Engine(
        [participant("alice"), participant("bob")],
        build_protocol("debate"),
        max_rounds=2,
        workspace=LocalWorkspace(tmp_path),
    )
    engine.budget.max_wall_seconds = 900.0
    assert engine._insufficient_budget() is None


def test_no_budget_limit_means_no_guard(tmp_path):
    engine = Engine(
        [participant("alice"), participant("bob")],
        build_protocol("debate"),
        max_rounds=1,
        workspace=LocalWorkspace(tmp_path),
    )
    engine.budget.max_wall_seconds = None
    assert engine._insufficient_budget() is None


@pytest.mark.asyncio
async def test_a_failed_final_round_falls_back_to_the_last_good_one(tmp_path, monkeypatch):
    """When a round is wiped out, wrap up on **the last round that actually produced something**
    rather than calling it dead across the board.

    It used to be `outcome = EXHAUSTED; break`: three complete rounds of deliberation judged
    "unfinished" because the fourth was wiped out, with the earlier results vanishing.

    What is asserted is **not** "the outcome must improve" — the parties here really do disagree
    and exhausted is correct. What is pinned down is: **the outcome is computed from the last
    valid round**, the failed round is dropped, and it says which round it fell back to.

    The implementation makes round 2 **produce no turns at all** (no subprocess is spawned). I
    tried both a tiny timeout and crash mode, and both raced under a full loaded run — green
    alone, occasionally red in the whole suite.
    **An intermittent red is worse than a steady one**: people learn "just run it again".
    """
    engine = Engine(
        [
            participant("alice", FAKE_VERDICT="disagree"),
            participant("bob", FAKE_VERDICT="disagree"),
        ],
        build_protocol("debate"),
        max_rounds=3,
        workspace=LocalWorkspace(tmp_path),
    )

    real_run_phase = Engine._run_phase

    async def barren(self, phase, phase_index, state, record):
        if record.index >= 2:
            return  # round 2: produce not one turn
            yield  # pragma: no cover - makes it an async generator
        async for event in real_run_phase(self, phase, phase_index, state, record):
            yield event

    monkeypatch.setattr(Engine, "_run_phase", barren)

    events = [e async for e in engine.run("该用 Postgres 还是 SQLite？")]
    verdict = next(e for e in events if isinstance(e, ev.VerdictFinal))

    assert verdict.rounds_used == 2, (
        f"the failed round should have been dropped; rounds_used={verdict.rounds_used}"
    )
    assert verdict.unresolved == 2, (
        "it should reflect the last valid round's result, rather than zeroing or voiding it"
    )

    note = next(
        e
        for e in events
        if isinstance(e, ev.ErrorEvent) and "Every participant failed this round." in e.message
    )
    assert "Wrapped up on the results of round 1" in note.message
    assert "is not voided by this one" in note.message


def test_the_fallback_uses_decide_outcome_not_a_hardcoded_failure():
    """What matters is **computing it with the decision function** rather than hard-coding
    EXHAUSTED.

    The last valid round of that user's run had unresolved=0 (all three partial on each other),
    which the decision function makes "consensus with reservations" — and a hard-coded EXHAUSTED
    erases it.
    """
    import inspect

    # Take that branch **structurally**, not by counting characters. It used to be "count 1200
    # characters forward", which fell short as soon as a comment got longer — what it actually
    # guarded was how long the comment was, not how the code was written.
    source = inspect.getsource(Engine.run)
    tail = source[source.index("Every participant failed this round.") :]
    failed_branch = tail[: tail.index("self._gather_evidence")]
    assert "decide_outcome" in failed_branch, "the failure branch is still hard-coding the outcome"
    assert "state.rounds.pop()" in source


@pytest.mark.asyncio
async def test_when_no_round_ever_succeeded_it_is_genuinely_exhausted(tmp_path):
    """With not one round succeeding it really is exhausted — and it must not pretend otherwise."""
    # **Use crash mode rather than a tiny timeout.** A 0.01s timeout races under a full loaded run —
    # I had already fixed one instance in this very file and missed this one: green over four solo
    # runs, occasionally red in the whole suite. An intermittent red is worse than a steady one.
    specs = [participant(pid, FAKE_MODE="crash") for pid in ("alice", "bob")]
    engine = Engine(
        specs, build_protocol("debate"), max_rounds=2, workspace=LocalWorkspace(tmp_path)
    )
    events = [e async for e in engine.run("议题")]
    verdict = next((e for e in events if isinstance(e, ev.VerdictFinal)), None)
    if verdict is not None:
        assert verdict.outcome == Outcome.EXHAUSTED.value


@pytest.mark.asyncio
async def test_round_zero_always_gets_a_chance(tmp_path):
    """When the budget is too small to finish a round, **round 0 still runs**.

    Running one and getting the "squeezed by the wall-clock budget" error is more useful than
    producing nothing — the latter leaves the user with no idea what happened. What this guard
    exists to prevent is a different thing: opening one more round after several have succeeded,
    dooming it and burying the results already in hand.

    (My first version put the guard at the very top of the loop, so a run with
    max_wall_seconds=2 ran no round at all and came out completely blank, knocking out two
    existing tests.)
    """
    import sys

    from sesa.budget import Budget

    slow = tmp_path / "slow.py"
    slow.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    specs = [participant(pid) for pid in ("alice", "bob")]
    for spec in specs:
        spec.options["command"] = [sys.executable, str(slow)]
        spec.options["timeout"] = 600

    engine = Engine(
        specs,
        build_protocol("debate"),
        budget=Budget(max_wall_seconds=2),
        max_rounds=2,
        workspace=LocalWorkspace(tmp_path),
    )
    events = [e async for e in engine.run("议题")]

    turns = [e for e in events if isinstance(e, ev.TurnEnd)]
    assert turns, "round 0 never ran at all — the user gets a complete blank"
    assert any("wall-clock budget" in (e.error or "") for e in turns), (
        "it has to say the budget squeezed it, rather than sending people to adjust the participant's timeout"
    )


@pytest.mark.asyncio
async def test_a_lone_failed_round_still_produces_a_verdict(tmp_path):
    """If the failed round is **the only** round, it must not be dropped.

    Dropping it makes state.current None, and the engine then takes the "no round was produced"
    early return — **not even emitting verdict.final**, leaving the caller with an event stream
    that has no outcome, while the CLI's exit-code logic rests entirely on that event.

    (This was a regression I created while fixing "a failed round burying good results", caught
    on the spot by test_total_failure_is_not_dressed_up_as_not_measured.)
    """
    specs = [participant(pid, FAKE_MODE="crash") for pid in ("a", "b")]
    engine = Engine(
        specs, build_protocol("debate"), max_rounds=2, workspace=LocalWorkspace(tmp_path)
    )
    events = [e async for e in engine.run("议题")]

    verdict = next((e for e in events if isinstance(e, ev.VerdictFinal)), None)
    assert verdict is not None, "the outcome event still has to be emitted when no round succeeded"
    assert verdict.outcome == Outcome.EXHAUSTED.value

    note = next(
        e
        for e in events
        if isinstance(e, ev.ErrorEvent) and "Every participant failed this round." in e.message
    )
    assert "No round had completed before this" in note.message, (
        "it must not claim falsely to have fallen back to some round"
    )
