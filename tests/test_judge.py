"""The model judge: read the transcript directly and answer "did the positions substantively
change".

Neither counting nor embedding proxies can answer that question (DESIGN.md §14.5, §14.8).
The judge reads a transcript of something **already over** and influences no deliberation —
a different matter entirely from the design's "no referee", and conflating the two was a
reasoning error.

The judge's own failure modes have to be guarded one by one, and this file is the contract
for those guards.
"""

from __future__ import annotations

import pytest

from sesa import judge as jd

TRANSCRIPT = "第一轮：我主张方案 A，因为运维成本低。\n第三轮：我改为主张方案 B，被扩展性论据说服。"


def test_fabricated_quotes_are_rejected_mechanically():
    """The judge's commonest failure is inventing quotations. A quotation has to be findable in the
    transcript, and one that is not is void.
    """
    raw = (
        '```json\n{"participants": {'
        '"honest": {"verdict": "实质改变", "first_position": "我主张方案 A，因为运维成本低",'
        ' "final_position": "我改为主张方案 B", "reason": "被说服"},'
        '"liar": {"verdict": "实质改变", "first_position": "这句话转录里根本不存在过",'
        ' "final_position": "这句同样是编造出来的内容", "reason": "编的"}'
        '}, "overall": "变了"}\n```'
    )
    report = jd.parse(raw, TRANSCRIPT, "run1", "claude")
    assert [v.participant for v in report.usable] == ["honest"]
    assert [v.participant for v in report.rejected] == ["liar"]


def test_punctuation_differences_are_tolerated():
    """Quotation checking ignores whitespace and punctuation and nothing else — it must not be
    loose enough to let an invention through.
    """
    assert jd.verify_quote("我主张方案 A、因为运维成本低！", TRANSCRIPT)
    assert not jd.verify_quote("我主张方案 C，因为运维成本低", TRANSCRIPT)


def test_very_short_quotes_are_not_accepted():
    """A quotation too short hits by chance too easily and is not accepted."""
    assert not jd.verify_quote("方案", TRANSCRIPT)
    assert not jd.verify_quote("A", TRANSCRIPT)


def test_participant_cannot_judge_its_own_run():
    """Self-preference makes a participant systematically overrate its own change of position."""
    with pytest.raises(ValueError, match="cannot also judge"):
        jd.assert_not_participant("kimi", ["kimi", "deepseek"])
    jd.assert_not_participant("claude", ["kimi", "deepseek"])


def test_agreement_exposes_an_unstable_judge():
    """Different verdicts over one transcript mean this judge is unusable for this task."""

    def report(verdict: str) -> jd.JudgeReport:
        r = jd.JudgeReport(run_id="r", judge="claude")
        r.verdicts.append(
            jd.ParticipantVerdict(
                participant="a",
                verdict=verdict,
                first_position="",
                final_position="",
                reason="",
                first_verified=True,
                final_verified=True,
            )
        )
        return r

    stable = jd.agreement([report("实质改变")] * 3)
    assert stable["a"] == 1.0
    shaky = jd.agreement([report("实质改变"), report("仅扩充论证"), report("无变化")])
    assert shaky["a"] < 0.5


def test_transcript_excludes_the_raw_archive_block(tmp_path):
    """The archive's raw-output fold is there for checking the parsing and does not belong in the
    transcript the judge reads.
    """
    from sesa.record import Recorder
    from sesa.state import Turn

    # **Use the code that really writes the archive to build this archive.** Hand-writing an archive
    # format has it drift from the real one: when the split marker became language-independent, what
    # this test guarded was a format that no longer existed, and it stayed green.
    recorder = Recorder(tmp_path, "r1")
    turn = Turn("a", 0, 0, "draft", "给人读的正文。")
    turn.raw = '给人读的正文。\n\n```json\n{"residuals": []}\n```'
    recorder.save_turn(turn)

    run = tmp_path / "runs" / "r1"
    (run / "events.jsonl").write_text(
        '{"t": "run.start", "ts": 1.0, "run_id": "r1", "task": "议题",'
        ' "participants": ["a"], "protocol": "debate", "max_rounds": 1}\n',
        encoding="utf-8",
    )
    transcript, participants = jd.build_transcript(run)
    assert participants == ["a"]
    assert "给人读的正文" in transcript
    assert "residuals" not in transcript, (
        "the archived raw output got into the transcript the judge reads"
    )


def test_same_model_under_a_different_id_is_also_rejected():
    """A different id does not make a different model — self-preference follows the model, not the
    name.

    Nearly walked into it: exp-residual's participants were claude-conservative /
    claude-radical, judged by a judge with the id claude — different names, the same model
    behind them.
    """
    from sesa.types import ParticipantSpec

    def cli(pid: str) -> ParticipantSpec:
        return ParticipantSpec(id=pid, adapter="cli", options={"command": ["claude", "-p"]})

    specs = [cli("claude"), cli("claude-conservative"), cli("claude-radical")]
    with pytest.raises(ValueError, match="the same underlying model"):
        jd.assert_not_participant("claude", ["claude-conservative", "claude-radical"], specs)


def test_a_genuinely_different_model_passes():
    from sesa.types import ParticipantSpec

    specs = [
        ParticipantSpec(id="claude", adapter="cli", options={"command": ["claude", "-p"]}),
        ParticipantSpec(id="ds", adapter="openai_compat", model="deepseek-chat"),
    ]
    jd.assert_not_participant("claude", ["ds"], specs)


def test_verification_rate_is_the_judges_own_reliability_reading():
    """The judge itself needs evaluating too. Measured over one batch of deliberations, judges
    differ greatly in how many of their verdicts are voided.
    """

    def verdict(pid: str, verified: bool) -> jd.ParticipantVerdict:
        return jd.ParticipantVerdict(
            participant=pid,
            verdict="实质改变",
            first_position="",
            final_position="",
            reason="",
            first_verified=verified,
            final_verified=verified,
        )

    report = jd.JudgeReport(run_id="r", judge="j")
    report.verdicts = [verdict("a", True), verdict("b", False), verdict("c", True)]
    assert report.verification_rate == pytest.approx(2 / 3)


def test_hallucinated_participants_are_dropped():
    """Measured, a judge took the **file names** in the transcript for participant ids."""
    report = jd.JudgeReport(run_id="r", judge="j")
    for pid in ("alice", "r00_p0_alice_draft"):
        report.verdicts.append(
            jd.ParticipantVerdict(
                participant=pid,
                verdict="无变化",
                first_position="",
                final_position="",
                reason="",
                first_verified=True,
                final_verified=True,
            )
        )
    stray = report.drop_unknown_participants(["alice"])
    assert [v.participant for v in stray] == ["r00_p0_alice_draft"]
    assert [v.participant for v in report.verdicts] == ["alice"]


def test_cross_model_agreement_is_distinguished_from_repetition():
    """Repeating one judge measures certainty only; a homogeneous judge's errors correlate at
    ρ≈0.95, and being wrong together also means "agreeing" together.
    """

    def report(judge: str, verdict: str) -> jd.JudgeReport:
        r = jd.JudgeReport(run_id="r", judge=judge)
        r.verdicts.append(
            jd.ParticipantVerdict(
                participant="a",
                verdict=verdict,
                first_position="",
                final_position="",
                reason="",
                first_verified=True,
                final_verified=True,
            )
        )
        return r

    same_judge = [report("claude", "实质改变")] * 3
    assert jd.agreement(same_judge)["a"] == 1.0
    assert jd.cross_agreement(same_judge) == {}  # with only one judge there is no cross
    # evidence

    cross = [report("claude", "实质改变"), report("kimi", "实质改变")]
    assert jd.cross_agreement(cross)["a"] == 1.0


def test_the_tool_states_that_change_is_not_quality():
    """Without ground truth, a helpful influence and a harmful one cannot be told apart — the tool
    has to say so itself.
    """
    from sesa import i18n

    # This disclaimer goes into the eval report and follows the report's language — both languages
    # have to have it.
    assert "did it change for the better" in jd.change_is_not_quality()
    with i18n.scoped("zh"):
        assert "变得好不好" in jd.change_is_not_quality()
    assert "57" in jd.change_is_not_quality()
