"""Verification of the README's four bottom lines (round three).

**I did not write this file.** It comes from the third Sesa deliberation. 9 assertions failed
against the code at the time, and verified mechanically one by one, **all held**.

The sharpest of this round is a monotonicity inversion: both sides **agreeing without
reservation** is judged `exhausted`, while both giving only **a partial with residuals** is
judged `consensus_with_reservations` — a weaker agreement buying a better outcome. The
confidence bar hangs only on the strongest grade, and every downgrade path walks past it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from sesa.consensus import rapporteur as rap
from sesa.consensus.matrix import StanceMatrix
from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from sesa.record import Recorder, new_run_id
from sesa.types import Outcome, Stance, StanceOn

sys.path.insert(0, str(Path(__file__).parent))
from test_bottom_lines import _state, participant
from test_tty import _project, run_in_tty

#: needed only by the async cases; the synchronous cases in the same file should not carry this
#: marker
_aio = pytest.mark.asyncio


async def _run(tmp_path, specs, protocol="debate", **kw):
    run_id = new_run_id()
    engine = Engine(
        specs,
        build_protocol(protocol),
        matrix=StanceMatrix(stability_window=kw.pop("stability_window", 2)),
        recorder=Recorder(tmp_path, run_id),
        **kw,
    )
    events = [e async for e in engine.run("该用 Postgres 还是 SQLite？")]
    verdict = next(e for e in events if e.t == "verdict.final")
    root = tmp_path / "runs" / run_id
    return verdict, (root / "RESULT.md").read_text("utf-8"), (root / "REPORT.md").read_text("utf-8")


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


# ═══════════════════════════════════════════════════════════════════════════ # Group A: bottom line
# 2 — "someone objected" and "the engine did not measure it" are accounted separately
# ═══════════════════════════════════════════════════════════════════════════ #


@_aio
async def test_report_md_must_not_count_unmeasured_cells_as_disagreements(tmp_path):
    """report.py:314 ``f"{report.unresolved} open disagreements"``.

    ``ConsensusReport.unresolved`` is ``opposed + unmeasured`` (types.py:267) — precisely the two
    items bottom line 2 requires to be **accounted separately**. Adding them together and calling
    the sum "disagreements" is labelling missing data as disagreement.

    This is not an edge case: **every debate's round 0 hits it**. In round 0 nobody has read
    anybody, ``prompts.stance_instruction`` offers "(none)" as the set of people to take a
    position on (engine.py:301-305's comment says this is deliberate), and the matrix is
    necessarily all "unknown". So REPORT.md holds two adjacent lines that contradict each other:

        2 open disagreements · lowest confidence 0.90
        Why it did not converge:
        - 2 cells not measured (a → b; b → a) — the other's turn was not read, or the stance card
          could not be parsed

    The first line calls it disagreement, the second calls it missing measurement; the second was
    fixed in round two and the first was not.
    """
    _, _, report_md = await _run(tmp_path, [participant("a"), participant("b")], max_rounds=2)

    block = report_md[report_md.index("### 第 0 轮") : report_md.index("### 第 1 轮")]
    assert "2 处未测到" in block, (
        f"the premise does not hold (round 0 is not entirely unmeasured):\n{block}"
    )

    match = re.search(r"未决分歧 (\d+) 项", block)
    assert not (match and int(match.group(1)) > 0), (
        "round 0 has zero opposition and two unmeasured cells, and REPORT.md says "
        f"'{match.group(1) if match else '?'} open disagreements'; "
        "the blockers below the same paragraph already call them 'not measured'.\n" + block
    )


def test_terminal_output_must_not_count_unmeasured_cells_as_disagreements(tmp_path):
    """cli.py:353 ``f"{event.unresolved} open disagreements …"``.

    Round two added an ``if not protocol.measures_consensus`` branch here (cli.py:345-349), which
    stops reflect only. debate **does** measure consensus, so it takes the else and prints missing
    measurement as disagreement all the same — and this is on the first screen a new user sees
    from `sesa run`.

    The root cause is at the event layer: ``ConsensusUpdate`` carries only a merged ``unresolved``
    (events.py:152), and the separated ``opposed`` / ``unmeasured`` never cross the event
    boundary. No consumer of the event stream (the CLI, `sesa eval`'s "unresolved" column at
    cli.py:700) **can** account for them separately, however much it wants to. Fixing the print
    statement only has it emerge from the next outlet.
    """
    _, out = run_in_tty(
        [sys.executable, "-m", "sesa.cli", "run", "议题"],
        _project(tmp_path, "debate"),
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)

    # the terminal's progress display follows **the interface language** (English by default); only
    # REPORT.md follows the topic.
    assert "unknown" in plain, (
        f"the premise does not hold (round 0's matrix is not entirely unknown):\n{plain[-1200:]}"
    )
    counts = [
        int(n)
        for n in re.findall(r"未决分歧 (\d+) 项", plain)
        + re.findall(r"(\d+) unresolved disagreements", plain)
    ]
    assert not any(counts), (
        f"the terminal printed missing measurement as open disagreement: {counts}; nobody objected in this run, "
        "and round 0's two cells are only 'they have not read the other's turn yet'.\n"
        + plain[-1200:]
    )


def test_rapporteur_is_not_told_there_was_no_opposition_when_nothing_was_measured():
    """rapporteur.py:112 ``consensus_block = … if lines else "(the stance cards show no explicit
    opposition)"``.

    Round two added a ``stances_requested`` guard to "stance could not be parsed: …" on the
    adjacent lines 110-111, with the reason in the comment: "a protocol that never requests a
    stance card at all, listing its participants as stance-could-not-be-parsed, accuses them of
    not answering a question nobody put to them".

    **The else branch of the same function was not fixed with it.** reflect never asked for a
    single stance card, and the rapporteur is told "the stance cards show no explicit opposition"
    — asserting something never measured to the one model that decides RESULT.md's conclusion.
    And ``OUTCOME_NOTES`` (rapporteur.py:76-90) has no ``NOT_MEASURED`` entry, so the
    ``outcome_note`` ``build_prompt`` appends is an empty string — nothing corrects it.

    Compare report.py:30's ``assert set(OUTCOME_BANNER) == set(Outcome)``: the banner has an
    exhaustiveness check and the drafting prompt does not.
    """
    state = _state(["a", "b"], {})
    state.stances_requested = False
    report = StanceMatrix().assess(state)

    # the task is "t" (English), so the rapporteur prompt is English — the deliberation language
    # follows the task.
    prompt = rap.build_prompt(state, report, Outcome.NOT_MEASURED)
    block = prompt[prompt.index("# Where the disagreements stand") : prompt.index("# What to do")]

    assert "no explicit opposition" not in block, (
        "this protocol never requested a stance card, and the rapporteur prompt says 'the stance cards show no explicit opposition':\n"
        + block
    )


# ═══════════════════════════════════════════════════════════════════════════ # Group B: bottom
# lines 3/4 — the banner and the prose must say the same thing
# ═══════════════════════════════════════════════════════════════════════════ #


@_aio
async def test_reserved_consensus_result_does_not_claim_full_agreement(tmp_path):
    """report.py:136-137 folds ``CONSENSUS_WITH_RESERVATIONS`` into the same grade as full consensus.

    So within one RESULT.md:

        🟡 **Consensus with reservations** — nobody explicitly objected, but the reservations
        below are unresolved
        ## Open disagreements
        None. The parties agree on every substantive question.
        ## Reservations
        **a → b** - the specific point not yet accepted

    "The parties agree on every substantive question" conflicts directly with the reservations
    listed below it. Round two fixed exactly this shape of contradiction for
    ``partial_coverage_consensus`` (report.py:128-135's comment: "the banner says consensus with
    partial coverage, and if the prose still said no consensus was reached, one document would
    contradict itself"), and the next branch of the same ``elif`` chain was left untouched.
    """
    verdict, result_md, _ = await _run(
        tmp_path,
        [participant("a", FAKE_VERDICT="partial"), participant("b", FAKE_VERDICT="partial")],
        max_rounds=2,
    )
    assert verdict.outcome == Outcome.CONSENSUS_WITH_RESERVATIONS.value, verdict.outcome
    assert "## 保留意见" in result_md, (
        "the premise does not hold: this run has no residuals on record"
    )

    assert "各方在所有实质问题上意见一致" not in result_md, (
        "the banner says 'reservations unresolved' and the residuals are listed below one by one, while the prose says 'the parties agree on every substantive question'"
    )


# ═══════════════════════════════════════════════════════════════════════════ # Bottom line 3: a
# weaker agreement must not buy a better outcome
# ═══════════════════════════════════════════════════════════════════════════ #


def test_a_weaker_agreement_must_not_yield_a_better_outcome():
    """matrix.py:229-241's downgrade chain **never looks at** ``min_confidence``.

    The confidence bar takes effect only at ``converged`` (matrix.py:116-119 → 132), which is to
    say it stops only the strongest grade. The moment there is any missing measurement or
    reservation, control falls into 229-241, which looks only at coverage / agreed / reservations
    and skips confidence entirely.

    The consequence is non-monotonic:

    * both **agree without reservation**, confidence 0.10 → ``exhausted``
      "⏳ the discussion is unfinished — the rounds or the budget ran out"
    * both give only **partial + residuals**, confidence 0.10 → ``consensus_with_reservations``
      "🟡 consensus with reservations"

    The second is weaker on every dimension (zero explicit agrees, two residuals on record) and
    gets the better banner. And the first gives the user the wrong remedy: the banner says there
    were not enough rounds, while the blockers say plainly "lowest confidence 0.10 is below the
    threshold 0.60" — and no number of extra rounds raises a confidence.
    """
    matrix = StanceMatrix()
    rank = {  # strongest to weakest, the README's "six grades of outcome" ordering
        Outcome.CONSENSUS: 0,
        Outcome.CONSENSUS_WITH_RESERVATIONS: 1,
        Outcome.PARTIAL_COVERAGE_CONSENSUS: 2,
        Outcome.DEADLOCK: 3,
        Outcome.EXHAUSTED: 3,
    }

    def outcome_for(verdict, residuals):
        state = _state(
            ["a", "b"],
            {
                "a": _stance("a", ["b"], verdict, 0.10, residuals),
                "b": _stance("b", ["a"], verdict, 0.10, residuals),
            },
        )
        return matrix.decide_outcome(matrix.assess(state), rounds_left=0)

    unconditional = outcome_for("agree", None)
    reserved = outcome_for("partial", ["尚未接受的具体点"])

    assert rank[unconditional] <= rank[reserved], (
        f"both agreeing without reservation is judged {unconditional}, while both giving only a partial with residuals is judged "
        f"{reserved} — a weaker agreement buying a better outcome. "
        "The confidence bar takes effect only at the strongest grade and every downgrade path walks past it."
    )


# ═══════════════════════════════════════════════════════════════════════════ # Group A continued:
# the fourth and fifth outlets
# ═══════════════════════════════════════════════════════════════════════════ #


@_aio
async def test_a_non_measuring_protocol_reports_no_missing_data(tmp_path):
    """report.py:239 ``if result.coverage < 1.0 or result.unmeasured_cells:``.

    This section has no ``stances_requested`` guard. So in reflect's RESULT.md the banner and the
    "open disagreements" section (fixed in round two) say:

        ⚪ This protocol **does not measure consensus** … nobody sees anybody, so **there is no
        peer assessment to speak of**

    and a few lines down the same document says:

        ## What was not measured
        **0% of cells were measured.** What was not measured is not disagreement, it is
        **missing data**
        - a → b
        - b → a

    "No peer assessment to speak of" and "these two cells are missing data" cannot both be true:
    missing data means a reading should have existed and was not obtained, while under reflect
    those two cells do not exist at all.
    This is the fifth outlet of round two's error — the first four were all fixed on the
    ``stances_requested`` test, and this one missed the same guard.
    """
    verdict, result_md, _ = await _run(
        tmp_path, [participant("a"), participant("b")], protocol="reflect", max_rounds=2
    )
    assert verdict.outcome == Outcome.NOT_MEASURED.value, verdict.outcome

    assert "## 未测到的部分" not in result_md, (
        "this protocol structurally produces no peer assessment, and RESULT.md lists cells that do not exist as 'missing data':\n"
        + result_md[result_md.index("## What was not measured") :][:300]
    )


@_aio
async def test_a_failed_run_still_tells_the_reader_what_to_do_next(tmp_path):
    """Bottom line 4, "an open disagreement must come with a way out" — **the weakest item in this
    group, stated plainly as such.**

    Round two fixed "disagreements the engine backfilled come with no way out" (report.py:163-171)
    by appending ``sesa resume --inject`` to each disagreement. But that else branch hangs inside
    ``for d in result.disagreements``: **with an empty list of disagreements the loop never runs**.

    And an empty list is exactly the occasion that most needs a way out. When neither side's
    stance card parses:

        outcome exhausted; "no specific disagreements could be listed, but this deliberation did
        not reach consensus"; "0% of cells measured"; ``sesa resume`` appears **0** times in the
        whole document.

    The reader gets a document saying "not settled, and nothing was measured", and zero next
    steps.
    (The only other place that renders a resume hint is the "premises this conclusion depends on"
    section, and it requires at least one stance card with ``premises`` — which is also empty when
    no stance card parsed.)

    I mark this the weakest: it is "something that should be there is missing" rather than
    "something wrong was written", and it depends more on how bottom line 4 is read than groups A
    and B do.
    """
    verdict, result_md, _ = await _run(
        tmp_path,
        [participant("a", FAKE_MODE="no_stance"), participant("b", FAKE_MODE="no_stance")],
        max_rounds=2,
    )
    assert verdict.outcome not in (
        Outcome.CONSENSUS.value,
        Outcome.CONSENSUS_WITH_RESERVATIONS.value,
    ), verdict.outcome

    assert "sesa resume" in result_md, (
        f"outcome {verdict.outcome}, coverage 0%, and RESULT.md offers no next step at all:\n{result_md}"
    )


# ═══════════════════════════════════════════════════════════════════════════ # Group C: one 64 KiB
# line limit, scattered across three outlets
# ``asyncio.StreamReader``'s default line limit is 64 KiB. ``adapters/cli.py`` iterates the
# subprocess pipes by line in two places — stderr's ``drain_stderr`` (line 190) and stdout's
# ``parse="jsonl"`` (line 222) — neither raising the limit nor handling an overrun. The same
# assumption ("an agent CLI will not output a line over 64 KiB") is wrong at three outlets, and the
# three fail in different shapes, so fixing any one of them plugs neither of the others.
# ═══════════════════════════════════════════════════════════════════════════ #

import asyncio  # noqa: E402

from sesa.adapters.base import AdapterError  # noqa: E402
from sesa.adapters.cli import CliAdapter  # noqa: E402
from sesa.types import ParticipantSpec  # noqa: E402


def _cli(script: str, **opts) -> CliAdapter:
    return CliAdapter(
        ParticipantSpec(
            id="p",
            adapter="cli",
            options={"command": [sys.executable, "-c", script], "prompt": "stdin", **opts},
        )
    )


async def _collect(adapter) -> tuple[str, Exception | None]:
    parts: list[str] = []
    try:
        async for chunk in adapter.stream("hi", timeout=30):
            if text := getattr(chunk, "text", None):
                parts.append(text)
    except Exception as exc:
        return "".join(parts), exc
    return "".join(parts), None


#: 80 KiB of stderr on one line. 80000 > 65536, and **no newline in it**.
_ONE_LONG_LINE = "import sys;sys.stderr.write('A'*80000);sys.stderr.flush();sys.exit({code})"


def test_a_long_single_line_answer_on_stderr_is_not_silently_dropped():
    """cli.py:190 + 268 — a ``stderr_is_output`` turn is swallowed whole, with exit code 0.

    ``stderr_is_output: true`` is the channel for "a CLI that writes its answer to stderr"
    (cli.py:81-82's comment). On that path, ``drain_stderr``'s ``async for raw in proc.stderr``
    raises ``ValueError`` when one line exceeds ``StreamReader``'s default 64 KiB and the task dies
    on the spot; and that task is **never awaited**, only ``cancel()``-ed in ``finally``
    (cli.py:250), so the exception is never received.

    The result is the worst kind: exit code 0, ``stream()`` raising nothing, and
    ``Done(Usage.unknown())`` produced as usual — the engine receives a **"successful empty
    turn"**. Measured (reproduced three times out of three):

        stderr 60000 bytes (one line) → turn received, 60001 characters
        stderr 80000 bytes (one line) → turn received, 0 characters, exception None
        stderr 80000 bytes (with newlines) → turn received, 80000 characters

    The damage to bottom line 2 is direct: this participant **spoke**, the engine recorded silence,
    and the matrix then counted that cell as "not measured". The "the engine did not measure it"
    grade exists to admit a gap honestly, and here it is used to cover data the engine itself lost
    — with no output anywhere mentioning where those 80 KB went.
    """
    text, exc = asyncio.run(_collect(_cli(_ONE_LONG_LINE.format(code=0), stderr_is_output=True)))

    assert exc is None, (
        f"the premise does not hold: the process exited 0 and should not raise, and it raised {exc!r}"
    )
    assert len(text) > 0, (
        "a participant wrote 80000 bytes of turn to stderr and the engine received 0 characters, "
        "with exit code 0, no exception and no warning — the whole turn was silently discarded "
        "and will then be recorded as a 'successful empty turn'."
    )


def test_a_failing_process_does_not_claim_both_streams_were_empty():
    r"""cli.py:146 ``return "\n".join(parts) or "(both stdout and stderr were empty)"``.

    ``_why``'s docstring (cli.py:134-139) states the reason this function exists: "measured, 8
    deliberations were wasted for this reason, and all the event stream held was 'exit code 1
    (stderr empty)', with no sign that it was a quota problem". But it depends on ``stderr_buf``,
    and ``drain_stderr`` has already died on a line over 64 KiB, leaving ``stderr_buf`` an empty
    list.

    So the error message is not truncated, it is **a lie**: the process wrote 80 KB to stderr and
    Sesa tells the operator "both stdout and stderr were empty". Measured (reproduced 20 times out
    of 20):

        p: exit code 7
        (both stdout and stderr were empty)

    The boundary was measured to be exactly 64 KiB: 65000 bytes on one line comes through, 65600
    does not; the same 80 KB **with newlines** comes through in full.
    """
    _, exc = asyncio.run(_collect(_cli(_ONE_LONG_LINE.format(code=7))))

    assert isinstance(exc, AdapterError), (
        f"the premise does not hold: exit code 7 should raise AdapterError, and we got {exc!r}"
    )
    assert "both stdout and stderr were empty" not in str(exc), (
        f"the subprocess wrote 80000 bytes to stderr and Sesa reports 'both stdout and stderr were empty':\n{exc}"
    )


def test_a_long_jsonl_line_does_not_kill_the_turn():
    r"""cli.py:222 ``async for raw in proc.stdout`` — the same limit for ``parse="jsonl"``.

    ``parse: jsonl`` is not an edge configuration; it is DESIGN.md §3.2's sample configuration for
    ``codex exec --json``. One JSON line holding a longish turn crosses 64 KiB easily (Chinese
    escaped as ``\uXXXX`` costs 6 bytes a character, so about 11,000 characters hits the cap).
    Measured:

        one JSON line holding 60000 characters → OK
        one JSON line holding 70000 characters → ValueError: Separator is found, but chunk is
            longer than limit
        one JSON line holding 200000 characters → ValueError: Separator is not found, and chunk
            exceed the limit

    I mark this **the lightest of the three**, and say why: the engine records it as a failed turn
    carrying an error (engine.py:323-327), so the accounting is not wrong and bottom line 2 is not
    violated. It is a pure robustness defect — the more talkative the speaker the likelier it
    drops out — and it raises a bare ``ValueError`` rather than an ``AdapterError``, with not even
    the participant id in the message.

    The root cause of all three is the same: ``asyncio.create_subprocess_exec`` was not passed
    ``limit=``.
    """
    script = (
        "import sys,json;"
        "sys.stdout.write(json.dumps({'text':'X'*70000})+chr(10));"
        "sys.stdout.flush();sys.exit(0)"
    )
    text, exc = asyncio.run(_collect(_cli(script, parse="jsonl", extract="text")))

    assert exc is None, (
        f"one 70000-character JSONL turn killed the whole turn: {type(exc).__name__}: {exc}"
    )
    assert len(text) == 70000, (
        f"the turn was truncated: expected 70000 characters, received {len(text)}"
    )


# ═══════════════════════════════════════════════════════════════════════════ # Group D: the
# proposal produced nothing and the attack/response ran anyway
# This one was raised by deepseek in this deliberation (adversarial.py:49 ``_proposal_of``). I had
# not examined this path; verifying it confirmed it, so it is written up as a case.
# ═══════════════════════════════════════════════════════════════════════════ #

from sesa.protocols.adversarial import AdversarialProtocol  # noqa: E402
from sesa.state import DeliberationState, RoundRecord, Turn  # noqa: E402


def test_a_crashed_proposal_is_not_replaced_by_the_task_statement():
    """adversarial.py:49-56 ``_proposal_of`` silently ``return task`` when the proposal is missing.

    After proposer=a's proposal phase crashes (``turn.ok`` false), the prompts ``plan()`` renders
    were measured to be:

        --- attacks in parallel / b / attack
        # Under review
        Postgres or SQLite?
        # What to do
        You are the dedicated attacker. Find the **concrete defects** in the proposal above.

        --- response: a / a / rebut
        # Your proposal
        Postgres or SQLite?
        …
        - Concede the ones that hold, and say how you intend to fix the proposal

    Both send **the task text** out as "the proposal": the attackers are asked to point at "a
    specific place or specific claim" in a proposal that does not exist, and the proposer is asked
    to "fix" a proposal they never wrote. And ATTACK/REBUT say it plainly — "an attack you leave
    unanswered goes straight into the 'open items' of the final report" — so attacks on a
    hallucinated proposal travel all the way into the deliverable.

    Downstream this is **entirely indistinguishable** from ``proposer: input`` mode (where the
    proposal really is the task input): REPORT.md gives no sign that "there was no proposal this
    round". It belongs to the same family as bottom line 2's error — treating "not obtained" as
    "obtained" — except that this time the fill-in value is not 0 but the task text.
    """
    state = DeliberationState(
        task="该用 Postgres 还是 SQLite？",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ("a", "b", "c")],
        max_rounds=3,
    )
    record = RoundRecord(index=0)
    state.rounds.append(record)
    record.turns.append(
        Turn(participant="a", round=0, phase=0, kind="draft", text="", raw="", error="exit code 1")
    )

    phases = AdversarialProtocol(proposer="a").plan(state)
    rendered = {
        (phase.label, move.participant): (
            move.prompt(state) if callable(move.prompt) else move.prompt
        )
        for phase in phases
        for move in phase.moves
    }

    attack = rendered[("Attacks in parallel", "b")]
    rebut = rendered[("Response: a", "a")]

    assert state.task not in attack.split("# 你要做的")[0], (
        "the proposal phase crashed and the attackers are still told the task text is the 'thing under review':\n"
        + attack[:200]
    )
    assert state.task not in rebut.split("# 各方提出的攻击")[0], (
        "the proposal phase crashed and the proposer is still told the task text is 'your proposal':\n"
        + rebut[:200]
    )
