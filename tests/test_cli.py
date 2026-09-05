"""The CLI surface: argument handling, exit codes, output modes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from sesa.cli import app

runner = CliRunner()
FAKE = str(Path(__file__).parent / "fake_agent.py")

CONFIG = f"""
version: 1
participants:
  - id: claude
    adapter: cli
    command: ["{sys.executable}", "{FAKE}"]
    prompt: stdin
    env: {{FAKE_ID: claude}}
  - id: kimi
    adapter: cli
    command: ["{sys.executable}", "{FAKE}"]
    prompt: stdin
    env: {{FAKE_ID: kimi}}
protocol: debate
rounds: {{max: 2}}  # 第 0 轮谁也没读过谁，收敛至少要到第 1 轮
"""


def project(tmp_path: Path, config: str = CONFIG) -> Path:
    (tmp_path / "sesa.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def test_version_lists_adapters_and_protocols():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "openai_compat" in result.output and "adversarial" in result.output


def test_run_streams_jsonl_and_exits_zero_on_consensus(tmp_path, monkeypatch):
    """Regression: JSON mode used to return 2 forever because the if/elif chain swallowed
    verdict.final.
    """
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["run", "该用 Postgres 还是 SQLite？", "--json"])
    assert result.exit_code == 0  # consensus reached
    lines = [json.loads(x) for x in result.output.splitlines() if x.startswith("{")]
    assert lines[0]["t"] == "run.start"
    assert lines[-1]["t"] == "verdict.final"
    assert lines[-1]["outcome"] == "consensus"


def test_run_exits_two_when_no_consensus(tmp_path, monkeypatch):
    """The exit codes let CI decide on them: 0 consensus reached, 2 not reached."""
    config = CONFIG.replace("{FAKE_ID: claude}", "{FAKE_ID: claude, FAKE_VERDICT: disagree}")
    monkeypatch.chdir(project(tmp_path, config))
    result = runner.invoke(app, ["run", "议题", "--json"])
    assert result.exit_code == 2


def test_run_reads_task_from_file(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    (tmp_path / "rfc.md").write_text("# 一份待评审的 RFC\n\n内容。", encoding="utf-8")
    result = runner.invoke(app, ["run", "--file", "rfc.md", "--json"])
    assert result.exit_code == 0
    first = json.loads(result.output.splitlines()[0])
    assert "待评审的 RFC" in first["task"]


def test_run_writes_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    runner.invoke(app, ["run", "议题", "--json"])
    runs = list((tmp_path / ".sesa" / "runs").iterdir())
    assert len(runs) == 1
    for name in ("RESULT.md", "RESULT.json", "REPORT.md", "events.jsonl"):
        assert (runs[0] / name).exists()


def test_run_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["run", "--file", "nope.md"])
    assert result.exit_code == 1
    assert "No such file" in result.output


def test_run_rejects_single_participant(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["run", "议题", "-p", "claude"])
    assert result.exit_code == 1
    assert "at least 2 participants" in result.output


def test_run_reports_unknown_participant(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["run", "议题", "-p", "nobody", "-p", "claude"])
    assert result.exit_code == 1
    assert "No such participant" in result.output


def test_run_reports_unknown_protocol(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["run", "议题", "--protocol", "nope"])
    assert result.exit_code == 1
    assert "unknown protocol" in result.output


def test_participants_list(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["participants", "list"])
    assert result.exit_code == 0
    assert "claude" in result.output and "kimi" in result.output


def test_doctor_checks_each_participant(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "usable" in result.output


def test_doctor_reports_broken_participant(tmp_path, monkeypatch):
    config = CONFIG.replace("{FAKE_ID: kimi}", "{FAKE_ID: kimi, FAKE_MODE: crash}")
    monkeypatch.chdir(project(tmp_path, config))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "unusable" in result.output


def test_runs_and_report_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    runner.invoke(app, ["run", "该用 Postgres 还是 SQLite？", "--json"])
    listing = runner.invoke(app, ["runs"])
    assert listing.exit_code == 0
    run_id = next(p.name for p in (tmp_path / ".sesa" / "runs").iterdir())
    result = runner.invoke(app, ["report", run_id])
    assert result.exit_code == 0
    assert "## 未决分歧" in result.output


DEADLOCK_CONFIG = (
    CONFIG.replace("{FAKE_ID: claude}", "{FAKE_ID: claude, FAKE_VERDICT: disagree}")
    .replace("{FAKE_ID: kimi}", "{FAKE_ID: kimi, FAKE_VERDICT: disagree}")
    .replace("rounds: {{max: 2}}", "rounds: {max: 3, stability_window: 2}")
)


def test_resume_carries_prior_rounds_and_injects_new_information(tmp_path, monkeypatch):
    """A deadlock is not a terminus: one added piece of information carries on from where it
    stopped, with nothing re-run from the start.

    Every open disagreement in RESULT.md carries `sesa resume ... --inject` — that command has
    to really exist and really work, or it is a promise we made and did not keep.
    """
    monkeypatch.chdir(project(tmp_path, DEADLOCK_CONFIG))
    first = runner.invoke(app, ["run", "该用 Postgres 还是 SQLite？", "--json"])
    assert first.exit_code == 2  # not settled

    run_id = next(p.name for p in (tmp_path / ".sesa" / "runs").iterdir())
    second = runner.invoke(app, ["resume", run_id, "--inject", "峰值 QPS 约 3000", "--json"])
    assert second.exit_code in (0, 2, 3)

    events = [json.loads(x) for x in second.output.splitlines() if x.startswith("{")]
    kinds = [e["t"] for e in events]
    assert "run.resume" in kinds
    assert kinds[0] == "run.start"

    resume_event = next(e for e in events if e["t"] == "run.resume")
    assert resume_event["from_run"] == run_id
    assert resume_event["inject"] == "峰值 QPS 约 3000"

    # existing rounds carried in: a resume's first round is not numbered 0
    first_round = next(e for e in events if e["t"] == "round.start")
    assert first_round["round"] > 0

    # the added information entered the next round's context
    injected = next(e for e in events if e["t"] == "human.inject")
    assert injected["text"] == "峰值 QPS 约 3000"


def test_resume_rejects_unknown_run(tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path))
    result = runner.invoke(app, ["resume", "nope", "--inject", "x"])
    assert result.exit_code == 1
    assert "Cannot find" in result.output


def test_resume_rejects_a_different_participant_roster(tmp_path, monkeypatch):
    """With different people it is not a continuation of the same deliberation, and the positions
    in the stance cards would point at people who are not there.
    """
    monkeypatch.chdir(project(tmp_path, DEADLOCK_CONFIG))
    runner.invoke(app, ["run", "议题", "--json"])
    run_id = next(p.name for p in (tmp_path / ".sesa" / "runs").iterdir())

    swapped = DEADLOCK_CONFIG.replace("id: kimi", "id: gpt5")
    (tmp_path / "sesa.yaml").write_text(swapped, encoding="utf-8")
    result = runner.invoke(app, ["resume", run_id, "--inject", "x"])
    assert result.exit_code == 1
    assert "differs from the original run" in result.output
