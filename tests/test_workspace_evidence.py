"""The two foundations of a code task: worktree isolation and execution evidence.

These two are the key to lifting the fundamental limit on the whole evaluation — **a text
topic has no ground truth, while whether a test passes is objective**.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sesa.evidence import CrossTestMatrix, EvidenceRunner, run_verify
from sesa.workspace import EphemeralWorkspace, GitError, GitWorktreeWorkspace


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
    (repo / "impl.py").write_text("def double(x):\n    raise NotImplementedError\n", "utf-8")
    (repo / "test_impl.py").write_text("def test_placeholder():\n    assert True\n", "utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)
    return repo


# --------------------------------------------------------------------------- # Isolation
# --------------------------------------------------------------------------- #


def test_each_participant_gets_an_isolated_checkout(tmp_path):
    ws = GitWorktreeWorkspace(make_repo(tmp_path), "run1")
    checkouts = ws.prepare(["alice", "bob"])
    try:
        (checkouts["alice"].path / "impl.py").write_text("alice 改的", "utf-8")
        assert "alice 改的" not in (checkouts["bob"].path / "impl.py").read_text("utf-8")
    finally:
        ws.cleanup()


def test_dirty_repo_is_refused(tmp_path):
    """The participants really do write files and run commands, usually with auto-approval on — the
    user's unsaved work has to be banked first. This is not fastidious, it is an irreversible
    risk.
    """
    repo = make_repo(tmp_path)
    (repo / "unsaved.txt").write_text("未提交的工作", "utf-8")
    with pytest.raises(GitError, match="uncommitted changes"):
        GitWorktreeWorkspace(repo, "run1").prepare(["alice"])


def test_non_git_directory_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError, match="is not a git repository"):
        GitWorktreeWorkspace(plain, "run1").prepare(["alice"])


def test_branches_survive_cleanup(tmp_path):
    """The branch is a deliverable: an implementation that was not adopted carries the minority
    opinion, and deleting it erases the disagreement.
    """
    repo = make_repo(tmp_path)
    ws = GitWorktreeWorkspace(repo, "run1")
    ws.prepare(["alice", "bob"])
    ws.cleanup()
    branches = subprocess.run(
        ["git", "branch", "--list", "sesa/*"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "sesa/run1/alice" in branches
    assert "sesa/run1/bob" in branches


def test_revision_changes_with_uncommitted_edits(tmp_path):
    """Participants often edit files without committing, and HEAD alone would say the revision is
    unchanged and the old evidence still valid.
    """
    ws = GitWorktreeWorkspace(make_repo(tmp_path), "run1")
    checkouts = ws.prepare(["alice"])
    try:
        before = ws.revision_of(checkouts["alice"])
        (checkouts["alice"].path / "impl.py").write_text("改了但没提交", "utf-8")
        assert ws.revision_of(checkouts["alice"]) != before
    finally:
        ws.cleanup()


def test_ephemeral_workspace_touches_no_repo(tmp_path):
    ws = EphemeralWorkspace()
    checkouts = ws.prepare(["a", "b"])
    try:
        assert checkouts["a"].path != checkouts["b"].path
        assert checkouts["a"].branch is None
    finally:
        ws.cleanup()
    assert not checkouts["a"].path.exists()  # temp directories we made ourselves have to be
    # cleaned up


# --------------------------------------------------------------------------- # Evidence
# --------------------------------------------------------------------------- #


def test_verify_captures_exit_code_and_tail(tmp_path):
    """The important part of test output is at the end (the failure summary, the statistics line),
    so keep the tail and drop the head.
    """
    code, summary = run_verify("echo 开头 && echo 结尾 && exit 3", tmp_path)
    assert code == 3
    assert "结尾" in summary


def test_verify_timeout_is_reported_not_hidden(tmp_path):
    code, summary = run_verify("sleep 5", tmp_path, timeout=0.5)
    assert code == 124
    assert "did not finish" in summary


def test_cross_testing_exposes_an_implementation_that_only_passes_its_own_tests(tmp_path):
    """All-green self-tests cannot tell good from bad — running someone else's tests can.

    This is the core defence against "testing only yourself" in a code task: when whoever writes
    the implementation also writes the tests, a green light may be nothing but assert True, or
    assertions that happen to match their own bug.
    """
    ws = GitWorktreeWorkspace(make_repo(tmp_path), "run1")
    checkouts = ws.prepare(["good", "narrow"])
    try:
        (checkouts["good"].path / "impl.py").write_text(
            "def double(x):\n    return x * 2\n", "utf-8"
        )
        (checkouts["good"].path / "test_impl.py").write_text(
            "from impl import double\n\ndef test_negative():\n    assert double(-2) == -4\n",
            "utf-8",
        )
        # correct for positive numbers only, and the tests cover only positive numbers — a private
        # assumption encoded into the tests
        (checkouts["narrow"].path / "impl.py").write_text(
            "def double(x):\n    return x * 2 if x > 0 else 0\n", "utf-8"
        )
        (checkouts["narrow"].path / "test_impl.py").write_text(
            "from impl import double\n\ndef test_positive():\n    assert double(3) == 6\n", "utf-8"
        )

        revisions = {p: ws.revision_of(c) for p, c in checkouts.items()}
        runner = EvidenceRunner(f"{sys.executable} -m pytest -q", timeout=120)

        for record in runner.self_test(checkouts, revisions):
            assert record.passed  # the self-test phase cannot tell the two apart
            assert record.is_self_test  # which is exactly why it is the weakest form of
            # evidence

        matrix, records = runner.cross_test(checkouts, revisions, ["test_impl.py"])
        assert matrix.passed("narrow", "good") is True  # good survives narrow's tests
        assert matrix.passed("good", "narrow") is False  # narrow does not survive good's
        assert matrix.universally_passing() == ["good"]
        assert all(r.source == "engine" for r in records)  # only what the engine executed
        # itself counts as evidence
        assert all(not r.is_self_test for r in records)

        # the scene has to be restored, or the next round runs code the participant did not write
        assert "x > 0 else 0" in (checkouts["narrow"].path / "impl.py").read_text("utf-8")
        assert "test_positive" in (checkouts["narrow"].path / "test_impl.py").read_text("utf-8")
        assert not list(checkouts["narrow"].path.glob("*.sesa-backup"))
    finally:
        ws.cleanup()


def test_matrix_flags_tests_that_only_their_author_passes():
    matrix = CrossTestMatrix(command="x")
    matrix.results = {("a", "a"): 0, ("a", "b"): 1, ("b", "a"): 0, ("b", "b"): 0}
    assert matrix.only_own_tests_pass("a") is True
    assert matrix.only_own_tests_pass("b") is False


def test_participants_with_no_successful_turn_are_excluded_from_cross_testing(tmp_path):
    """A participant with no successful turn still has the original code in their working copy.

    Counting them into the cross-test passes the baseline stub off as "their implementation" and
    "their tests" — measured, a participant who never spoke successfully had its stub test
    (assert True) hand somebody else the hardest conclusion available, "passes everyone's tests",
    turning the conclusion completely on its head.
    """
    import asyncio

    from sesa.engine import Engine
    from sesa.protocols import build as build_protocol
    from sesa.state import RoundRecord, Turn
    from sesa.types import ParticipantSpec

    repo = make_repo(tmp_path)
    specs = [
        ParticipantSpec(id=pid, adapter="cli", options={"command": ["true"]})
        for pid in ("worked", "failed")
    ]
    engine = Engine(
        specs,
        build_protocol("debate"),
        workspace=GitWorktreeWorkspace(repo, "run1"),
        evidence=EvidenceRunner(f"{sys.executable} -m pytest -q", test_paths=["test_impl.py"]),
    )
    engine._checkouts = engine.workspace.prepare(["worked", "failed"])
    try:
        state = type("S", (), {"rounds": [], "round_index": 1, "current": None})()
        record = RoundRecord(0)
        record.turns = [
            Turn("worked", 0, 0, "draft", "写了东西"),
            Turn("failed", 0, 0, "draft", "", error="AdapterError: 命令不存在"),
        ]
        state.rounds = [record]
        state.current = record

        events = asyncio.run(_drain(engine._cross_test(state)))
        messages = [e.message for e in events if e.t == "error"]
        assert any("failed" in m and "left out of the cross-test" in m for m in messages)
        assert any("Fewer than 2 usable participants" in m for m in messages)
        assert engine._cross_matrix is None  # too few usable participants, so no matrix is
        # produced
    finally:
        engine.workspace.cleanup()


async def _collect(agen):
    return [item async for item in agen]


def _drain(agen):
    return _collect(agen)
