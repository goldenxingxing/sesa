"""Verification of the README's four bottom lines at the delivery layer (round two).

**I did not write this file.** It comes from the second Sesa deliberation — the participant
claude read the source in its own git worktree, wrote the tests and ran them. 14 assertions
failed against the code at the time, and verified mechanically one by one, **all held**.

Among them, `test_fallback_minority_truncation_leaves_a_mark` was **raised first by deepseek
and written up as a test by claude after review** — the weaker model (with external scan
material) raised it, the stronger one confirmed and formalised it. This is the first time
this project observed the debate really producing something neither side would have had
alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.stance import parse_stance
from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from sesa.record import Recorder, new_run_id
from sesa.types import Outcome

sys.path.insert(0, str(Path(__file__).parent))
from test_bottom_lines import participant

pytestmark = pytest.mark.asyncio


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


# ═══════════════════════════════════════════════════════════════════════════ # Bottom line 3: a
# deadlock really was detected and written up as "the rounds ran out"
# ═══════════════════════════════════════════════════════════════════════════ #


async def test_a_detected_deadlock_is_not_relabelled_as_out_of_rounds(tmp_path):
    """matrix.py:215 `EXHAUSTED if terminating else DEADLOCK` — the termination condition wins.

    The stall counter reaches stability_window only in **the last round**, when rounds_left==0,
    so a deadlock that has been detected is downgraded to "⏳ the rounds or the budget ran out".
    The factory defaults max_rounds=4 / stability_window=2 (config.py:155-156,
    sesa.example.yaml:79-80) land exactly on that intersection: both positions unmoved from start
    to finish, and stalled reaches 2 only in round 3 (the last).

    The cost is concrete: the banner tells the user "the discussion is unfinished", so they add
    rounds — while the engine has already measured that more rounds will not help.
    """
    verdict, result_md, _ = await _run(
        tmp_path,
        [participant("a", FAKE_VERDICT="disagree"), participant("b", FAKE_VERDICT="disagree")],
        max_rounds=4,  # the factory defaults
    )
    assert verdict.outcome == Outcome.DEADLOCK.value, (
        f"neither side gave an inch over 4 rounds and the outcome is {verdict.outcome}; "
        "changing max_rounds to 5 turns it into deadlock — the only difference being whether there were enough rounds"
    )
    assert "轮数或预算耗尽" not in result_md


# ═══════════════════════════════════════════════════════════════════════════ # Bottom lines 2/4:
# partial_coverage_consensus's deliverable contradicts itself
# ═══════════════════════════════════════════════════════════════════════════ #


async def _partial_coverage(tmp_path):
    return await _run(
        tmp_path,
        [participant("claude"), participant("kimi"), participant("mute", FAKE_MODE="no_stance")],
        max_rounds=2,
    )


async def test_partial_coverage_result_does_not_contradict_its_own_banner(tmp_path):
    """report.py:128's allowlist holds only CONSENSUS / CONSENSUS_WITH_RESERVATIONS.

    The previous round added a dedicated branch for NOT_MEASURED and none for
    PARTIAL_COVERAGE_CONSENSUS, so line 3 of one RESULT.md says "🟠 consensus with partial
    coverage" and a dozen lines later it says "this deliberation did not reach consensus". Zero
    explicit opposition and yet "not settled" — precisely what bottom line 2 exists to prevent:
    labelling missing data as disagreement.
    """
    verdict, result_md, _ = await _partial_coverage(tmp_path)
    assert verdict.outcome == Outcome.PARTIAL_COVERAGE_CONSENSUS.value
    assert "并未达成共识" not in result_md, (
        "the banner says 'consensus with partial coverage' and the prose says 'this deliberation did not reach consensus' — one document contradicting itself"
    )


async def test_result_md_says_how_much_was_never_measured(tmp_path):
    """types.py:195 "must carry coverage" — and render_result never reads either field.

    ``Result.coverage`` / ``Result.unmeasured_cells`` reach result.json only; in the RESULT.md
    people read, 5% coverage and 95% coverage look exactly the same.
    """
    verdict, result_md, _ = await _partial_coverage(tmp_path)
    assert verdict.outcome == Outcome.PARTIAL_COVERAGE_CONSENSUS.value
    named = "mute → claude" in result_md or "mute → kimi" in result_md
    quantified = re.search(r"覆盖[率]?\s*[:：]?\s*\d", result_md) is not None
    assert named or quantified, (
        "RESULT.md has one qualitative sentence, 'some cells were not measured': no coverage figure and no list of which cells. "
        "5% coverage and 95% coverage look identical in this deliverable"
    )


async def test_partial_coverage_has_a_terminal_banner(tmp_path):
    """report.py:30 pins banner completeness with an assert; cli.py's OUTCOME_STYLE does not.

    So that grade degrades in the terminal to the bare enum value ``partial_coverage_consensus``,
    white on white, without the sentence "not measured is not agreement" — and the terminal is
    the default output.
    """
    from sesa.cli import OUTCOME_STYLE

    missing = sorted(o.value for o in Outcome if o.value not in OUTCOME_STYLE)
    assert not missing, (
        f"these outcomes have no banner in the terminal and print as a bare enum value: {missing}"
    )


# ═══════════════════════════════════════════════════════════════════════════ # Bottom line 2: total
# failure written up as "this protocol does not measure consensus"
# ═══════════════════════════════════════════════════════════════════════════ #


async def test_total_failure_is_not_dressed_up_as_not_measured(tmp_path):
    """engine.py:645 unconditionally rewrites a non-measuring protocol's outcome to NOT_MEASURED.

    But engine.py:190 had already set EXHAUSTED because "every participant failed this round".
    The result: a run that produced not one character delivers "each answered independently,
    nobody seeing anybody, so there is no peer assessment to speak of" — writing up "everybody
    crashed" as "the protocol is designed that way", with a CLI exit code of 4 (whose own comment
    says that grade should not go red in CI).
    """
    verdict, result_md, _ = await _run(
        tmp_path,
        [participant("a", FAKE_MODE="crash"), participant("b", FAKE_MODE="crash")],
        protocol="reflect",
        max_rounds=2,
    )
    assert verdict.outcome != Outcome.NOT_MEASURED.value, (
        "everyone crashed and nothing was produced, and the outcome is not_measured — the reason nothing was measured is stated wrongly"
    )
    assert "各方独立作答" not in result_md


# ═══════════════════════════════════════════════════════════════════════════ # Bottom line 2:
# REPORT.md accuses participants of "stance could not be parsed" when the engine never asked
# ═══════════════════════════════════════════════════════════════════════════ #


async def test_report_md_does_not_blame_participants_never_asked_for_a_stance(tmp_path):
    """reflect's Moves are expects_stance=False (engine.py:376 is where stance cards are collected),
    so the engine never asked a or b for one. But assess() runs anyway, and REPORT.md says "the
    stance of a, b could not be parsed; nobody may take a position on their behalf" and "2 open
    disagreements".

    The previous round fixed RESULT.md and the terminal progress output, and left the REPORT.md
    minutes unfixed.
    """
    verdict, _, report_md = await _run(
        tmp_path, [participant("a"), participant("b")], protocol="reflect", max_rounds=2
    )
    assert verdict.outcome == Outcome.NOT_MEASURED.value
    assert "立场未能解析" not in report_md, (
        "accuses the participants of handing in no stance card when the engine never asked for one"
    )
    assert "未决分歧" not in report_md, (
        "this protocol produces no peer assessment and it is accounted for as 'N open disagreements'"
    )


# ═══════════════════════════════════════════════════════════════════════════ # Bottom line 4: an
# open disagreement must come with a way out — the ones the engine backfills do not
# ═══════════════════════════════════════════════════════════════════════════ #


async def test_engine_filled_disagreements_still_offer_a_way_out(tmp_path):
    """rapporteur.py:200 hard-codes decisive_question to "", while report.py:148's "**next step**:
    sesa resume …" is emitted only when it is non-empty.

    So the path where the rapporteur omitted a disagreement and the engine filled it in
    mechanically from the matrix — precisely the degraded scenario that most needs to point
    somewhere — lists the disagreement in RESULT.md and offers not one way out.
    """
    _, result_md, _ = await _run(
        tmp_path,
        [participant("a", FAKE_VERDICT="disagree"), participant("b", FAKE_VERDICT="disagree")],
        max_rounds=4,
    )
    assert "分歧 1" in result_md, "the premise: this path really did list a disagreement"
    assert "sesa resume" in result_md, "listed an open disagreement and offered no way out"


# ═══════════════════════════════════════════════════════════════════════════ # Bottom line 2: an
# agree with reservations promoted to a clean agree by prefix matching
# ═══════════════════════════════════════════════════════════════════════════ #


@pytest.mark.parametrize(
    "verdict", ["agree with major reservations", "agree, but only on scope", "agree except X"]
)
async def test_a_hedged_agree_is_not_an_explicit_agree(verdict):
    """stance.py:107-109 matches with ``token.startswith(valid)``.

    "Only a parseable **explicit** agree counts as resolved" — but "agree with major
    reservations" matches the prefix "agree" and counts straight away as one clean agreement,
    without even carrying residuals. And the direction is inverted too: "partially agree" is
    judged unknown (conservative) while "agree with major reservations" is judged agree
    (aggressive).
    """
    text = (
        '```json\n{"position":"p","confidence":0.9,'
        f'"stance_on":{{"b":{{"verdict":"{verdict}","reason":"r"}}}}}}\n```'
    )
    stance = parse_stance(text, "a", 1, ["b"])
    assert stance is not None
    on = stance.stance_on.get("b")
    assert on is None or on.verdict != "agree", (
        f"{verdict!r} was taken for an explicit agree — it is a position with reservations and must not count as a resolved cell"
    )


# ═══════════════════════════════════════════════════════════════════════════ # Added during round
# two: resume and briefing, two paths never tested
# `grep -rn load_state tests/` returned **nothing** before these were added — resume is the README's
# "way out of a deadlock", and no test had ever run it. All three below failed against the code at
# the time. ═══════════════════════════════════════════════════════════════════════════ #


def _resumable_run(tmp_path):
    """Build a persisted deliberation for load_state to read back."""
    import sesa.events as ev
    from sesa.record import Recorder
    from sesa.state import Turn

    run_id = new_run_id()
    rec = Recorder(tmp_path, run_id)
    rec.emit(
        ev.RunStart(
            task="该用 Postgres 还是 SQLite？",
            participants=["a", "b"],
            protocol="debate",
            max_rounds=4,
            run_id=run_id,
        )
    )
    raw = (
        "我主张 Postgres。\n\n```json\n"
        '{"position":"用 Postgres","confidence":0.9,'
        '"premises":["写入峰值超过 2k QPS","团队已有 DBA"],'
        '"stance_on":{"b":{"verdict":"disagree","reason":"b 忽略了并发写"}}}\n```'
    )
    rec.emit(
        ev.StanceEmit(
            round=1,
            participant="a",
            stance={
                "position": "用 Postgres",
                "confidence": 0.9,
                "stance_on": {"b": "disagree"},
                "reasons": {"b": "b 忽略了并发写"},
                "residuals": {},
                "key_claims": ["SQLite 单写者模型撑不住"],
                "changed": False,
                "unknown": False,
            },
        )
    )
    rec.save_turn(
        Turn(
            participant="a",
            round=1,
            phase=0,
            kind="revise",
            text="我主张 Postgres。",
            raw=raw,
            thinking="其实我没把握，随便挑个听起来硬的理由顶上去。",
        )
    )
    rec.close()
    return tmp_path / "runs" / run_id


def _load(run_dir, **kw):
    from sesa.record import load_state
    from sesa.types import ParticipantSpec

    return load_state(
        run_dir,
        [ParticipantSpec(id="a", adapter="cli"), ParticipantSpec(id="b", adapter="cli")],
        max_rounds=4,
        **kw,
    )


async def test_resume_does_not_leak_private_thinking_into_others_context(tmp_path):
    """record.py:183 reads the whole archive file into ``Turn.text``.

    ``save_turn`` (record.py:52-66) appends two archive details to the same ``.md``: the model's
    raw output (**stance card included**) and the reasoning. The comments say it plainly — "the
    stance card is stripped so that machine-readable matter does not go into other people's
    context", "the reasoning goes to disk only and never into other people's context". And
    ``load_state`` reads ``path.read_text()`` whole, so ``Turn.text`` (state.py:24 says "this is
    the version fed to the others") brings both back on a resume.

    The failing scenario: ``sesa resume`` with ``share_thinking=never`` (the factory default) —
    in the first resumed round, a's private reasoning appears verbatim in b's context.
    This is not a rendering blemish: it overturns the entire meaning of the share_thinking
    setting.
    """
    from sesa import prompts

    state = _load(_resumable_run(tmp_path), share_thinking="never")
    others = prompts.render_others(state.rounds[-1], exclude="b", share_thinking=False)
    assert "随便挑个听起来硬的理由" not in others, (
        "share_thinking=never, and after a resume a's private reasoning still entered b's context"
    )
    assert '"confidence"' not in others, (
        "the raw stance card (machine-readable matter) leaked back into other people's context"
    )


async def test_resume_keeps_the_premises_that_conclusions_hang_on(tmp_path):
    """engine.py:437-450's stance.emit payload **has no premises field at all**.

    The reason ``Stance.premises`` exists is written at types.py:162-166: listed separately so it
    can be "attacked one by one", and it is "where `resume --inject`'s 'veto a premise'
    intervention gets its purchase". record.py:15 also declares "events.jsonl is the only source
    of truth". Together those two sentences make a promise the code does not keep: the premises
    never entered the event stream, so ``load_state`` (record.py:186-208) naturally cannot rebuild
    them.

    The failing scenario: a declares two premises in round 1 → resume → in round 2 b's
    ``render_others`` has no "### Premises a declared" section (prompts.py:294's condition is a
    non-empty ``stance.premises``). Bottom line 4, "the conclusion is delivered together with its
    premises", does not hold on the resume path — and resume is precisely the entrance designed
    for attacking premises.
    """
    from sesa import prompts

    state = _load(_resumable_run(tmp_path))
    stance = state.rounds[-1].stances["a"]
    assert stance.position == "用 Postgres", "the premise: the position itself was read back"
    assert stance.premises, (
        "the conclusion came back and the premises were lost — and the premises are what resume --inject exists to veto"
    )
    assert "Premises a declared" in prompts.render_others(state.rounds[-1], exclude="b")


async def test_an_unreadable_briefing_does_not_silently_mute_a_participant(tmp_path):
    """engine.py:143-148 catches a briefing read failure, and prompts.py:52 re-reads it every round.

    The comment on the engine's opening ``load_briefing`` failure says "say so when it cannot be
    read, but do not drag the whole run down" — and it only ``continue``s past that opening read.
    The ``prompts.system_prompt`` (prompts.py:52) actually used for every turn **reads the same
    file again**, and nothing catches it there: every ``turn.end`` for that participant carries a
    ``ValueError`` and it never says a word.

    The failing scenario: ``briefing: "@notes.md"`` with one letter wrong in the path ⇒ a is
    absent throughout, outcome ``exhausted`` ("the rounds or the budget ran out"). What the user
    sees is one opening warning and a "we did not finish" conclusion, while the truth is that a
    three-party deliberation had only two parties speaking from start to finish.
    """
    a = participant("a")
    a.options["briefing"] = "@" + str(tmp_path / "does-not-exist.md")
    run_id = new_run_id()
    engine = Engine(
        [a, participant("b")],
        build_protocol("debate"),
        matrix=StanceMatrix(stability_window=2),
        recorder=Recorder(tmp_path, run_id),
        max_rounds=1,
    )
    events = [e async for e in engine.run("该用 Postgres 还是 SQLite？")]
    spoke = {e.participant for e in events if e.t == "turn.end" and e.error is None}
    assert "a" in spoke, (
        "the briefing file could not be read ⇒ a said nothing at all. "
        "Failing to read private material should degrade to 'take part without private material', not throw them out of the deliberation"
    )


def test_fallback_minority_truncation_leaves_a_mark(tmp_path):
    """rapporteur.py:214 ``text.strip()[:2000]`` — truncation leaves no trace.

    This one was raised by deepseek in this deliberation; I confirmed it on review and wrote it up
    as a test. On the degraded path (drafting failed) a minority opinion is cut to 2000 characters
    and RESULT.md's "## Minority opinions" prints it as-is (report.py:232-233), leaving the reader
    with no idea there was more.
    """
    from sesa.consensus import rapporteur as rap
    from sesa.consensus.matrix import StanceMatrix as _M
    from sesa.state import DeliberationState, RoundRecord, Turn
    from sesa.types import ParticipantSpec

    long_text = "我反对，理由如下。" * 400  # well over 2000 characters
    st = DeliberationState(
        task="t",
        participants=[
            ParticipantSpec(id="a", adapter="cli"),
            ParticipantSpec(id="b", adapter="cli"),
        ],
        max_rounds=2,
    )
    rec = RoundRecord(index=1)
    rec.turns.append(Turn(participant="a", round=1, phase=0, kind="revise", text=long_text))
    st.rounds.append(rec)
    report = _M().assess(st)
    draft = rap.fallback_draft(st, report)
    kept = draft["minority"]["a"]
    assert len(kept) < len(long_text.strip()), "the premise: this passage really was truncated"
    assert kept.rstrip().endswith(("…", "...", "）", ")")), (
        f"the minority opinion was cut by {len(long_text) - len(kept)} characters, with no marker at the end"
    )
