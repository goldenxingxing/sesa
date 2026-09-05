"""The remaining high-severity items of the second full scan, those verified as real.

Of the 18 high/critical items, 10 had already been fixed, and [17] (parse_draft on a non-dict)
and [51] (protocols wrongly reporting "ignored") no longer hold against the current code on
review — a scan report is a snapshot of one moment, and **accepting it wholesale would "fix"
things long since fixed**.
The remaining 6 hold, and this file pins them down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sesa.state import EvidenceRecord
from sesa.workspace.ephemeral import EphemeralWorkspace

# ── [70] the temp directory leaks for good ──────────────────────────────────────── #


def test_second_cleanup_actually_removes_the_directory():
    ws = EphemeralWorkspace()
    ws.prepare(["a"])
    ws.cleanup()
    ws.prepare(["b"])
    second = ws.root
    ws.cleanup()
    assert not second.exists(), (
        "the second cleanup cannot remove it, and the temp directory leaks for good"
    )


# ── [79] a failed git diff must not silently weaken the fingerprint ─────────────── #


def test_fingerprint_returns_none_when_diff_is_unavailable(monkeypatch, tmp_path):
    """Falling back to hashing the status lines when the diff is unavailable weakens the
    fingerprint, exactly when it is most needed, into the form **already known to be
    insufficient** — and leaves no trace of it.
    """
    from sesa.workspace import worktree as wt

    calls = {"n": 0}

    def fake_git(args, cwd, **kw):
        calls["n"] += 1
        if args[:1] == ["diff"]:
            raise wt.GitError("diff 挂了")
        if args[:1] == ["rev-parse"]:
            return "abc123"
        if args[:1] == ["status"]:
            return " M semver.py"
        return ""

    monkeypatch.setattr(wt, "_git", fake_git)
    ws = wt.GitWorktreeWorkspace(repo=tmp_path, run_id="r1")
    checkout = wt.Checkout(participant="a", path=tmp_path)
    assert ws.revision_of(checkout) is None


# ── [60] the base class must not pretend to know the current revision ───────────── #


def test_base_workspace_reports_unknown_rather_than_the_prepare_time_value():
    """Returning the value captured at prepare makes every staleness check pass unconditionally —
    **a probe that always says "unchanged" is worse than no probe**.
    """
    from sesa.workspace.base import Checkout, Workspace

    class Bare(Workspace):
        def prepare(self, participants):
            return {}

    checkout = Checkout(participant="a", path=Path("/tmp"), revision="captured-at-prepare")
    assert Bare().revision_of(checkout) is None


# ── revision used to be written and never read ──────────────────────────────────── #


@pytest.mark.parametrize(
    "recorded,current,stale",
    [
        ("r1", "r2", True),  # the code changed
        ("r1", "r1", False),  # unchanged
        (None, "r2", False),  # the revision cannot be measured ⇒ it must not be claimed
        # stale
        ("r1", None, False),
    ],
)
def test_evidence_knows_whether_it_has_been_invalidated(recorded, current, stale):
    """Unknown on either side counts as "not stale": under-reporting beats over-reporting —
    over-reporting throws away valid evidence, which is scarce as it is.
    """
    item = EvidenceRecord("a", "pytest", 0, "ok", revision=recorded)
    assert item.is_stale(current) is stale


def test_stale_evidence_does_not_impose_a_duty_to_verify():
    """Demanding someone reproduce a result whose code has since changed leaves them either unable
    to (judged not to have verified, and downgraded) or lying that they did. Both outcomes are
    this rule's own doing.
    """
    from sesa.consensus.matrix import StanceMatrix
    from sesa.state import DeliberationState, RoundRecord, Turn
    from sesa.types import ParticipantSpec, Stance, StanceOn

    ids = ["a", "b"]
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=2
    )
    earlier = RoundRecord(0)
    earlier.turns = [Turn(p, 0, 0, "draft", "话") for p in ids]
    earlier.evidence = [
        EvidenceRecord(p, "pytest", 0, "ok", source="engine", stale=True) for p in ids
    ]
    state.rounds.append(earlier)

    later = RoundRecord(1)
    later.turns = [Turn(p, 1, 0, "revise", "话") for p in ids]
    later.stances = {
        p: Stance(participant=p, round=1, confidence=0.9, stance_on={q: StanceOn(verdict="agree")})
        for p, q in (("a", "b"), ("b", "a"))
    }
    state.rounds.append(later)

    assert StanceMatrix().assess(state).unverified_agreements == []


def test_the_gate_and_the_prompt_read_the_same_evidence():
    """The point of imposition and the point of announcement must rest on the same fact (DESIGN
    14.25.1).

    The previous version of this test only asserted that the word `stale` appeared in both
    sources. **Far too weak** — I later changed the assessment to accumulate and forgot the
    prompt side, so participants were told they need only verify the previous round while being
    judged over every round, and this test stayed green.

    It now compares **the result sets**: given the same state, both sides must name the same
    participants.
    """
    from unittest.mock import Mock

    from sesa.consensus.matrix import StanceMatrix
    from sesa.engine import Engine
    from sesa.state import DeliberationState, RoundRecord, Turn
    from sesa.types import ParticipantSpec

    ids = ["a", "b", "c"]
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id=i, adapter="cli") for i in ids], max_rounds=4
    )

    def _round(index, evidence):
        record = RoundRecord(index)
        record.turns = [Turn(p, index, 0, "draft", "话") for p in ids]
        record.evidence = list(evidence)
        return record

    # a produced evidence only in round 0; b in round 1; c handed in a self-report, which does not
    # count.
    state.rounds.append(_round(0, [EvidenceRecord("a", "pytest", 0, "ok", source="engine")]))
    state.rounds.append(
        _round(
            1,
            [
                EvidenceRecord("b", "pytest", 0, "ok", source="engine"),
                EvidenceRecord("c", "pytest", 0, "我跑过了", source="claimed"),
            ],
        )
    )
    state.rounds.append(_round(2, []))  # the round currently in progress

    engine = Engine.__new__(Engine)
    engine._checkouts = {}
    engine.workspace = Mock(revision_of=Mock(return_value=None))

    from_prompt = {e.participant for e in engine._fresh_evidence(state)}
    from_gate = StanceMatrix._verifiable(state)

    assert from_prompt == from_gate, (
        f"announced {from_prompt} and judged by {from_gate} — a participant would be downgraded by a rule they were never told"
    )
    assert from_gate == {"a", "b"}, (
        "the earlier round's evidence has held all along; a self-report does not count"
    )


# ── [16] the rapporteur listing one is not "they missed none" ───────────────────── #


def test_backfill_compares_which_pairs_were_written_not_how_many():
    """Missing four fifths is no different in kind from missing all of it.

    The test went through three versions: first "did they write any" (one entry and the whole
    thing was skipped), which I changed to "how many did they write", and that still does not stop
    a rapporteur listing one **irrelevant** disagreement to make up the number. Only a
    pair-by-pair check does.
    """
    import inspect

    from sesa.consensus import rapporteur

    # Look at the implementation body: ``reconcile`` itself is only a language-scope wrapper.
    source = inspect.getsource(rapporteur._reconcile)
    assert "_pair_is_covered" in source
    assert "len(listed) >= report.opposed" not in source


# ── [41] an existing backup may be the only surviving copy of the original ──────── #


def test_a_preexisting_backup_is_never_destroyed(tmp_path):
    from sesa.evidence.runner import EvidenceRunner

    victim = tmp_path / "tests.py.sesa-backup"
    victim.write_text("上一次跑崩前留下的、原件仅存的副本", encoding="utf-8")
    spare = EvidenceRunner._unused_name(victim)
    assert not spare.exists() and spare != victim


# ── [38] installation failing part-way must still restore ───────────────────────── #


def test_partial_install_is_still_restorable(tmp_path, monkeypatch):
    """If installation raises on the Nth path, the tests already copied in must still be
    restorable.

    It used to be `backups = self._install_tests(...)` **outside** the try — so an exception
    part-way left the caller with no restore manifest at all, polluting the other party's working
    copy for good, while "the scene must be restored afterwards" is the very premise of
    cross-testing.
    """
    import shutil

    from sesa.evidence.runner import EvidenceRunner

    source, target = tmp_path / "src", tmp_path / "dst"
    for base in (source, target):
        base.mkdir()
    for name in ("a_test.py", "b_test.py"):
        (source / name).write_text("测试内容", encoding="utf-8")
        (target / name).write_text(f"{name} 的原件", encoding="utf-8")

    real_copy = shutil.copy2

    def explode(src, dst, *a, **kw):
        if "b_test" in str(src):
            raise PermissionError("第二个路径上炸了")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", explode)

    backups: list = []
    with pytest.raises(PermissionError):
        EvidenceRunner._install_tests(source, target, ["a_test.py", "b_test.py"], backups)

    assert backups, (
        "after a failure part-way the backup manifest is empty — the files already copied in can never be restored"
    )
    EvidenceRunner._restore(backups)
    assert (target / "a_test.py").read_text(encoding="utf-8") == "a_test.py 的原件"
