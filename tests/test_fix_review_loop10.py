"""Reviewing the output of "this batch of fixes" (13 items written by claude, all verified as
real).

**I did not write this file.** It comes from a Sesa deliberation on the topic "this batch of
fixes: were they thorough? did they break anything?" In round 0 claude reported ten items,
R1–R6 and H1–H4; in round 1 it wrote them up as 13 failing tests; deepseek objected to
claude's characterisation and severity ordering in 5 places.

The author verified them one by one: **all 13 of claude's held**; of deepseek's 5 objections
the most substantial (F1) rested on a misreading of the code (`suspicious_testers` already
requires the self-test to pass), but its ordering — pushing R3 to "most severe" — really did
put that regression in front of the author's eyes first.

The three shapes caught this round are exactly the ones the topic asked for:
- **newly created**: the atomic write puts model-controlled content outside the working copy
- **broke something**: narrowing quotations by speaker chops up the participants' own prose
- **half-plugged**: the fingerprint's two outlets, and bare substring matching
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import warnings
from pathlib import Path

import pytest

from sesa.evidence.runner import CrossTestMatrix
from sesa.judge import _only_from, verify_quote
from sesa.patch import _FENCE, apply_files, render_workspace
from sesa.protocols import build
from sesa.record import load_state
from sesa.types import ParticipantSpec
from sesa.workspace.base import Checkout
from sesa.workspace.worktree import GitWorktreeWorkspace

# --------------------------------------------------------------------------- #
# 1. judge: the new "narrow quotations by speaker" chopped up the participants' own prose
# --------------------------------------------------------------------------- #


def test_a_speaker_block_must_not_end_at_the_speakers_own_subheading():
    """`_only_from` treats any `## ` in the prose as a block boundary.

    The transcript's block heading is `## r00_p0_alice_draft`, while a participant's turn itself
    writes `## Premises` / `## Conclusion` — which our prompt asks for. So alice's turn is cut off
    at her own first second-level heading and nothing after it counts as hers.

    The consequence is not "one check fewer": `final_position` is exactly the sentence most likely
    to appear after `## Conclusion`, so **an honest judge is mechanically ruled to have invented
    it** — while `verification_rate`'s docstring calls itself "the reading of the judge's own
    credibility".
    """
    transcript = (
        "# 议题\n\n要不要引入缓存层？\n\n"
        "## r00_p0_alice_draft\n\n我倾向于引入缓存。\n\n"
        "## 前提\n\n我假设读多写少。\n\n"
        "## 结论\n\n因此我主张先上一层只读缓存，再评估。\n\n"
        "## r00_p1_bob_draft\n\n我反对。\n"
    )
    assert "因此我主张先上一层只读缓存" in _only_from(transcript, "alice")
    assert verify_quote("因此我主张先上一层只读缓存，再评估", transcript, speaker="alice")


def test_a_shorter_participant_id_must_not_absorb_a_longer_ones_block():
    """`taking = speaker in line` is bare substring matching.

    judge.py's own `_fingerprint` docstring uses `claude` and `claude-conservative` as its
    example. So a judge putting claude-conservative's words in claude's mouth passes this check —
    while stopping exactly that is the whole reason it exists.
    """
    transcript = (
        "## r00_p0_claude_draft\n\n我认为应该用方案 A，理由是它更简单。\n\n"
        "## r00_p1_claude-conservative_draft\n\n我坚决反对方案 A，必须用方案 B 并且加锁。\n"
    )
    assert not verify_quote("我坚决反对方案 A，必须用方案 B 并且加锁", transcript, speaker="claude")


# --------------------------------------------------------------------------- #
# 2. patch: the atomic write's staging file landed outside the working copy
# --------------------------------------------------------------------------- #


def test_staging_file_must_not_be_written_outside_the_workspace():
    """`target.with_name(...)` is root's **sibling** when target equals root.

    `_is_safe` validated only target, never the staging path that actually gets written.
    Given `name=.` — patch.py:124's comment says this is model output seen in practice —
    model-controlled content is written outside the working copy. Before the change, that input
    produced one rejection and nothing else.
    """
    base = Path(tempfile.mkdtemp())
    root = base / "claude"
    root.mkdir()
    (base / "deepseek").mkdir()

    result = apply_files("```python name=.\nMODEL CONTROLLED CONTENT\n```\n", root)
    assert result.applied == []
    strays = [p.name for p in base.iterdir() if p.name not in ("claude", "deepseek")]
    assert strays == [], f"written outside the working copy: {strays}"


def test_failed_write_must_not_leave_staging_in_the_workspace():
    """On a failed write the `.sesa-partial` stays put and is read into the context by
    render_workspace next round.

    `_SKIP_SUFFIXES` does not include `.sesa-partial`, and `commit_all`'s `git add -A` commits it
    into the delivered branch as well.
    """
    root = Path(tempfile.mkdtemp())
    (root / "src").mkdir()
    (root / "src" / "real.py").write_text("ok\n")

    apply_files("```python name=src\nJUNK BODY\n```\n", root)
    leftovers = [str(p.relative_to(root)) for p in root.rglob("*.sesa-partial")]
    assert leftovers == [], f"leftover staging files: {leftovers}"
    assert "JUNK BODY" not in render_workspace(root)


@pytest.mark.parametrize("info", ["name=a.py", "path=a.py"])
def test_a_bare_name_attribute_fence_is_still_accepted(info):
    r"""`(?:^|\s)` shuts ```` ```name=a.py ```` out.

    The old regex accepted it. patch.py's module docstring says this layer exists because "it is
    exactly the **weaker models** most likely to show what debate is worth", and omitting the
    language tag is a typical weak-model deviation.
    """
    text = f"```{info}\nprint(1)\n```\n"
    assert [m.group("path") for m in _FENCE.finditer(text)] == ["a.py"]


def test_filename_is_still_not_mistaken_for_name():
    """This is the fix's **positive** goal and has to be kept — it stops the previous item being
    carelessly reverted.
    """
    text = "```python filename=e.py\nprint(5)\n```\n"
    assert list(_FENCE.finditer(text)) == []


# --------------------------------------------------------------------------- #
# 3. worktree: the fingerprint plugged half the door
# --------------------------------------------------------------------------- #


def _repo() -> Path:
    root = Path(tempfile.mkdtemp()) / "repo"
    root.mkdir()

    def g(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    g("init", "-q", ".")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (root / "a.txt").write_text("hi")
    g("add", "-A")
    g("commit", "-qm", "init")
    return root


def test_fingerprint_sees_edits_inside_an_untracked_directory():
    """Without -uall, `status --porcelain` folds an untracked directory into one line `?? dir/`.

    `_untracked_digest` gets a directory rather than files, `digest.update(b"")` is a no-op, and
    the fingerprint is identical whatever the files inside become — precisely the blind spot this
    fix claimed to have removed.
    """
    root = _repo()
    ws = GitWorktreeWorkspace(repo=root, run_id="r")
    ck = Checkout(participant="p", path=root, branch=None, revision=None)

    (root / "newdir").mkdir()
    (root / "newdir" / "f.py").write_text("v1\n")
    before = ws.revision_of(ck)
    (root / "newdir" / "f.py").write_text("COMPLETELY DIFFERENT\n" * 50)
    assert ws.revision_of(ck) != before


def test_fingerprint_sees_edits_to_a_quoted_untracked_path():
    r"""git C-escapes non-ASCII paths by default: `?? "\346\226\207..."`.

    `.strip('"')` does not unescape, the path lands somewhere that does not exist, and it falls
    back to hashing an empty string. This project's prose, examples and file names are largely in
    Chinese.
    """
    root = _repo()
    ws = GitWorktreeWorkspace(repo=root, run_id="r")
    ck = Checkout(participant="p", path=root, branch=None, revision=None)

    (root / "文件.md").write_text("v1\n")
    before = ws.revision_of(ck)
    (root / "文件.md").write_text("v2 different\n" * 50)
    assert ws.revision_of(ck) != before


# --------------------------------------------------------------------------- #
# 4. resume: the new invariant was upheld on only one restore path
# --------------------------------------------------------------------------- #


def test_resume_must_not_crash_on_an_out_of_range_confidence():
    """`Stance.__post_init__` gained a 0–1 check, and `_coerce_optional_float` does not clamp.

    The author wrote `_restore_stance_on` specifically for StanceOn's version of this problem
    ("a correct check added, without checking every path into that type"), and left the
    Stance.confidence invariant added in the same commit outside the door.
    The parsing layer uses `_coerce_confidence` (divide by 100 and clamp); the restore layer uses
    a bare float().
    """
    run_dir = Path(tempfile.mkdtemp())
    (run_dir / "turns").mkdir()
    events = [
        {"t": "run.start", "run_id": "x", "task": "T", "participants": ["a", "b"]},
        {
            "t": "stance.emit",
            "round": 0,
            "participant": "a",
            "stance": {"position": "p", "confidence": 85, "stance_on": {"b": "agree"}},
        },
    ]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
    )
    specs = [ParticipantSpec(id="a", adapter="cli"), ParticipantSpec(id="b", adapter="cli")]

    state = load_state(run_dir, specs, max_rounds=3)
    assert state.rounds[0].stances["a"].confidence == pytest.approx(0.85)


# --------------------------------------------------------------------------- #
# 5. the protocol-option warning: a false alarm on the most ordinary invocation
# --------------------------------------------------------------------------- #


def test_no_warning_for_an_option_the_cli_synthesised_itself():
    """`sesa run -f task.md` with the default debate protocol warns that `proposer` was ignored.

    But the user never wrote proposer — cli.py:218 itself swaps the default "rotate" for "input",
    and `_DEFAULTS` exempts only "rotate". The author's own comment says the point is to avoid
    "flooding a perfectly normal configuration … training the user to ignore warnings", and this
    path is exactly the one that slipped through.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build("debate", turn_taking="parallel", proposer="input")
    assert [str(w.message) for w in caught] == []


# --------------------------------------------------------------------------- #
# 6. suspicious_testers: with two participants both are named for certain
# --------------------------------------------------------------------------- #


def test_two_participants_are_not_both_suspicious_testers():
    """With N=2 there is one off-diagonal cell, and "only my own implementation passes" is equivalent
    to "the two implementations differ".

    The signal this batch newly wired up names them in RESULT.md:
    "**a's and b's tests pass only for their own implementations** … which may mean only that the
    tests and the implementation came out of the same misunderstanding".
    examples/self-review/sesa.yaml is exactly a two-participant configuration.
    """
    matrix = CrossTestMatrix(command="pytest -q", results={("a", "b"): 1, ("b", "a"): 1})
    assert matrix.suspicious_testers({("a", "a"): 0, ("b", "b"): 0}) == []


# --------------------------------------------------------------------------- #
# 7. the confidence bar: default-deny applies only when nobody filled it in
# --------------------------------------------------------------------------- #


def test_the_confidence_gate_treats_partial_reporting_like_no_reporting():
    """The `elif` covers only confidences_known == 0.

    Three people all leaving it blank → deadlock; **one** of them writing 0.9 → consensus with
    reservations. The other two still report nothing and ride on that one cell's bar.
    `min_confidence` takes the minimum over those who filled it in, so "lowest" does not deserve
    the name in this state.

    This assertion is a claim about **consistency**: given that the author chose to treat "nobody
    reported" as the bar not being met, the two who did not report in "only one reported" should
    be treated the same way. Either both block or both let through; an optional field must not
    flip the whole outcome.
    """
    from sesa.consensus.matrix import StanceMatrix
    from sesa.types import ConsensusReport

    def report(known: int, lowest: float) -> ConsensusReport:
        return ConsensusReport(
            round=2,
            matrix={},
            min_confidence=lowest,
            converged=False,
            stalled_rounds=2,
            confidences_known=known,
            # **It has to be set**: the bar's test is `confidences_known < expected_confidences`,
            # and leaving the default 0 makes both cases return the same result — the test goes
            # green having measured nothing. A passing test masquerading as evidence.
            expected_confidences=3,
            agreed=1,
            opposed=0,
            unmeasured=0,
            coverage=1.0,
            reservations=2,
            partials=2,
        )

    matrix = StanceMatrix()
    none_reported = matrix.decide_outcome(report(0, 0.0), rounds_left=0)
    one_of_three = matrix.decide_outcome(report(1, 0.9), rounds_left=0)
    assert none_reported == one_of_three


def test_the_confidence_gate_fixture_actually_discriminates():
    """**A passing test can also have measured nothing.**

    The previous version's fixture did not set `expected_confidences` (default 0), so the test
    `confidences_known < expected_confidences` was always false — "all three leave it blank" and
    "one writes 0.9" returned the same result, the test went green, and the author believed it
    proved something.

    This is another shape of "an empty value masquerading as data": masquerading as a passing test.
    So this explicitly checks that the three cases **differ from each other**, and goes red by
    itself if the fixture stops working.
    """
    from sesa.consensus.matrix import StanceMatrix
    from sesa.types import ConsensusReport, Outcome

    def report(known: int, lowest: float) -> ConsensusReport:
        return ConsensusReport(
            round=2,
            matrix={},
            min_confidence=lowest,
            converged=False,
            stalled_rounds=2,
            confidences_known=known,
            expected_confidences=3,
            agreed=1,
            coverage=1.0,
            reservations=2,
        )

    matrix = StanceMatrix()
    none_reported = matrix.decide_outcome(report(0, 0.0), rounds_left=0)
    one_reported = matrix.decide_outcome(report(1, 0.9), rounds_left=0)
    all_reported = matrix.decide_outcome(report(3, 0.9), rounds_left=0)

    assert none_reported == one_reported == Outcome.DEADLOCK, (
        "partial reporting has to be treated the same as no reporting at all"
    )
    assert all_reported == Outcome.CONSENSUS_WITH_RESERVATIONS
    assert all_reported != none_reported, (
        "if the three cases do not differ, this test has measured nothing"
    )
