"""How copy detection behaves inside the engine.

The risk in a debate is not failing to converge, it is **converging on the wrong side**.
Measured, one participant swapped its own 34/34 implementation for a rival's 23/34
(similarity 0.97), and what came out was a perfectly normal-looking consensus — see
DESIGN.md 14.18.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sesa import adoption as adopt
from sesa.engine import Engine
from sesa.evidence import EvidenceRunner
from sesa.protocols import build as build_protocol
from sesa.record import Recorder, new_run_id
from sesa.report import render_result
from sesa.types import Outcome, ParticipantSpec, Result
from sesa.workspace import GitWorktreeWorkspace
from tests.test_engine import drive, final, participant

GOOD = (
    "def satisfies(v, r):\n    return expand(r)(v)\n\n\ndef expand(r):\n    return lambda v: True\n"
)
BAD = "import re\n\n\ndef satisfies(version, spec):\n    return bool(re.match('x', spec))\n"


def _snap(**files: str) -> dict[str, str]:
    return dict(files)


def test_detects_abandoning_ones_own_work_for_the_peers():
    previous = {"alice": _snap(**{"semver.py": GOOD}), "bob": _snap(**{"semver.py": BAD})}
    current = {"alice": _snap(**{"semver.py": BAD}), "bob": _snap(**{"semver.py": BAD})}

    found = adopt.detect(previous, current, round_index=1)

    assert len(found) == 1
    assert (found[0].participant, found[0].adopted_from) == ("alice", "bob")
    assert found[0].similarity_to_peer > adopt.THRESHOLD > found[0].similarity_to_own


def test_an_untouched_file_is_not_adoption():
    """When two people wrote the same thing to begin with, neither copied the other."""
    same = {"alice": _snap(**{"semver.py": GOOD}), "bob": _snap(**{"semver.py": GOOD})}
    assert adopt.detect(same, same, round_index=1) == []


def test_having_nothing_of_ones_own_is_not_adoption():
    """Having nothing of your own and taking the other's is different in kind from throwing your own
    work away to copy.
    """
    previous = {"alice": {}, "bob": _snap(**{"semver.py": BAD})}
    current = {"alice": _snap(**{"semver.py": BAD}), "bob": _snap(**{"semver.py": BAD})}
    assert adopt.detect(previous, current, round_index=1) == []


def test_snapshot_skips_the_repository_internals_and_binaries(tmp_path):
    (tmp_path / "semver.py").write_text(GOOD, encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00")

    files = adopt.snapshot(tmp_path)

    assert set(files) == {"semver.py"}


# --------------------------------------------------------------------------- # Into the deliverable
# --------------------------------------------------------------------------- #


def _result(outcome: Outcome, adoptions: list) -> Result:
    return Result(
        run_id="r1",
        task="实现 satisfies()",
        outcome=outcome,
        conclusion="采用 bob 的实现。",
        adoptions=adoptions,
        branches={"alice": "sesa/r1/alice", "bob": "sesa/r1/bob"},
    )


def test_a_regressive_adoption_is_flagged_above_the_conclusion():
    """The reader has to know **before** reading the conclusion that it was bought with a
    regression.
    """
    result = _result(
        Outcome.CONSENSUS_WITH_RESERVATIONS,
        [(1, "alice", "bob", "semver.py", 0.97, 0.08, True)],
    )
    text = render_result(result)

    assert "这份结论建立在一次退步之上" in text
    assert text.index("退步") < text.index("## 结论"), (
        "the warning has to come before the conclusion"
    )
    assert "sesa/r1/alice" in text, "say which branch still holds the abandoned implementation"


def test_adoption_without_evidence_regression_is_stated_not_judged():
    """Without execution evidence of things getting worse, state the fact only — similarity does not
    answer good or bad.
    """
    result = _result(Outcome.CONSENSUS, [(1, "alice", "bob", "semver.py", 0.83, 0.12, False)])
    text = render_result(result)

    assert "整段照搬对手成果" in text
    assert "这份结论建立在一次退步之上" not in text
    assert "也可能是被说服了" in text


def test_a_clean_run_says_nothing_about_adoption():
    text = render_result(_result(Outcome.CONSENSUS, []))
    assert "照搬" not in text


# --------------------------------------------------------------------------- # End to end: real
# subprocesses + a real git repository
# --------------------------------------------------------------------------- #


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
    (repo / "impl.py").write_text("VALUE = 0\n", encoding="utf-8")
    (repo / "test_impl.py").write_text(
        "from impl import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _coder(pid: str, first: str, second: str) -> ParticipantSpec:
    """A fake participant that writes impl.py into the working directory each round."""
    spec = participant(pid)
    spec.options["env"]["FAKE_WRITE"] = f"impl.py={first}|impl.py={second}"
    return spec


async def test_a_consensus_bought_with_a_regression_is_not_reported_as_full_consensus(tmp_path):
    """alice is right in round 0 and switches to bob's wrong version in round 1, with self-tests
    going from passing to failing.

    The parties really did agree, but delivering that as full consensus is papering over.
    """
    good, bad = r"VALUE = 1\n# alice 的推导\n", r"VALUE = 2\n# bob 的推导\n"
    engine = Engine(
        [_coder("alice", good, bad), _coder("bob", bad, bad)],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        workspace=GitWorktreeWorkspace(_repo(tmp_path), "run1"),
        evidence=EvidenceRunner(f"{sys.executable} -m pytest -q", test_paths=["test_impl.py"]),
        max_rounds=2,
    )
    events = await drive(engine, task="把 VALUE 改对")

    hits = [e for e in events if e.t == "adoption"]
    assert hits, "no copying detected"
    got = hits[0]
    assert (got.participant, got.adopted_from) == ("alice", "bob")
    assert got.evidence_before == 0 and got.evidence_after != 0
    assert got.evidence_regressed

    verdict = final(events)
    assert verdict.outcome == Outcome.CONSENSUS_WITH_RESERVATIONS.value, (
        "they agreed, and in the wrong direction; this cannot be reported as full consensus"
    )


async def test_everyone_keeping_their_own_work_raises_no_adoption(tmp_path):
    good = r"VALUE = 1\n"
    engine = Engine(
        [_coder("alice", good, good + r"# 补个注释\n"), _coder("bob", good, good)],
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        workspace=GitWorktreeWorkspace(_repo(tmp_path), "run1"),
        evidence=EvidenceRunner(f"{sys.executable} -m pytest -q", test_paths=["test_impl.py"]),
        max_rounds=2,
    )
    events = await drive(engine, task="把 VALUE 改对")

    assert [e for e in events if e.t == "adoption"] == []
    assert final(events).outcome == Outcome.CONSENSUS.value


async def test_a_truncated_turn_keeps_its_code_but_cannot_vote(tmp_path, monkeypatch):
    """A truncated turn: the code still lands, the position is not adopted.

    Both directions were got wrong: first "accept silently", letting half a sentence pass for a
    complete turn; then "discard the whole thing", which threw away usable code from 4 turns in
    one experiment, one of which had already written 109 of 118 tests. The right thing is to
    separate the two.
    """
    from sesa import adapters
    from sesa.types import Done, TextDelta, Usage

    async def truncated_stream(self, prompt, **kw):
        yield TextDelta("```python name=impl.py\nVALUE = 1\n```\n我还想再说一点，但是")
        yield Done(Usage.unknown(), truncated=True)

    monkeypatch.setattr(adapters.cli.CliAdapter, "stream", truncated_stream)

    specs = [participant(pid) for pid in ("alice", "bob")]
    for spec in specs:
        spec.options["apply_code_blocks"] = True

    engine = Engine(
        specs,
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        workspace=GitWorktreeWorkspace(_repo(tmp_path), "run1"),
        max_rounds=1,
    )
    events = await drive(engine, task="把 VALUE 改对")

    applied = [e for e in events if e.t == "files.applied" and e.files]
    assert applied, "code finished before the truncation has to land on disk"

    ends = [e for e in events if e.t == "turn.end"]
    assert all(e.truncated for e in ends), "the truncation has to be recorded in the event stream"
    assert not [e for e in events if e.t == "stance.emit"], "half a sentence is not a position"
    assert final(events).outcome != Outcome.CONSENSUS.value, (
        "nobody took a valid position, so consensus must not be declared"
    )


async def test_a_timed_out_turn_still_lands_the_code_it_finished(tmp_path, monkeypatch):
    """A failed turn may still have produced something — do not throw the baby out with it.

    Twice measured as "the outcome says failure while the working copy actually holds
    something", and both were found by chance: once claude had finished writing the test file
    before hitting its quota, once kimi had output before timing out.
    `extract_files` only accepts closed fences, a half-written one cannot be extracted anyway,
    and writing them out is safe.
    """
    from sesa import adapters
    from sesa.adapters.base import AdapterError
    from sesa.types import TextDelta

    async def dies_midway(self, prompt, **kw):
        yield TextDelta("先交一个写完的文件：\n\n```python name=impl.py\nVALUE = 1\n```\n")
        yield TextDelta("我还想再写一个 ```python name=half.py\nVALUE = ")
        raise AdapterError("x: 已 1s 没有任何输出，判定为卡死并终止")

    monkeypatch.setattr(adapters.cli.CliAdapter, "stream", dies_midway)

    specs = [participant(pid) for pid in ("alice", "bob")]
    for spec in specs:
        spec.options["apply_code_blocks"] = True

    engine = Engine(
        specs,
        build_protocol("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        workspace=GitWorktreeWorkspace(_repo(tmp_path), "run1"),
        max_rounds=1,
    )
    events = await drive(engine, task="改点东西")

    applied = [e for e in events if e.t == "files.applied" and e.files]
    assert applied, "files finished before the timeout have to land on disk"
    assert all("half.py" not in f for e in applied for f in e.files), (
        "a half-written fence cannot be extracted and must not land either"
    )
    assert any(e.t == "turn.end" and e.error for e in events), (
        "but the round is still recorded as failed"
    )
