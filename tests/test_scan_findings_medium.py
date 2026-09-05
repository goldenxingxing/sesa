"""The verification results for the full scan's medium items (checked one by one, not accepted
wholesale).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from sesa.budget import Budget
from sesa.config import _as_bool
from sesa.evidence.runner import run_verify
from sesa.patch import extract_files
from sesa.protocols import build
from sesa.types import Stance, StanceOn


def test_a_zero_limit_is_a_limit_not_an_absence():
    """`limit=0` is the legitimate setting "stop immediately", not "no limit set".

    A truthiness test has `exceeded()` honour it and `near_limit()` not — one setting behaving two
    opposite ways.
    """
    budget = Budget(max_wall_seconds=0.0)
    budget.reset()

    assert budget.exceeded() is not None
    assert budget.near_limit() is not None, "both places have to agree about the same setting"


def test_only_a_standalone_name_attribute_marks_a_file():
    """`filename=x.py` is not `name=x.py`. A missing word boundary takes it for a write instruction."""
    assert list(extract_files("```python name=a.py\nBODY\n```")) == ["a.py"]
    assert list(extract_files("```python path=c.py\nBODY\n```")) == ["c.py"]
    assert extract_files("```python filename=b.py\nBODY\n```") == {}
    assert extract_files("```python x-name=d.py\nBODY\n```") == {}


def test_a_quoted_false_in_yaml_is_still_false():
    """`bool("false")` is True. And this field decides whether residuals enter other people's
    context.
    """
    assert _as_bool("false") is False
    assert _as_bool("no") is False
    assert _as_bool(False) is False
    assert _as_bool("true") is True
    assert _as_bool(True) is True


def test_a_misspelled_turn_taking_is_refused_not_silently_downgraded():
    """Degrading silently to parallel is the worst handling: the user believes they configured
    sequential and gets parallel, and the two have entirely different consensus semantics.
    """
    with pytest.raises(ValueError, match="turn_taking"):
        build("debate", turn_taking="sequentail")

    for good in ("parallel", "sequential"):
        assert build("debate", turn_taking=good).turn_taking == good


def test_the_banner_completeness_guard_survives_python_dash_o():
    """`python -O` strips assert lines entirely, and the guard silently vanishes in production."""
    for path in ("src/sesa/report.py", "src/sesa/cli.py"):
        text = Path(path).read_text(encoding="utf-8")
        assert "assert set(OUTCOME" not in text, f"{path} still uses assert as an integrity guard"
        assert "raise RuntimeError" in text


def test_an_empty_partial_cannot_be_constructed_at_all():
    """An invariant written in a docstring and enforced by nobody is not written at all.

    The extraction layer downgrades an empty-payload partial to unknown, but **any code that
    constructs one directly bypasses that path** — resume recovery, the judge, and the tests.
    """
    with pytest.raises(ValueError, match="partial requires a non-empty residuals"):
        StanceOn(verdict="partial")

    assert StanceOn(verdict="partial", residuals=["尚未接受的点"]).verdict == "partial"


@pytest.mark.parametrize("bad", [7.5, -1.0, 100.0])
def test_a_confidence_outside_zero_to_one_is_refused(bad):
    """Models filling in 0–100 is common and the extraction layer divides by 100 — but direct
    construction bypasses that step. A confidence of 7.5 makes every bar meaningless.
    """
    with pytest.raises(ValueError, match="confidence"):
        Stance(participant="a", round=0, confidence=bad)


@pytest.mark.parametrize("good", [0.0, 0.5, 1.0, None])
def test_a_legitimate_confidence_still_works(good):
    assert Stance(participant="a", round=0, confidence=good).confidence == good


def test_a_compound_command_is_killed_as_a_group(tmp_path):
    """A `shell=True` timeout kills only the shell, and the process really running inside `a && b` is
    orphaned.
    """
    started = time.monotonic()
    code, summary = run_verify("sleep 30 & sleep 30", tmp_path, timeout=2)

    assert code == 124
    assert time.monotonic() - started < 10, (
        "the whole group should be reaped immediately on timeout"
    )
    assert "did not finish" in summary


def test_a_dirty_fingerprint_changes_when_only_the_content_changes(tmp_path):
    """`git status --porcelain` holds only status codes and file names.

    One file edited twice leaves an identical status line and an identical fingerprint — so "has
    the code changed" fails in the commonest case.
    """
    from sesa.workspace.base import Checkout
    from sesa.workspace.worktree import GitWorktreeWorkspace

    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
    target = repo / "impl.py"
    target.write_text("VALUE = 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)

    workspace = GitWorktreeWorkspace(repo, "run1")
    checkout = Checkout(participant="x", path=repo, branch=None)

    target.write_text("VALUE = 1\n", encoding="utf-8")
    first = workspace.revision_of(checkout)
    target.write_text("VALUE = 2  # 完全不同的实现\n", encoding="utf-8")
    second = workspace.revision_of(checkout)

    assert first and second
    assert first != second, (
        "when the content changes and the status code does not, the fingerprint has to change with it"
    )


def test_an_untracked_file_also_moves_the_fingerprint(tmp_path):
    """`git diff` cannot see untracked files — and an implementation a participant creates is exactly
    untracked.
    """
    from sesa.workspace.base import Checkout
    from sesa.workspace.worktree import GitWorktreeWorkspace

    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
    (repo / "seed.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)

    workspace = GitWorktreeWorkspace(repo, "run1")
    checkout = Checkout(participant="x", path=repo, branch=None)
    fresh = repo / "new_impl.py"

    fresh.write_text("VALUE = 1\n", encoding="utf-8")
    first = workspace.revision_of(checkout)
    fresh.write_text("VALUE = 2\n", encoding="utf-8")

    assert workspace.revision_of(checkout) != first


# --------------------------------------------------------------------------- # The low-severity
# items that affect correctness
# --------------------------------------------------------------------------- #


def test_a_failed_turn_is_not_served_back_as_last_round_answer():
    """The engine records failed turns in turns too (timeouts, crashes, empty replies).

    And `reflect` uses `latest_by` to fill in "your answer from the last round" — picking up the
    failed one tells the participant "you said nothing last round", erasing its own proposal.
    """
    from sesa.state import RoundRecord, Turn

    record = RoundRecord(0)
    record.turns = [
        Turn("a", 0, 0, "draft", "我的真实主张"),
        Turn("a", 0, 0, "draft", "", error="超时"),
    ]

    assert record.latest_by("a").text == "我的真实主张"
    assert record.latest_by("a", only_ok=False).error == "超时", (
        "it still has to be available when investigating a failure"
    )


@pytest.mark.parametrize(
    "protocol,option,value,should_warn",
    [
        # Warn only when **the option means something to that protocol and the user changed the
        # default**. The caller (the CLI) hands every config key to every protocol indiscriminately,
        # and warning about the ones that do not know a key is a wall of false alarms — that is not
        # the user misconfiguring anything, it is the caller taking a shortcut. council
        # **deliberately** discards turn_taking (all-see-all semantics require one snapshot, so
        # parallel is forced). A user explicitly configuring sequential and being ignored in silence
        # — that warning is right.
        ("council", "turn_taking", "sequential", True),
        ("adversarial", "proposer", "claude", False),  # adversarial knows proposer
        ("reflect", "turn_taking", "parallel", False),  # equal to the default
        ("debate", "proposer", "rotate", False),  # equal to the default
    ],
)
def test_an_option_a_protocol_ignores_is_reported_but_only_if_you_meant_it(
    protocol, option, value, should_warn
):
    """The silent discard is deliberate (one extra key must not kill the run), but it cannot be
    completely silent.

    Warn only about what **the user explicitly changed and the option really means something to
    that protocol**.
    This one was sent back once by claude in a deliberation: my earlier implementation warned about
    `proposer="input"` — and that value is **synthesised by the CLI itself**, with the user
    configuring nothing. A false alarm trains the user to ignore warnings, which is worse than the
    silent discard.
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build(protocol, **{option: value})

    assert bool(caught) is should_warn


def test_a_failed_write_does_not_leave_a_truncated_file(tmp_path):
    """`write_text` truncates before writing. A failure part-way leaves a **truncated** original —
    worse than not writing at all, because it looks like a successful change.
    """
    from sesa.patch import apply_files

    target = tmp_path / "impl.py"
    target.write_text("原来的完整内容\n" * 50, encoding="utf-8")
    original = target.read_text(encoding="utf-8")

    apply_files("```python name=impl.py\n新内容\n```", tmp_path)

    assert target.read_text(encoding="utf-8").strip() == "新内容"
    assert not list(tmp_path.glob("*.sesa-partial")), "temp files have to be cleaned up"
    assert original != target.read_text(encoding="utf-8")


def test_unicode_digits_do_not_slip_through_the_wizard():
    """`'²'.isdigit()` is True, while `int('²')` raises ValueError."""
    assert "²".isdigit() and not "²".isdecimal()
    wizard = Path("src/sesa/wizard.py").read_text(encoding="utf-8")
    assert ".isdigit()" not in wizard, "the wizard's numeric validation has to use isdecimal"


def test_an_old_record_with_an_empty_partial_can_still_be_resumed(tmp_path):
    """Once an invariant moves into `__post_init__`, **every construction path has to follow**.

    The extraction layer has always had the downgrade rule for an empty-payload partial; the resume
    recovery layer constructs StanceOn directly. Add the invariant and any record holding such
    historical data makes `resume` crash outright —
    **a correct check added, without checking every path into that type.**
    """
    from sesa import events as ev
    from sesa.record import Recorder, load_state, new_run_id
    from sesa.types import ParticipantSpec

    run_id = new_run_id()
    recorder = Recorder(tmp_path, run_id)
    recorder.emit(
        ev.RunStart(
            task="t", participants=["a", "b"], protocol="debate", max_rounds=2, run_id=run_id
        )
    )
    recorder.emit(
        ev.StanceEmit(
            round=1,
            participant="a",
            stance={
                "position": "p",
                "confidence": 0.9,
                "stance_on": {"b": "partial"},  # an old record: partial with no residuals
                "residuals": {},
                "reasons": {"b": "有保留"},
                "key_claims": [],
                "changed": False,
                "unknown": False,
            },
        )
    )
    recorder.close()

    state = load_state(
        tmp_path / "runs" / run_id,
        [ParticipantSpec(id=i, adapter="cli") for i in ("a", "b")],
        max_rounds=2,
    )

    restored = state.rounds[-1].stances["a"].stance_on["b"]
    assert restored.verdict == "unknown", (
        "an empty-payload partial is treated as unknown, matching the parsing layer"
    )
    assert restored.reason == "有保留", (
        "the reasons cannot be lost — losing them has the disagreements reinvented from scratch"
    )


def test_a_participant_who_was_truncated_every_round_is_named_up_front():
    """**The engine knew, and did not say.**

    Measured end to end: a participant wrote 36,271 characters over two rounds, `fences_seen`
    recorded 0, zero files landed, its stance card was adopted not once — and the word "truncated"
    appeared **0 times** in `RESULT.md`. What the reader saw was a normal deliverable, saved only
    by another participant mentioning in passing that "the code in their turn never landed in the
    files that were verified".

    Someone truncated in every round **has not really taken part**, and that has to come before the
    conclusion.
    """
    from sesa.report import render_result
    from sesa.types import Outcome, Result

    text = render_result(
        Result(
            run_id="r1",
            task="实现 satisfies()",
            outcome=Outcome.EXHAUSTED,
            truncated_turns={"deepseek": 2},
        )
    )

    assert "deepseek（2 轮）" in text
    assert "其实没有真正参与" in text
    assert "max_tokens" in text, "it has to give an actionable next step"
    assert text.index("截断") < text.index("## 结论"), "it has to come before the conclusion"


def test_a_clean_run_says_nothing_about_truncation():
    from sesa.report import render_result
    from sesa.types import Outcome, Result

    assert "截断" not in render_result(Result(run_id="r", task="t", outcome=Outcome.CONSENSUS))


def test_every_caveat_that_undermines_the_conclusion_comes_before_it():
    """**The reader has to know these things before reading the conclusion.**

    The "asymmetric material" block used to sit after the conclusion — written down, but in a place
    the reader reaches after already believing the conclusion. What these items have in common is
    that every one of them changes how you read it.
    """
    from sesa.report import render_result
    from sesa.types import Outcome, Result

    # **It has to hold in both languages.** The deliverable's language follows the task, so this
    # bottom line cannot be checked in one only — a Chinese task produces a Chinese deliverable, and
    # testing English alone leaves a broken ordering on the Chinese path unnoticed.
    for task, heading, caveats in (
        # in the fixture the last field of adoptions is True (evidence regressed), which takes the
        # "regression" branch rather than the "copying" one — pick the fragment that is actually
        # rendered.
        ("Pick a plan", "## Conclusion", ("cut off", "did not see the same", "step backwards")),
        ("请选一个方案", "## 结论", ("截断", "材料不对称", "退步")),
    ):
        text = render_result(
            Result(
                run_id="r1",
                task=task,
                outcome=Outcome.CONSENSUS_WITH_RESERVATIONS,
                conclusion="Plan A.",
                truncated_turns={"deepseek": 2},
                briefings={"kimi": 100},
                adoptions=[(1, "alice", "bob", "impl.py", 0.97, 0.08, True)],
            )
        )
        where = text.index(heading)
        for caveat in caveats:
            assert text.index(caveat) < where, f"'{caveat}' comes after the conclusion ({task})"
