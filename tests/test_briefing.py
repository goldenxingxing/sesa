"""Background material private to one participant.

This is the one deliberately created information asymmetry in Sesa. By default everyone
gets the same task and the same working copy, and the debate can only run on "thinking
differently"; while in reality what makes a joint deliberation irreplaceable is that **the
parties hold different material** — one sees the caller, another the callee; one has run a
static scan, another has only the source.

The cost has to be stated: with asymmetric material, **a disagreement may be only an
information gap and not a difference in judgement**. So it goes to disk, into the event
stream, and is stated in the deliverable — private ≠ leaves no trace.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sesa import prompts
from sesa.report import render_result
from sesa.state import DeliberationState
from sesa.types import Outcome, ParticipantSpec, Result


def _state(*specs: ParticipantSpec) -> DeliberationState:
    return DeliberationState(task="审这段代码", participants=list(specs), max_rounds=2)


def test_a_briefing_reaches_only_its_owner():
    alice = ParticipantSpec(
        id="alice", adapter="cli", options={"briefing": "静态扫描报告：第 42 行空指针"}
    )
    bob = ParticipantSpec(id="bob", adapter="cli")
    state = _state(alice, bob)

    assert "第 42 行空指针" in prompts.system_prompt(state, "alice")
    assert "第 42 行空指针" not in prompts.system_prompt(state, "bob")


def test_a_briefing_can_come_from_a_file(tmp_path):
    report = tmp_path / "ocr.md"
    report.write_text("发现 1 处 high：engine.py 提前返回导致状态未初始化", encoding="utf-8")
    spec = ParticipantSpec(id="alice", adapter="cli", options={"briefing": f"@{report}"})

    assert "提前返回导致状态未初始化" in prompts.load_briefing(spec)


def test_a_missing_briefing_file_fails_loudly(tmp_path):
    """Failing to read it is an error, never quietly treated as empty — that would leave a
    deliberation looking normal while material was missing.
    """
    spec = ParticipantSpec(id="alice", adapter="cli", options={"briefing": f"@{tmp_path}/nope.md"})

    with pytest.raises(ValueError, match="briefing could not be read"):
        prompts.load_briefing(spec)


def test_the_owner_is_told_to_restate_it_in_the_open():
    """ "The tool says there is a problem" is not an argument — nobody can examine what they cannot
    see.
    """
    spec = ParticipantSpec(id="alice", adapter="cli", options={"briefing": "扫描结果"})

    rendered = prompts.system_prompt(
        _state(spec, ParticipantSpec(id="bob", adapter="cli")), "alice"
    )

    assert "其他参与者看不到这一段" in rendered
    assert "用自己的话讲进正式发言" in rendered


def test_no_briefing_changes_nothing():
    plain = ParticipantSpec(id="alice", adapter="cli")
    assert prompts.load_briefing(plain) == ""
    assert prompts.render_briefing("") == ""


def test_the_deliverable_warns_that_the_sides_were_not_symmetric():
    """With asymmetric material a disagreement may be only an information gap — the reader has to
    know that first.
    """
    result = Result(
        run_id="r1",
        task="审这段代码",
        outcome=Outcome.CONSENSUS,
        conclusion="接受该改动。",
        briefings={"deepseek": 4200},
    )

    text = render_result(result)

    assert "各方材料不对称" in text
    assert "deepseek（4200 字）" in text
    assert "信息差" in text


def test_a_symmetric_run_says_nothing_about_briefings():
    text = render_result(Result(run_id="r1", task="t", outcome=Outcome.CONSENSUS))
    assert "材料不对称" not in text


def test_the_briefing_is_written_to_disk(tmp_path):
    """Private ≠ leaves no trace: it influenced this deliberation, so it must be reviewable."""
    from sesa.record import Recorder, new_run_id

    rec = Recorder(tmp_path, new_run_id())
    path = rec.save_briefing("deepseek", "扫描发现 3 处 high")
    rec.close()

    assert Path(path).read_text(encoding="utf-8") == "扫描发现 3 处 high"
