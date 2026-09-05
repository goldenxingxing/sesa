"""The engine end to end: real subprocesses playing the participants, driving the whole
deliberation pipeline.

No mocks — the contract between Engine and CliAdapter (streaming, exit codes, feeding stdin,
stance-card extraction and retry) holds only over real processes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sesa.budget import Budget
from sesa.consensus.matrix import StanceMatrix
from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from sesa.record import Recorder, new_run_id, read_events
from sesa.types import Outcome, ParticipantSpec

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


async def drive(engine: Engine, task: str = "该用 Postgres 还是 SQLite？") -> list:
    return [event async for event in engine.run(task)]


def kinds(events: list) -> list[str]:
    return [e.t for e in events]


def final(events: list):
    return next(e for e in events if e.t == "verdict.final")


# --------------------------------------------------------------------------- #


async def test_two_agreeing_participants_reach_consensus(tmp_path):
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=2,
    )
    events = await drive(engine)
    verdict = final(events)
    assert verdict.outcome == Outcome.CONSENSUS.value
    assert verdict.unresolved == 0
    assert verdict.drafted_by in ("claude", "kimi")  # the rapporteur role rotates among the
    # participants


async def test_persistent_disagreement_is_reported_as_deadlock_not_consensus(tmp_path):
    """Stuck is not the same as united — this project's honesty bottom line."""
    engine = Engine(
        [
            participant("claude", FAKE_VERDICT="disagree"),
            participant("kimi", FAKE_VERDICT="disagree"),
        ],
        build_protocol("debate"),
        matrix=StanceMatrix(stability_window=2),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=6,
    )
    verdict = final(await drive(engine))
    assert verdict.outcome == Outcome.DEADLOCK.value
    assert verdict.unresolved > 0


async def test_phase_moves_run_concurrently(tmp_path):
    """Parallel within a phase: both turn.start events should precede either turn.end."""
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        max_rounds=1,
    )
    seq = kinds(await drive(engine))
    first_end = seq.index("turn.end")
    assert seq[:first_end].count("turn.start") == 2


async def test_unparseable_stance_becomes_unknown_and_blocks_consensus(tmp_path):
    """No stance card extracted means a retry; still failing means unknown — no guessing, no writing
    on their behalf.
    """
    engine = Engine(
        [participant("claude", FAKE_MODE="no_stance"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=1,
    )
    events = await drive(engine)
    stances = [e for e in events if e.t == "stance.emit"]
    claude = next(e for e in stances if e.participant == "claude")
    assert claude.degraded is True  # took the retry path
    assert claude.stance["unknown"] is True
    assert final(events).outcome != Outcome.CONSENSUS.value


async def test_one_participant_crashing_does_not_abort_the_run(tmp_path):
    engine = Engine(
        [participant("claude", FAKE_MODE="crash"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=1,
    )
    events = await drive(engine)
    ends = [e for e in events if e.t == "turn.end"]
    assert any(e.error for e in ends) and any(not e.error for e in ends)
    assert final(events)  # and the run still wrapped up with a result


async def test_all_participants_crashing_stops_the_run(tmp_path):
    engine = Engine(
        [participant("a", FAKE_MODE="crash"), participant("b", FAKE_MODE="crash")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=2,
    )
    events = await drive(engine)
    assert any(e.t == "error" for e in events)


async def test_false_consensus_is_flagged_not_papered_over(tmp_path):
    """The stance cards all claim agreement while the prose substantively conflicts — it has to be
    labelled honestly.
    """
    engine = Engine(
        [participant("claude", FAKE_CONFLICT="两人对索引策略的描述互相矛盾"), participant("kimi")],
        build_protocol("debate"),
        rapporteur="claude",
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=2,  # in round 0 nobody has read anybody, so convergence is impossible
    )
    events = await drive(engine)
    assert any(e.t == "false_consensus" for e in events)
    assert final(events).outcome == Outcome.FALSE_CONSENSUS.value


async def test_low_confidence_blocks_consensus(tmp_path):
    engine = Engine(
        [participant("claude", FAKE_CONF=0.2), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=1,
    )
    assert final(await drive(engine)).outcome != Outcome.CONSENSUS.value


async def test_wall_clock_budget_stops_the_run(tmp_path):
    """A CLI adapter cannot report token counts, and the wall clock is the only reliable gate."""
    engine = Engine(
        [
            participant("claude", FAKE_VERDICT="disagree"),
            participant("kimi", FAKE_VERDICT="disagree"),
        ],
        build_protocol("debate"),
        budget=Budget(max_wall_seconds=0.01),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=8,
    )
    verdict = final(await drive(engine))
    assert verdict.outcome == Outcome.EXHAUSTED.value
    assert verdict.rounds_used < 8


async def test_ensemble_runs_exactly_one_round(tmp_path):
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("ensemble"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=5,
    )
    assert final(await drive(engine)).rounds_used == 1


async def test_artifacts_are_written_and_replayable(tmp_path):
    run_id = new_run_id()
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, run_id),
        max_rounds=1,
    )
    events = await drive(engine)
    run_dir = tmp_path / "runs" / run_id

    for name in ("RESULT.md", "RESULT.json", "REPORT.md", "events.jsonl"):
        assert (run_dir / name).exists(), name
    assert list((run_dir / "turns").glob("*.md"))

    # the event stream is the only source of truth: what lands on disk has to match what was yielded
    replayed = read_events(run_dir)
    assert [e["t"] for e in replayed] == kinds(events)

    result = (run_dir / "RESULT.md").read_text(encoding="utf-8")
    for heading in ("## 结论", "## 共识依据", "## 未决分歧", "## 少数意见"):
        assert heading in result  # the skeleton is constant


async def test_engine_requires_at_least_two_participants():
    with pytest.raises(ValueError, match="at least 2 participants"):
        Engine([participant("solo")], build_protocol("debate"))


async def test_rapporteur_omitting_disagreements_is_corrected_by_the_matrix(tmp_path):
    """When the rapporteur omits a disagreement, the engine has to fall back on the disagreement
    matrix — the document must never say "they agree".

    This is where this project's honesty bottom line lands in code: the engine holds a computable
    ground truth and should not take the rapporteur's self-report on trust.
    """
    engine = Engine(
        [
            participant("claude", FAKE_VERDICT="disagree"),
            participant("kimi", FAKE_VERDICT="disagree"),
        ],
        build_protocol("debate"),
        matrix=StanceMatrix(stability_window=2),
        recorder=Recorder(tmp_path, (run_id := new_run_id())),
        max_rounds=4,
    )
    events = await drive(engine)
    verdict = final(events)
    # what this case cares about is whether the engine backfills when the rapporteur omits a
    # disagreement; whether the outcome is deadlock or exhausted depends on the round budget and is
    # not what this test asserts
    assert verdict.outcome != Outcome.CONSENSUS.value
    assert verdict.unresolved > 0

    # fake_agent's drafting output always has an empty disagreements, and the engine has to notice
    # and backfill
    assert any(e.t == "writer.mismatch" and e.kind == "omitted_disagreements" for e in events)

    result = (tmp_path / "runs" / run_id / "RESULT.md").read_text(encoding="utf-8")
    assert "无。各方在所有实质问题上意见一致。" not in result
    assert "### 分歧 1：" in result


async def test_round_zero_can_never_be_consensus(tmp_path):
    """In round 0 nobody has read anybody — declaring consensus there hollows out the whole
    deliberation.

    Regression: an early version asked participants to fill in stance_on in round 0, leaving the
    model nothing to do but invent; if they all happened to guess agree, the engine declared
    consensus after a round in which nothing was contested.
    """
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=1,
    )
    events = await drive(engine)
    first = next(e for e in events if e.t == "consensus.update")
    assert first.round == 0
    assert first.state != "converged"
    assert final(events).outcome != Outcome.CONSENSUS.value


async def test_participants_are_not_asked_about_unseen_others(tmp_path):
    """Round 0's stance card should hold no position on anyone else — they have not seen each other
    yet.
    """
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=2,
    )
    events = await drive(engine)
    stances = [e for e in events if e.t == "stance.emit"]
    round_zero = [e for e in stances if e.round == 0]
    assert round_zero and all(e.stance["stance_on"] == {} for e in round_zero)
    # once they see each other in round 1 there should be positions
    assert any(e.stance["stance_on"] for e in stances if e.round == 1)


async def test_consensus_becomes_possible_once_everyone_has_read_everyone(tmp_path):
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=2,
    )
    events = await drive(engine)
    assert final(events).outcome == Outcome.CONSENSUS.value


async def test_rapporteur_finding_disagreements_overrides_a_converged_matrix(tmp_path):
    """The rapporteur reads the whole thing and finds a real conflict while the stance cards claim
    agreement — that is false consensus.

    This direction matters more than "the matrix has a disagreement and the rapporteur omitted it":
    it means the rapporteur caught a substantive conflict the structured stance cards failed to
    reflect.
    """
    engine = Engine(
        [
            participant("claude", FAKE_DRAFT_DISAGREE="格式服从预算的分配策略"),
            participant("kimi"),
        ],
        build_protocol("debate"),
        rapporteur="claude",
        recorder=Recorder(tmp_path, (run_id := new_run_id())),
        max_rounds=2,
    )
    events = await drive(engine)
    assert any(e.t == "false_consensus" for e in events)
    verdict = final(events)
    assert verdict.outcome == Outcome.FALSE_CONSENSUS.value

    result = (tmp_path / "runs" / run_id / "RESULT.md").read_text(encoding="utf-8")
    assert "已达成共识" not in result
    assert "格式服从预算的分配策略" in result


async def test_long_task_does_not_become_the_document_title(tmp_path):
    """A topic is often an entire document, and pushing it into an H1 turns the whole document into a
    heading.
    """
    long_task = "评审这份 RFC\n\n" + "背景说明。" * 200
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, (run_id := new_run_id())),
        max_rounds=2,
    )
    [event async for event in engine.run(long_task)]
    result = (tmp_path / "runs" / run_id / "RESULT.md").read_text(encoding="utf-8")
    assert result.splitlines()[0] == "# 评审这份 RFC"
    assert "<details><summary>完整议题</summary>" in result


async def test_wall_clock_budget_constrains_each_turn_not_only_rounds(tmp_path):
    """The budget used to be checked only at round boundaries, and one slow participant overran a 900s
    cap to 1185s.
    """
    from sesa.budget import Budget

    budget = Budget(max_wall_seconds=30)
    engine = Engine(
        [participant("claude"), participant("kimi")],
        build_protocol("debate"),
        budget=budget,
        max_rounds=2,
    )
    assert engine._turn_budget() == pytest.approx(30, abs=0.1)  # 30s remaining < the
    # default 600s
    budget.started_at -= 28
    assert 0 < engine._turn_budget() <= 2  # one turn is squeezed too as the budget burns
    # down
    budget.started_at -= 100
    assert engine._turn_budget() == 1.0  # never pass 0 or a negative to the adapter


async def test_writer_mismatch_is_a_first_class_event(tmp_path):
    """The rapporteur is one of the participants on rotation — a mismatch is a conflict of role, a
    fact rather than a warning.
    """
    engine = Engine(
        [
            participant("claude", FAKE_VERDICT="disagree"),
            participant("kimi", FAKE_VERDICT="disagree"),
        ],
        build_protocol("debate"),
        matrix=StanceMatrix(stability_window=2),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=4,
    )
    events = await drive(engine)
    mismatch = [e for e in events if e.t == "writer.mismatch"]
    assert mismatch and mismatch[0].kind == "omitted_disagreements"
    assert mismatch[0].writer in ("claude", "kimi", "fallback")


async def test_reservations_downgrade_the_verdict_rather_than_failing_it(tmp_path):
    """Only reservations left is not a failure but "broadly agreed, with the reservations on record"."""
    engine = Engine(
        [
            participant("claude", FAKE_VERDICT="partial"),
            participant("kimi", FAKE_VERDICT="partial"),
        ],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, (run_id := new_run_id())),
        max_rounds=2,
    )
    verdict = final(await drive(engine))
    assert verdict.outcome == Outcome.CONSENSUS_WITH_RESERVATIONS.value

    result = (tmp_path / "runs" / run_id / "RESULT.md").read_text(encoding="utf-8")
    assert "有保留的共识" in result
    assert "## 保留意见" in result  # residuals enter the deliverable verbatim


async def test_participant_declared_timeout_is_honoured(tmp_path):
    """An agent CLI with tools may need minutes for one round, and must be able to ask for more time
    for itself.

    Regression: a participant's configured timeout was once silently overridden by the engine's
    turn_timeout default, which measurably killed an experiment using the claude CLI (900s
    configured, cut off at 600s).
    """
    from sesa.budget import Budget

    slow = participant("slow")
    slow.options["timeout"] = 900
    silent = participant("silent")
    silent.options.pop("timeout")  # declared nothing of its own
    engine = Engine(
        [slow, silent],
        build_protocol("debate"),
        budget=Budget(max_wall_seconds=3600),
        turn_timeout=600,
    )
    assert engine._turn_budget("slow") == 900  # the participant's own declaration is
    # honoured
    assert engine._turn_budget("silent") == 600  # one that declared nothing takes the
    # configured default


async def test_remaining_wall_budget_still_caps_a_generous_participant(tmp_path):
    """A participant may ask for more time, but not break through the run-wide wall-clock budget."""
    from sesa.budget import Budget

    slow = participant("slow")
    slow.options["timeout"] = 900
    budget = Budget(max_wall_seconds=120)
    engine = Engine([slow, participant("fast")], build_protocol("debate"), budget=budget)
    assert engine._turn_budget("slow") == pytest.approx(120, abs=1)
    budget.started_at -= 119
    assert 0 < engine._turn_budget("slow") <= 2


async def test_raw_output_is_archived_so_parsing_can_be_audited(tmp_path):
    """The archive has to keep the model's own words, or there is no way to check whether the parser
    dropped a residual.

    The stance card is stripped so that machine-readable matter does not go into other people's
    context; stripping it from the archive too would make "the event stream is the only source of
    truth" untrue — the source of that truth would have been thrown away.
    """
    engine = Engine(
        [participant("a", FAKE_VERDICT="partial"), participant("b", FAKE_VERDICT="partial")],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, (run_id := new_run_id())),
        max_rounds=2,
    )
    await drive(engine)

    archived = (tmp_path / "runs" / run_id / "turns" / "r01_p0_a_revise.md").read_text("utf-8")
    assert "the model's raw output" in archived
    assert '"residuals"' in archived  # the original stance card is findable in the archive

    # but the prose fed to the other participants should hold no stance card
    turn_text = archived.split("---\n\n<details>")[0]
    assert '"residuals"' not in turn_text


def _code_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
    (repo / "SPEC.md").write_text("规格正文：必须支持 ^ 与 ~", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)
    return repo


@pytest.mark.parametrize("protocol", ["debate", "reflect"])
async def test_file_output_instruction_is_last_in_every_protocol(tmp_path, protocol):
    """A participant that cannot write files must receive a prompt ending with the file-output
    instruction, and carrying the working directory.

    Only the debate family appends the stance-card instruction, so the same paragraph sits in
    different positions under different protocols. The measured consequence: 3 of 16 turns in the
    reflect group wrote code without marking the path (the working directory did not move) against
    0 of 16 in the debate group — the control baseline was thereby systematically weakened on code
    tasks, and the two groups became incomparable.
    """
    from sesa import patch
    from sesa.workspace import GitWorktreeWorkspace

    repo = _code_repo(tmp_path)
    dump = tmp_path / "prompts.txt"

    specs = [participant(pid, FAKE_DUMP=str(dump)) for pid in ("alice", "bob")]
    for spec in specs:
        spec.options["apply_code_blocks"] = True

    engine = Engine(
        specs,
        build_protocol(protocol),
        recorder=Recorder(tmp_path, new_run_id()),
        workspace=GitWorktreeWorkspace(repo, "run1"),
        max_rounds=1,
    )
    await drive(engine, task="实现 SPEC.md")

    seen = [p for p in dump.read_text(encoding="utf-8").split("\n\x00\n") if p.strip()]
    # the prompts follow the task's language, and the task is Chinese, so the instruction section is
    # Chinese too.
    from sesa import i18n
    from sesa import prompts as pr

    with i18n.scoped(pr.pick_language("实现 SPEC.md")):
        instruction = patch.INSTRUCTION.format().strip()
    coding = [p for p in seen if instruction in p]
    assert coding, f"{protocol}: not one prompt carries the file-output instruction"
    for prompt in coding:
        assert "规格正文" in prompt, (
            f"{protocol}: the working directory's contents never entered the prompt"
        )
        assert prompt.rstrip().endswith(instruction), (
            f"{protocol}: the file-output instruction is not at the end of the prompt"
        )


async def test_a_budget_capped_timeout_says_so_instead_of_blaming_the_participant(tmp_path):
    """When the cap was squeezed down by **the run-wide wall-clock budget**, the error must not say
    "this participant hung".

    The engine takes the smaller of "the participant's declared timeout" and "the remaining
    budget". Without saying which one is in force, the user goes and adjusts the participant's
    timeout — which does nothing at all.
    """
    from sesa.budget import Budget

    slow = tmp_path / "slow.py"
    slow.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    specs = [participant(pid) for pid in ("alice", "bob")]
    for spec in specs:
        spec.options["command"] = [sys.executable, str(slow)]
        spec.options["timeout"] = 600  # the participant asked for a long time

    engine = Engine(
        specs,
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        budget=Budget(max_wall_seconds=2),  # but the whole run's budget is only 2 seconds
        max_rounds=1,
    )
    events = await drive(engine)

    failed = [e for e in events if e.t == "turn.end" and e.error]
    assert failed, "there should be failed turns when the budget runs out"
    assert any("wall-clock budget" in e.error for e in failed), (
        f"it does not say the budget squeezed it, so the user will go and adjust the timeout: {[e.error for e in failed]}"
    )
    assert any("budget.max_wall_seconds" in e.error for e in failed), (
        "it has to say which parameter to adjust"
    )
