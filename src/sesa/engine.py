"""The deliberation engine — protocol, consensus, adapters and budget strung into one
streaming loop.

The Engine **knows nothing about terminals**: it only yields :class:`Event`. The CLI, the
TUI, the SDK, MCP and third-party products all consume the same event stream. This is the
only correct posture for "easy to build on".

The execution contract (matching :meth:`Protocol.plan`'s docstring):

1. ``plan(state)`` — this round's RoundRecord is not on the stack yet
2. append this round's RoundRecord
3. run the Phases in order; the Moves inside a Phase run **concurrently**, with their events
   merged live
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from . import adoption as adopt
from . import events as ev
from . import patch, prompts
from .adapters import Adapter
from .adapters import build as build_adapter
from .budget import Budget
from .consensus import rapporteur as rap
from .consensus.matrix import StanceMatrix
from .consensus.stance import parse_stance, parse_verdict_lines, strip_stance_block
from .evidence import EvidenceRunner
from .i18n import scoped, t
from .protocols import Phase, Protocol
from .record import Recorder
from .report import render_report, render_result
from .state import DeliberationState, RoundRecord, Turn
from .types import (
    Done,
    Outcome,
    ParticipantSpec,
    Result,
    Stance,
    TextDelta,
    ThinkingDelta,
    Usage,
)
from .workspace import Checkout, LocalWorkspace, Workspace

_SENTINEL = object()


class Engine:
    """The driver of one deliberation."""

    def __init__(
        self,
        participants: list[ParticipantSpec],
        protocol: Protocol,
        *,
        matrix: StanceMatrix | None = None,
        budget: Budget | None = None,
        recorder: Recorder | None = None,
        max_rounds: int = 4,
        share_thinking: str = "never",
        share_residuals: bool = True,
        rapporteur: str = "rotate",
        workspace: Workspace | None = None,
        evidence: EvidenceRunner | None = None,
        turn_timeout: float = 1800.0,
    ) -> None:
        if len(participants) < 2:
            raise ValueError(t("A deliberation needs at least 2 participants"))
        self.participants = participants
        self.protocol = protocol
        self.matrix = matrix or StanceMatrix()
        self.budget = budget or Budget()
        self.recorder = recorder
        self.max_rounds = max_rounds
        #: Human interventions queued by the interface or the SDK, merged into state when the next
        #: round begins.
        #: The caller is not allowed to touch `state.pending_injections` directly: that object is
        #: created inside `run()` and is out of reach from outside, and exposing state would turn an
        #: internal structure into a public contract.
        self._queued_injections: list[str] = []
        #: A human request to wrap up early. Once set, **the current round finishes** and no new
        #: round starts; it goes straight to drafting — rather than being cut off mid-round. Half a
        #: round of turns yields no stance card, that round's money is wasted, and the consensus
        #: assessment records it as "not measured".
        self._stop_requested = False
        self.share_thinking = share_thinking
        self.share_residuals = share_residuals
        self.rapporteur = rapporteur
        # **The default is "the directory you started in", not an empty temp directory.**
        # EphemeralWorkspace used to be the default, so without --repo every participant was placed
        # in an empty temp directory — the task said "review the documents in this folder", they
        # could see not one of them, and they produced a perfectly normal-looking review all the
        # same.
        self.workspace = workspace or LocalWorkspace()
        self.evidence = evidence
        self.turn_timeout = turn_timeout
        #: participant -> their own working copy. In a code task each gets a worktree; one shared
        #: directory has them trampling each other, with no way to tell who changed what.
        self._checkouts: dict[str, Checkout] = {}
        #: the final round's cross-test results, for the report to render
        self._cross_matrix = None
        self._self_tests: dict[tuple[str, str], int] = {}
        self._adapters: dict[str, Adapter] = {p.id: build_adapter(p) for p in participants}

    # ------------------------------------------------------------------ # The main loop
    # ------------------------------------------------------------------ #

    def inject(self, text: str) -> bool:
        """Queue one human intervention for **the next round**.

        **It does not take effect immediately** — the round being written cannot see it. The caller
        (the TUI) has to say so, or the user thinks the key did nothing. Returns whether it was
        queued: an empty string is not.
        """
        if not (text := text.strip()):
            return False
        self._queued_injections.append(text)
        return True

    def request_stop(self) -> None:
        """Request an early wrap-up: **write it up once this round finishes**, and start no new round.

        It does not cut the current round off mid-way — half a turn yields no stance card, that
        round's money is wasted, and the consensus assessment only records it as "not measured".
        To stop right now, use Ctrl-C, and that path leaves a RunAborted behind.
        """
        self._stop_requested = True

    async def run(
        self,
        task: str,
        *,
        prior: DeliberationState | None = None,
        inject: str | None = None,
        resumed_from: str | None = None,
    ) -> AsyncIterator[ev.Event]:
        """Drive one deliberation.

        Passing the state restored from a previous run as ``prior`` makes it a **resume**:
        everyone's earlier turns and positions come in as completed rounds, and ``inject`` is the
        information the human added. This turns a deadlock from a terminus into an ordinary step in
        the loop — they cannot agree → they tell you what is missing → you supply it → they carry
        on.
        """
        run_id = self.recorder.run_id if self.recorder else "adhoc"
        if prior is not None:
            state = prior
            state.max_rounds = self.max_rounds
            state.share_thinking = self.share_thinking
            state.share_residuals = self.share_residuals
        else:
            state = DeliberationState(
                task=task,
                participants=self.participants,
                max_rounds=self.max_rounds,
                share_thinking=self.share_thinking,
                share_residuals=self.share_residuals,
            )
        state.stances_requested = self.protocol.measures_consensus
        if inject:
            state.pending_injections.append(inject)
        self.budget.reset()
        self._checkouts = self.workspace.prepare(state.ids)
        # The branch names have to reach the prompt, or a participant has no way to go and verify
        # another's artefacts.
        state.branches = {
            pid: c.branch for pid, c in self._checkouts.items() if getattr(c, "branch", None)
        }
        self._snapshots = {}
        self._last_exit = {}
        self._adoption_events = []
        # Cleared every run. Running a second deliberation on the same Engine would otherwise leave
        # the previous run's results in these two, so "the hardest evidence" and "tests that pass
        # only for their author" would point at the previous run's participants.
        self._cross_matrix = None
        self._self_tests = {}
        self._briefings = {}
        self._shadow_checked = False
        self._warned_inert_budget = False
        self._warned_no_isolation = False

        yield self._emit(
            ev.RunStart(
                run_id=run_id,
                task=state.task,
                participants=state.ids,
                protocol=self.protocol.name,
                max_rounds=self.max_rounds,
            )
        )
        if resumed_from:
            yield self._emit(ev.RunResume(from_run=resumed_from, inject=inject or ""))

        for spec in self.participants:
            try:
                text = prompts.load_briefing(spec)
            except ValueError as exc:
                # Say so when it cannot be read, but do not drag the whole run down — the others can
                # deliberate perfectly well. What separates this from "quietly treat it as empty" is
                # that an event is left behind and can be found afterwards.
                yield self._emit(ev.ErrorEvent(where=f"briefing {spec.id}", message=str(exc)))
                continue
            if not text:
                continue
            self._briefings[spec.id] = len(text)
            if self.recorder is not None:
                self.recorder.save_briefing(spec.id, text)
            yield self._emit(
                ev.Briefing(
                    participant=spec.id,
                    chars=len(text),
                    source=str(spec.options.get("briefing", ""))[:200],
                    excerpt=text[:300],
                )
            )

        outcome: Outcome | None = None
        # On a resume, max_rounds means "how many more rounds", not counting the rounds already done
        limit = len(state.rounds) + min(self.max_rounds, self.protocol.max_useful_rounds(state))

        while state.round_index < limit and not self._stop_requested:
            # **If the remaining budget cannot support a round, do not start the round.**
            # The budget used to be checked only after a round ended, so with the wall clock at
            # 1691/2000 a new round still started — and each call's timeout was `min(declared,
            # max(1.0, remaining))`, squeezed down to **1 second** when the remainder was too small,
            # dooming that round. Measured (the first real user): everyone wrote 5,000–10,000
            # characters in the first three rounds, in the fourth the three of them ran 309 seconds
            # each and produced 0 characters, and the run was judged exhausted, **burying all three
            # earlier rounds' results under that one**. **Round 0 always gets a chance.** When the
            # budget is too small to finish a round at all, running one and getting the "squeezed by
            # the wall-clock budget" error is more useful than producing nothing — the latter leaves
            # the user with no idea what happened. What this guard exists to prevent is a different
            # thing: opening one more round after several have already succeeded, dooming it and
            # burying the results already in hand.
            if state.rounds and (shortfall := self._insufficient_budget()) is not None:
                yield self._emit(ev.ErrorEvent(where="budget", message=shortfall))
                break

            # Queued human interventions are merged in **before the new round opens**, so that
            # plan() sees them while rendering prompts. Merging them after plan wastes the round.
            if self._queued_injections:
                state.pending_injections.extend(self._queued_injections)
                self._queued_injections.clear()
            # **plan() has to be under the task's language too.** Round 0's prompts are evaluated in
            # place inside plan() (not deferred), so a scope covering only _run_move leaves them
            # already formed in the interface language — measured: a Chinese task received English
            # prompts.
            with scoped(prompts.pick_language(state.task)):
                phases = self.protocol.plan(state)
            if not phases:
                break

            drafter = self._pick_rapporteur(state)
            yield self._emit(ev.RoundStart(round=state.round_index, rapporteur=drafter))

            record = RoundRecord(state.round_index, injections=list(state.pending_injections))
            state.rounds.append(record)  # Contract step 2: push it only after plan
            for text in state.pending_injections:
                yield self._emit(ev.HumanInject(round=record.index, kind="inject", text=text))
            state.pending_injections.clear()

            for index, phase in enumerate(phases):
                async for event in self._run_phase(phase, index, state, record):
                    yield event

            if not any(t.ok for t in record.turns):
                # **A failed round must not erase the rounds that succeeded.**
                # Measured: everyone handed in 5,000–10,000 characters over the first three rounds,
                # round 2 already had unresolved=0 (consensus with reservations), the fourth round
                # was wiped out by the budget running out — and this went straight to EXHAUSTED +
                # break, so the user got "unfinished" and the result they should have had vanished.
                # The right thing: drop this round from the record and wrap up on the results of
                # **the last round that actually produced something**; only with no successful round
                # at all is it really exhausted. **Drop it only when there is a previous round to
                # fall back to.**
                # If this is the only round, dropping it makes state.current None, and the engine
                # then takes the "no round was produced" early return — **not even emitting the
                # outcome event**, leaving the caller with an event stream that has no
                # verdict.final. (The bottom-line test
                # test_total_failure_is_not_dressed_up_as_not_measured caught this on the spot: I
                # created it while fixing "a failed round burying good results".)
                fell_back = len(state.rounds) > 1
                if fell_back:
                    state.rounds.pop()
                yield self._emit(
                    ev.ErrorEvent(
                        where=f"round {record.index}",
                        message=(
                            t("Every participant failed this round.")
                            + " "
                            + (
                                t(
                                    "Wrapped up on the results of round {n} — that round "
                                    "was complete and is not voided by this one.",
                                    n=state.current.index,
                                )
                                if fell_back
                                else t(
                                    "No round had completed before this, so the run has no result."
                                )
                            )
                        ),
                    )
                )
                if not fell_back:
                    outcome = Outcome.EXHAUSTED
                    break
                # Let the outcome be judged on the last **valid** round's consensus, rather than
                # exhausted across the board.
                report = state.current.consensus or self.matrix.assess(state)
                state.current.consensus = report
                outcome = (
                    self.matrix.decide_outcome(report, rounds_left=0, budget_exhausted=True)
                    or Outcome.EXHAUSTED
                )
                break

            # Evidence must be executed by the engine itself — a participant's "I ran it, the result
            # was …" is only a claim awaiting verification (see DESIGN.md §6.2).
            async for event in self._gather_evidence(record):
                yield event

            # Copy detection has to come **after** the evidence — only with this round's self-test
            # results in hand can "converged on the other party" be told from converging on the
            # right or the wrong side.
            if self.workspace.isolates_participants:
                for event in self._detect_adoption(record):
                    yield event
            elif not self._warned_no_isolation:
                # Said once only. In a shared directory everyone's snapshot is identical by
                # construction — computing it anyway is not only wasted (measured 5× slower) but
                # produces an empty "no copying detected" conclusion, when what actually happened is
                # "this question cannot be asked in this kind of workspace".
                self._warned_no_isolation = True
                yield self._emit(
                    ev.ErrorEvent(
                        where="adoption",
                        message=t(
                            "The participants share one working directory, so "
                            "**adoption detection and cross-testing cannot run** — both "
                            "compare each participant's own artefacts, and here there is "
                            "only one copy. Use `--repo` (a git worktree per participant) "
                            "when you need them."
                        ),
                    )
                )

            report = self.matrix.assess(state)
            record.consensus = report
            yield self._emit(
                ev.ConsensusUpdate(
                    round=record.index,
                    unresolved=report.unresolved,
                    min_confidence=round(report.min_confidence, 3),
                    matrix=report.matrix,
                    state="converged"
                    if report.converged
                    else ("stalled" if report.stalled_rounds else "open"),
                )
            )

            if (inert := self.budget.unenforceable()) and not self._warned_inert_budget:
                # Reported once per deliberation: this is a configuration matter, not a state that
                # changes by round.
                self._warned_inert_budget = True
                yield self._emit(ev.ErrorEvent(where="budget", message=inert))

            if warning := self.budget.near_limit():
                yield self._emit(
                    ev.BudgetWarn(
                        spent_usd=round(self.budget.spent_usd, 4),
                        limit_usd=self.budget.max_usd,
                        elapsed_s=round(self.budget.elapsed, 1),
                        limit_s=self.budget.max_wall_seconds,
                        reason=warning,
                    )
                )

            outcome = self.matrix.decide_outcome(
                report,
                rounds_left=limit - state.round_index,
                # A human wrap-up and an exhausted budget are equivalent on the point that matters —
                # there is no next round: the assessment has to know that **no later round can be
                # counted on to resolve the disagreements**, or it will report an optimistic
                # intermediate state on the assumption that rounds remain.
                budget_exhausted=bool(self.budget.exceeded()) or self._stop_requested,
            )
            if outcome is not None:
                break

        if state.current is None:
            yield self._emit(ev.ErrorEvent(where="run", message=t("no round was produced")))
            return

        if outcome is None:
            outcome = Outcome.EXHAUSTED
        if state.current.consensus is None:
            state.current.consensus = self.matrix.assess(state)

        async for event in self._cross_test(state):
            yield event
        self._commit_branches(run_id)

        async for event in self._finalize(state, outcome, run_id):
            yield event

    # ------------------------------------------------------------------ # Phase execution:
    # concurrent within a phase, events merged live
    # ------------------------------------------------------------------ #

    async def _run_phase(
        self, phase: Phase, phase_index: int, state: DeliberationState, record: RoundRecord
    ) -> AsyncIterator[ev.Event]:
        if not phase:
            return
        factories = [
            (lambda m=move: self._run_move(m, phase_index, state, record)) for move in phase.moves
        ]
        async for event in _merge(factories):
            yield event

    async def _run_move(
        self, move, phase_index: int, state: DeliberationState, record: RoundRecord
    ) -> AsyncIterator[ev.Event]:
        pid = move.participant
        adapter = self._adapters[pid]
        started = time.perf_counter()

        yield self._emit(ev.TurnStart(round=record.index, participant=pid))

        text_parts: list[str] = []
        think_parts: list[str] = []
        usage = Usage.unknown()
        error: str | None = None
        truncated = False
        try:
            # **The prompts use the task's language, the interface uses the interface's.**
            # Asking in Chinese should get you a deliberation in Chinese even when the interface is
            # in English. This scope covers only the assembly of prompts: the text scrolling in the
            # terminal is rendered by `_drive` consuming events, follows the interface language, and
            # is unaffected.
            with scoped(prompts.pick_language(state.task)):
                prompt = move.render(state)
                workdir = self._cwd_of(pid) if self._applies_code(pid) else None
                if workdir:
                    # A participant that cannot write files **cannot read them either**. Without the
                    # working directory fed to it, it can only guess — measured, two DeepSeek
                    # participants both stated plainly that "we have not seen the actual contents of
                    # SPEC.md" and each guessed a version.
                    prompt += patch.render_workspace(workdir)
                visible = self._visible_others(state, record, pid)
                if move.expects_stance:
                    # A position can only be taken on someone whose turn was **actually seen**. In
                    # round 0 nobody has seen anybody, and demanding stance_on there leaves the
                    # model nothing to do but invent — which would have the engine declaring
                    # consensus after a round in which nothing was contested.
                    prompt += prompts.stance_instruction(visible)
                    # The verification duty must be announced **here**, not in each protocol's
                    # template.
                    # The stance card is appended in this one place shared by every protocol, while
                    # the evidence block is rendered only by the debate family — so a participant
                    # under adversarial would be downgraded by a rule they were never told and could
                    # not comply with. **The point of imposition and the point of announcement must
                    # be the same place**, or every new protocol commits the same error again.
                    prompt += prompts.verification_duty(
                        self._fresh_evidence(state), visible, state.branches
                    )
                if workdir:
                    # It has to come **after** the stance-card instruction: only the debate family
                    # appends a stance card, so the same paragraph sits in different positions under
                    # different protocols. Measured, 3 of 16 turns in the reflect group wrote code
                    # without marking the path, against 0 of 16 in the debate group — the control
                    # baseline was thereby systematically weakened on code tasks, and the two groups
                    # became incomparable.
                    prompt += patch.INSTRUCTION.format()
            async for chunk in adapter.stream(
                prompt,
                system=prompts.system_prompt(state, pid),
                cwd=self._cwd_of(pid),
                timeout=self._turn_budget(pid),
                context={"round": str(record.index), "phase": str(phase_index)},
            ):
                if isinstance(chunk, TextDelta):
                    text_parts.append(chunk.text)
                    yield self._emit(
                        ev.TurnDelta(round=record.index, participant=pid, text=chunk.text)
                    )
                elif isinstance(chunk, ThinkingDelta):
                    # Goes to disk and to people only; whether it enters others' context is decided
                    # by share_thinking
                    think_parts.append(chunk.text)
                    yield self._emit(
                        ev.TurnThinking(round=record.index, participant=pid, text=chunk.text)
                    )
                elif isinstance(chunk, Done):
                    usage = chunk.usage
                    truncated = chunk.truncated
        except Exception as exc:
            # One participant failing (the process dying, a timeout, an HTTP error) must not end the
            # whole deliberation: record it as a failed turn and the others carry on; only everyone
            # failing in one round aborts it.
            error = f"{type(exc).__name__}: {exc}"
            if getattr(exc, "timed_out", False) and self._budget_capped(pid):
                # The cap was squeezed down by **the run-wide wall-clock budget**, not by this
                # participant being slow. Without saying so, the user goes and adjusts the
                # participant's timeout, which does nothing at all.
                error += "\n" + t(
                    "(the real cap comes from the run-wide wall-clock budget of {cap:.0f}s, "
                    "{used:.0f}s of which is used — raising this participant's timeout "
                    "does nothing; raise budget.max_wall_seconds)",
                    cap=self.budget.max_wall_seconds,
                    used=self.budget.elapsed,
                )

        self.budget.add(usage)
        raw = "".join(text_parts)
        body = strip_stance_block(raw) if move.expects_stance else raw.strip()

        turn = Turn(
            participant=pid,
            round=record.index,
            phase=phase_index,
            kind=move.kind,
            text=body,
            raw=raw,
            thinking="".join(think_parts),
            usage=usage,
            duration_s=time.perf_counter() - started,
            error=error,
            truncated=truncated,
        )
        record.turns.append(turn)
        if self.recorder:
            self.recorder.save_turn(turn)

        yield self._emit(
            ev.TurnEnd(
                round=record.index,
                participant=pid,
                chars=len(body),
                duration_s=round(turn.duration_s, 2),
                usage={"known": usage.known, "in": usage.input_tokens, "out": usage.output_tokens},
                error=error,
                truncated=truncated,
                phase=turn.phase,
                kind=turn.kind,
            )
        )

        # **A failed turn may still have produced something.** For a participant killed by a
        # timeout, the complete code blocks already emitted are valid work — `extract_files` only
        # accepts closed fences, a half-written one cannot be extracted anyway, and writing them out
        # is safe.
        # Measured twice: "the outcome says failure while the working copy actually holds
        # something", and both times it was found by chance — once claude had finished writing the
        # test file before hitting its quota, once kimi had output before timing out. An agent CLI
        # writing its own files bypasses this branch entirely — its changes were there all along,
        # merely unrecorded in the event stream, making "what landed on disk" a black box.
        if turn.text.strip() and self._applies_code(pid) and (cwd := self._cwd_of(pid)):
            # Writing to disk must precede gathering evidence — otherwise the verification runs the
            # previous round's code
            result = patch.apply_files(raw, cwd)
            # Emit the event even when not one character landed. Measured, a participant wrote
            # 25,000 characters across 25 code fences, not one carrying name=, and the working
            # directory did not move — while the event stream held nothing at the time, looking
            # exactly like "nothing needed changing this round".
            yield self._emit(
                ev.FilesApplied(
                    round=record.index,
                    participant=pid,
                    files=[a.path for a in result.applied],
                    rejected=[f"{p}（{why}）" for p, why in result.rejected],
                    fences_seen=patch.count_fences(raw),
                )
            )

        if move.expects_stance and turn.complete:
            async for event in self._collect_stance(pid, raw, state, record, visible):
                yield event
        elif move.expects_stance and turn.truncated:
            # Someone cut off has usually not reached their conclusion — if the stance card really
            # was at the end, it has most likely been cut off by now; and even if one is extracted
            # by luck, that is half a sentence passing for a position. default-deny: better to
            # record unknown and block consensus than to guess.
            yield self._emit(
                ev.ErrorEvent(
                    where=f"round {record.index} {pid}",
                    message=t(
                        "The turn was cut off by the output budget. The prose and code are "
                        "kept, but the stance card is not adopted — half a sentence is not "
                        "a position. Raise this participant's max_tokens."
                    ),
                )
            )

    def _visible_others(self, state: DeliberationState, record: RoundRecord, pid: str) -> list[str]:
        """The others whose turns this participant has **actually seen** by now.

        That covers every turn of the previous round, plus turns already completed in earlier phases
        of this round (which is how adversarial's attackers see the proposal).
        """
        seen: set[str] = set()
        if previous := state.previous():
            seen.update(previous.statements())
        seen.update(t.participant for t in record.turns if t.ok)
        return [other for other in state.others(pid) if other in seen]

    async def _collect_stance(
        self,
        pid: str,
        raw: str,
        state: DeliberationState,
        record: RoundRecord,
        others: list[str],
    ) -> AsyncIterator[ev.Event]:
        """Extract the stance card; on failure retry once, and record unknown if it still fails — no
        guessing, no writing on their behalf.
        """
        stance = parse_stance(raw, pid, record.index, others)
        degraded = False

        if stance is None:
            degraded = True
            try:
                retry = await self._call_plain(pid, prompts.stance_retry_prompt(others, raw), state)
                # T2 degradation: parse the line-by-line table first (a tiny output space, and a bad
                # line costs one cell), then fall back to trying JSON once — some models will take
                # it upon themselves to give JSON anyway.
                stance = parse_verdict_lines(retry, pid, record.index, others) or parse_stance(
                    retry, pid, record.index, others
                )
            except Exception:
                stance = None

        if stance is None:
            stance = Stance.as_unknown(pid, record.index, raw=raw[-2000:])

        record.stances[pid] = stance
        yield self._emit(
            ev.StanceEmit(
                round=record.index,
                participant=pid,
                stance={
                    "position": stance.position,
                    "confidence": stance.confidence,
                    "stance_on": {k: v.verdict for k, v in stance.stance_on.items()},
                    # The residuals are the whole substance of a partial. The vast majority of cells
                    # sit at partial for a long time, and category alone cannot detect movement at
                    # all — the movement is in the residuals appearing and disappearing.
                    "residuals": {
                        k: v.residuals for k, v in stance.stance_on.items() if v.residuals
                    },
                    "reasons": {k: v.reason for k, v in stance.stance_on.items() if v.reason},
                    # The verification records must reach the event stream. Without them, after a
                    # resume every agree has no foundation and is downgraded to not measured — **a
                    # resumed deliberation could never reach consensus**. This is the same illness
                    # as the truncation flag from this morning: a new rule written into memory, with
                    # no thought for how it comes back from the event stream.
                    "verified": {
                        k: [dataclasses.asdict(x) for x in v.verified]
                        for k, v in stance.stance_on.items()
                        if v.verified
                    },
                    # The premises must reach the event stream or they are lost on resume — and the
                    # premises are exactly what `resume --inject` exists to veto.
                    "premises": stance.premises,
                    "key_claims": stance.key_claims,
                    "changed": stance.changed_from_last_round,
                    "unknown": stance.unknown,
                },
                degraded=degraded,
            )
        )

    def _fresh_evidence(self, state: DeliberationState) -> list:
        """The evidence fed to the prompts — the same set the consensus assessment reads.

        The test lives in :func:`sesa.state.visible_evidence`. What is done here in addition is
        **marking staleness**: the mark lands on the records, the matrix reads the same records, and
        both rest on one fact.
        """
        from .state import visible_evidence

        current = {
            pid: self.workspace.revision_of(checkout) for pid, checkout in self._checkouts.items()
        }
        for record in state.rounds:
            for item in record.evidence:
                item.stale = item.is_stale(current.get(item.participant))
        return visible_evidence(state)

    async def _call_plain(self, pid: str, prompt: str, state: DeliberationState) -> str:
        """One auxiliary call that emits no events (stance retry, drafting)."""
        parts: list[str] = []
        async for chunk in self._adapters[pid].stream(
            prompt,
            system=prompts.system_prompt(state, pid),
            cwd=self._cwd_of(pid),
            timeout=self._turn_budget(pid),
        ):
            if isinstance(chunk, TextDelta):
                parts.append(chunk.text)
            elif isinstance(chunk, Done):
                self.budget.add(chunk.usage)
        return "".join(parts)

    # ------------------------------------------------------------------ # Wrapping up
    # ------------------------------------------------------------------ #

    async def _gather_evidence(self, record: RoundRecord) -> AsyncIterator[ev.Event]:
        """Run the verification command in each working copy; the results enter everyone's context in
        the next round.
        """
        if self.evidence is None or not self._checkouts:
            return

        if not self._shadow_checked:
            self._shadow_checked = True
            for event in self._warn_if_verify_reads_another_copy():
                yield event
        revisions = {
            pid: self.workspace.revision_of(checkout) for pid, checkout in self._checkouts.items()
        }
        for item in await asyncio.to_thread(self.evidence.self_test, self._checkouts, revisions):
            record.evidence.append(item)
            yield self._emit(
                ev.Evidence(
                    round=record.index,
                    participant=item.participant,
                    cmd=item.cmd,
                    exit_code=item.exit_code,
                    summary=item.summary[:400],
                )
            )

    def _detect_adoption(self, record: RoundRecord) -> list[ev.Event]:
        """Find "someone lifted a rival's previous round wholesale", and match it against the execution
        evidence.

        The risk in a debate is not failing to converge, it is **converging on the wrong side**.
        Measured, one participant swapped its own 34/34 implementation for a rival's 23/34 at
        similarity 0.97 — and what came out was a perfectly normal-looking consensus (see DESIGN.md
        14.18).

        It reads the working copy itself rather than the code blocks in the prose, so that an agent
        CLI (writing its own files, its code never entering the prose) can be measured too.
        """
        if not self._checkouts:
            return []

        current = {pid: adopt.snapshot(c.path) for pid, c in self._checkouts.items()}
        if self.recorder is not None:
            # Persist per round. Computing and discarding would make an agent CLI's intermediate
            # work unrecoverable — its code never enters the prose and the branch keeps only the
            # final state.
            for pid, files in current.items():
                self.recorder.save_snapshot(record.index, pid, files)
        previous, self._snapshots = self._snapshots, current
        exits = {item.participant: item.exit_code for item in record.evidence}
        if not previous:
            # Round 0 has no previous round to compare against, but its exit codes have to be
            # recorded first — otherwise round 1's copying can never obtain "what the evidence
            # looked like before the copy", evidence_before is always None, and it looks like "this
            # run had no execution evidence".
            self._last_exit.update(exits)
            return []

        events: list[ev.Event] = []
        for found in adopt.detect(previous, current, round_index=record.index):
            record.adoptions.append(found)
            events.append(
                self._remember_adoption(
                    ev.AdoptionEvent(
                        round=found.round,
                        participant=found.participant,
                        adopted_from=found.adopted_from,
                        path=found.path,
                        similarity_to_peer=found.similarity_to_peer,
                        similarity_to_own=found.similarity_to_own,
                        evidence_before=self._last_exit.get(found.participant),
                        evidence_after=exits.get(found.participant),
                    )
                )
            )
        self._last_exit.update(exits)
        return events

    def _remember_adoption(self, event: ev.AdoptionEvent) -> ev.Event:
        """A copying event goes both into the event stream and through to the outcome — it affects the
        deliverable and the exit code.
        """
        self._adoption_events.append(event)
        return self._emit(event)

    def _warn_if_verify_reads_another_copy(self) -> list[ev.Event]:
        """Whether the verification command imports a package of the same name from **outside** the
        working copy.

        It has really happened twice: once the repository had been ``pip install -e``-ed, running
        pytest inside a different working copy imported **the original repository's** code.
        Whatever the participant changed is invisible and the tests stay green forever — **the
        entire execution-evidence layer silently disabled**, with everybody "passing".

        It warns without blocking: the command is the user's and we are in no position to decide
        their intent for them; but it has to be said, or a whole deliberation's evidence is empty.
        """
        from .evidence.runner import shadowed_imports

        events: list[ev.Event] = []
        for pid, checkout in self._checkouts.items():
            checkable, shadowed = shadowed_imports(self.evidence.command, checkout.path)
            if not checkable or not shadowed:
                continue
            events.append(
                self._emit(
                    ev.ErrorEvent(
                        where=f"verify {pid}",
                        message=t(
                            "The verification command imports code from outside the "
                            "working copy ({paths}) — {pid}'s changes are never exercised "
                            "by it, so passing tests prove nothing. Prefix the command "
                            "with PYTHONPATH (e.g. `PYTHONPATH=src <your python> -m pytest "
                            "-q`), or use the working copy's own interpreter.",
                            paths=t("\uff1b").join(shadowed),
                            pid=pid,
                        ),
                    )
                )
            )
            break  # The participants' working copies are structurally identical, so
            # reporting once is enough — no need to flood
        return events

    async def _cross_test(self, state: DeliberationState) -> AsyncIterator[ev.Event]:
        """The final round's cross-test: A's tests against B's implementation.

        In the self-test phase everyone is usually green — when whoever writes the implementation
        also writes the tests, a green light says next to nothing. Only running them across tells
        "the implementation is right" from "the tests only cover their author's own case".
        """
        if self.evidence is None or not self._checkouts or not self.evidence.test_paths:
            return

        # A participant with no successful turn still has the original code in their working copy.
        # Counting them into the cross-test passes the baseline stub off as "their implementation"
        # and "their tests" — measured, a participant who never spoke successfully had its stub test
        # (assert True) hand somebody else the hardest conclusion available, "passes everyone's
        # tests".
        contributed = {
            turn.participant for record in state.rounds for turn in record.turns if turn.ok
        }
        active = {pid: c for pid, c in self._checkouts.items() if pid in contributed}
        if skipped := sorted(set(self._checkouts) - contributed):
            yield self._emit(
                ev.ErrorEvent(
                    where="cross_test",
                    message=t(
                        "{ids} produced no successful turn, so their working copies still "
                        "hold the original code; they are left out of the cross-test — "
                        "otherwise the baseline stub would be passed off as their "
                        "implementation and their tests.",
                        ids=", ".join(skipped),
                    ),
                )
            )
        if len(active) < 2:
            yield self._emit(
                ev.ErrorEvent(
                    where="cross_test",
                    message=t("Fewer than 2 usable participants; the cross-test cannot run."),
                )
            )
            return

        revisions = {pid: self.workspace.revision_of(c) for pid, c in active.items()}
        matrix, records = await asyncio.to_thread(
            self.evidence.cross_test, active, revisions, self.evidence.test_paths
        )
        self._cross_matrix = matrix
        # The self-test results are supplied here: cross_test runs only off the diagonal, and the
        # judgement "their tests pass only for themselves" needs the diagonal. Without it that
        # signal can never be true.
        self._self_tests = {
            (item.participant, item.participant): item.exit_code
            for record in state.rounds
            for item in record.evidence
            # Use the against field to identify a self-test rather than looking for "×" in the
            # user's command string — the command is written by the user, and that character turning
            # up in one by coincidence is only a matter of time.
            if item.is_self_test
        }
        if state.current is not None:
            state.current.evidence.extend(records)
        for item in records:
            yield self._emit(
                ev.Evidence(
                    round=state.round_index - 1,
                    participant=item.participant,
                    cmd=item.cmd,
                    exit_code=item.exit_code,
                    summary=item.summary[:400],
                )
            )

    def _applies_code(self, pid: str) -> bool:
        """Whether the engine needs to write files on this participant's behalf.

        **Off by default**: an agent CLI writes its own files, and also writing out the code blocks
        from its narration would overwrite what it has just written. Only a participant that cannot
        write files turns this on explicitly.
        """
        spec = next((p for p in self.participants if p.id == pid), None)
        return bool(spec and spec.options.get("apply_code_blocks"))

    def _cwd_of(self, pid: str) -> Path | None:
        """This participant's working directory. In a code task, their own worktree."""
        checkout = self._checkouts.get(pid)
        return checkout.path if checkout else None

    def _turn_budget(self, pid: str | None = None) -> float:
        """The timeout cap for this call.

        The smaller of two caps:

        * **What the participant declared**, ``timeout`` (or the configured ``turn_timeout`` when
          unset) — an agent CLI with tools may need minutes for one round, and must be able to ask
          for more time for itself. This value used to be silently overridden by the engine's
          default, which measurably killed one experiment.
        * **The remaining wall-clock budget** — the budget used to be checked only at round
          boundaries, while one round alone can run for minutes; measured, a 900s cap was overrun
          to 1185s.
        """
        declared = self.turn_timeout
        if pid is not None:
            spec = next((p for p in self.participants if p.id == pid), None)
            if spec and spec.options.get("timeout"):
                declared = float(spec.options["timeout"])

        limit = self.budget.max_wall_seconds
        if limit is None:
            return declared
        return min(declared, max(1.0, limit - self.budget.elapsed))

    #: A round needs at least this many seconds to be worth starting. Below it, every call is
    #: squeezed to `max(1.0, remaining)` and is bound to produce 0 characters — burning time and
    #: burying the results already in hand.
    MIN_ROUND_SECONDS = 30.0

    def _insufficient_budget(self) -> str | None:
        """Whether the remaining wall clock supports another round; if not, return a sentence for the
        user.
        """
        limit = self.budget.max_wall_seconds
        if limit is None:
            return None
        left = limit - self.budget.elapsed
        if left >= self.MIN_ROUND_SECONDS:
            return None
        return t(
            "Only {left:.0f}s of wall clock remain (limit {cap:.0f}s), not enough for "
            "another round, so the run wrapped up on the rounds already completed. "
            "**This is not a failure** — the earlier rounds' results are all there. "
            "Raise `budget.max_wall_seconds` to debate for longer.",
            left=max(0.0, left),
            cap=limit,
        )

    def _budget_capped(self, pid: str) -> bool:
        """Whether this call's cap was squeezed down by **the budget** rather than declared by the
        participant.

        The two errors have to differ: cut off by the budget means "the run's time is up", and
        reporting it as "this participant hung" sends people hunting a problem that does not exist.
        """
        limit = self.budget.max_wall_seconds
        if limit is None:
            return False
        spec = next((p for p in self.participants if p.id == pid), None)
        declared = float((spec.options.get("timeout") if spec else None) or self.turn_timeout)
        return (limit - self.budget.elapsed) < declared

    def _pick_rapporteur(self, state: DeliberationState) -> str:
        if self.rapporteur != "rotate" and self.rapporteur in state.ids:
            return self.rapporteur
        return state.rotate()

    async def _finalize(
        self, state: DeliberationState, outcome: Outcome, run_id: str
    ) -> AsyncIterator[ev.Event]:
        report = state.current.consensus
        drafter = self._pick_rapporteur(state)

        # **This round only**: counting every round would leave produced True when round 0 produced
        # normally and a later round collapsed entirely into EXHAUSTED, so that total failure would
        # be buried under NOT_MEASURED — exactly what this test exists to prevent.
        produced = bool(state.rounds) and any(t.ok for t in state.rounds[-1].turns)
        if not self.protocol.measures_consensus and produced:
            # **This has to be settled before the drafting prompt is generated.** This protocol
            # produces no peer assessment at all (reflect has nobody seeing anybody), so every cell
            # is necessarily unknown; carrying on with default-deny would report "the rounds ran out
            # with disagreements still open" — labelling missing data as disagreement, exactly what
            # this project's second bottom line exists to prevent. This once sat 70 lines after the
            # drafting, so the rapporteur wrote the draft as though the discussion were "unfinished"
            # while the banner said "this protocol does not measure consensus", and the two did not
            # match.
            # The `produced` premise matters just as much: with everyone crashing and nothing
            # produced, the reason nothing was measured is **that they all failed**, not that "this
            # protocol does not measure consensus". Calling that not_measured too covers a total
            # failure with a respectable label.
            outcome = Outcome.NOT_MEASURED

        draft = None
        try:
            raw = await self._call_plain(drafter, rap.build_prompt(state, report, outcome), state)
            with scoped(prompts.pick_language(state.task)):
                # `parse_draft` fills in a placeholder title when the rapporteur omits the topic,
                # and that line goes straight into RESULT.md — so it has to follow the deliberation
                # language.
                draft = rap.parse_draft(raw, state.ids)
        except Exception as exc:
            yield self._emit(ev.ErrorEvent(where=f"rapporteur:{drafter}", message=str(exc)))

        if draft is None:
            draft = rap.fallback_draft(state, report)
            drafter = None  # labelled honestly in the report as a mechanical summary, not
            # written by any one person

        # The rapporteur may omit a disagreement — check against the disagreement matrix, the ground
        # truth
        draft = rap.reconcile(draft, state, report)
        if note := draft.get("reconciled"):
            yield self._emit(
                ev.WriterMismatch(
                    round=state.current.index,
                    writer=drafter or "fallback",
                    kind="omitted_disagreements",
                    detail=note,
                )
            )

        # False consensus: the stance cards claim agreement while the prose substantively conflicts
        # — recorded honestly, not papered over. Both directions have to be checked: the conflicts
        # the rapporteur reported explicitly, and the disagreements they listed after reading the
        # whole thing — the latter matters more, being the real conflict the stance cards failed to
        # reflect and the rapporteur caught.
        if outcome in (
            Outcome.CONSENSUS,
            Outcome.CONSENSUS_WITH_RESERVATIONS,
            Outcome.PARTIAL_COVERAGE_CONSENSUS,
        ):
            # A conflict the rapporteur reported explicitly counts against any grade of consensus
            conflicts = list(draft.get("conflicts_found") or [])
            # But "the rapporteur listed open disagreements" counts as false consensus only when
            # **full agreement is claimed**: consensus_with_reservations is by definition "there are
            # reservations", and the rapporteur writing those reservations up as open disagreements
            # is expected behaviour, not a contradiction. All three measured runs were falsely
            # reported as false consensus, precisely because this test was too broad.
            if outcome is Outcome.CONSENSUS:
                conflicts += [
                    t(
                        "the rapporteur listed an open disagreement \u300c{topic}\u300d, "
                        "but the matrix shows the parties in full agreement",
                        topic=d.topic,
                    )
                    for d in draft["disagreements"]
                ]
            if conflicts:
                yield self._emit(
                    ev.WriterMismatch(
                        round=state.current.index,
                        writer=drafter or "fallback",
                        kind="claimed_disagreements",
                        detail="；".join(conflicts)[:500],
                    )
                )
                yield self._emit(
                    ev.FalseConsensus(
                        round=state.current.index,
                        detected_by=drafter or "fallback",
                        conflicts=conflicts,
                    )
                )
                outcome = Outcome.FALSE_CONSENSUS

        adoptions = [
            (
                event.round,
                event.participant,
                event.adopted_from,
                event.path,
                event.similarity_to_peer,
                event.similarity_to_own,
                event.evidence_regressed,
            )
            for event in self._adoption_events
        ]
        if outcome is Outcome.CONSENSUS and any(a[6] for a in adoptions):
            # Consensus was reached, but it rests on a regression: someone threw their own
            # implementation away for a rival's, and their self-tests went from passing to failing
            # as a result. It is still consensus — the parties really did agree — but delivering it
            # as full consensus would be papering over. **No such downgrade without execution
            # evidence**: similarity does not answer good or bad.
            outcome = Outcome.CONSENSUS_WITH_RESERVATIONS

        result = Result(
            run_id=run_id,
            task=state.task,
            outcome=outcome,
            conclusion=draft["conclusion"],
            drafted_by=drafter,
            grounds=draft["grounds"],
            disagreements=draft["disagreements"],
            minority=draft["minority"],
            cross_test=self._cross_matrix.render() if self._cross_matrix else "",
            branches={pid: c.branch for pid, c in self._checkouts.items() if c.branch},
            suspicious_testers=(
                self._cross_matrix.suspicious_testers(self._self_tests)
                if self._cross_matrix
                else []
            ),
            universally_passing=(
                self._cross_matrix.universally_passing() if self._cross_matrix else []
            ),
            residuals=report.residuals,
            adoptions=adoptions,
            truncated_turns={
                pid: count
                for pid in state.ids
                if (
                    count := sum(
                        1
                        for record in state.rounds
                        for turn in record.turns
                        if turn.participant == pid and turn.truncated
                    )
                )
            },
            briefings=dict(self._briefings),
            # No consensus report ⇒ not one cell measured ⇒ coverage 0. This used to say `else 1.0`,
            # contradicting the comment right next to it: the comment said "1.0 is inverted" while
            # the code explicitly filled in 1.0 when the report was missing — the one path that most
            # needed to say "not measured" was the one claiming everything was.
            coverage=report.coverage if report else 0.0,
            unmeasured_cells=list(report.unmeasured_cells) if report else [],
            unverified_agreements=list(report.unverified_agreements) if report else [],
            unverifiable_agreements=list(report.unverifiable_agreements) if report else [],
            # The foundation of a consensus has to be delivered with the consensus. It is all
            # self-reported, and the reader has a right to see the original text.
            verification_grounds={
                f"{source} → {target}": [
                    f"{v.how}/{v.result}｜{v.of}｜{v.detail}".rstrip("｜") for v in on.verified
                ]
                for record in state.rounds
                for source, stance in record.stances.items()
                if not stance.unknown
                for target, on in stance.stance_on.items()
                if on.verified
            },
            premises={
                pid: stance.premises
                for record in state.rounds
                for pid, stance in record.stances.items()
                if stance.premises and not stance.unknown
            },
            rounds_used=len(state.rounds),
            usage=state.total_usage(),
        )

        result_path = None
        if self.recorder:
            result_path = str(self.recorder.save_result(result, render_result(result)))
            self.recorder.save_report(render_report(state, result, self.budget.caveat()))

        yield self._emit(
            ev.VerdictFinal(
                outcome=outcome.value,
                run_id=run_id,
                drafted_by=drafter,
                rounds_used=result.rounds_used,
                unresolved=report.unresolved,
                result_path=result_path,
            )
        )

    # ------------------------------------------------------------------ #

    def _commit_branches(self, run_id: str) -> None:
        """Turn everyone's changes into commits, keeping the branches for people to diff.

        **Nobody's branch is deleted** — an implementation that was not adopted carries the
        minority opinion.
        """
        commit = getattr(self.workspace, "commit_all", None)
        if commit is None:
            return
        for pid, checkout in self._checkouts.items():
            commit(checkout, t("sesa {run}: {pid}'s implementation", run=run_id, pid=pid))

    def _emit(self, event: ev.Event) -> ev.Event:
        if self.recorder:
            self.recorder.emit(event)
        return event


async def _merge(factories: list[Callable[[], AsyncIterator[ev.Event]]]) -> AsyncIterator[ev.Event]:
    """Merge several concurrent async event streams into one, live."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump(factory) -> None:
        try:
            async for event in factory():
                await queue.put(event)
        finally:
            await queue.put(_SENTINEL)

    tasks = [asyncio.create_task(pump(f)) for f in factories]
    finished = 0
    try:
        while finished < len(tasks):
            item = await queue.get()
            if item is _SENTINEL:
                finished += 1
            else:
                yield item
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # **One branch blowing up must not have the whole run pretend nothing happened.**
    # `pump`'s finally guarantees the sentinel is enqueued, so the count still completes and the
    # loop still exits normally, while the exception is swallowed by `return_exceptions=True`.
    # `_run_move`'s inner try/except cannot cover what sits outside it (`_emit`, `recorder.emit`,
    # `record.turns.append`, `patch.apply_files`, …) — and if any of those raises, that
    # participant's turn vanishes into thin air, while the outcome report shows only that they
    # "produced nothing", looking exactly like being killed by a timeout.
    for task in tasks:
        if task.cancelled():
            continue
        if (error := task.exception()) is not None:
            raise error
