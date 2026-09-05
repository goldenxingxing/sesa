"""Detecting a rival's code lifted wholesale.

Measured background: DeepSeek ×2 × semver × 24 runs. Three cells in the debate group
crossed the 0.5 line on an implementation file, and all three lost points (34→33, 34→23,
34→23); of the 13 cells that did not cross, only 1 lost points. In the reflect group
(nobody sees anybody) the metric peaked at 0.16.
"""

from __future__ import annotations

from pathlib import Path

from sesa.evaluate import ADOPTION_THRESHOLD, code_adoption

ALICE_V1 = (
    "def satisfies(v, r):\n    return expand(r)(v)\n\n\ndef expand(r):\n    return lambda v: True\n"
)
BOB_V1 = "import re\n\n\ndef satisfies(version, spec):\n    m = re.match(r'x', spec)\n    return bool(m)\n"


def _run(tmp_path: Path, turns: dict[str, str]) -> Path:
    run = tmp_path / "run"
    (run / "turns").mkdir(parents=True)
    for name, body in turns.items():
        (run / "turns" / name).write_text(body, encoding="utf-8")
    return run


def _md(code: str) -> str:
    return f"我的实现：\n\n```python name=semver.py\n{code}```\n"


def test_detects_a_participant_abandoning_its_own_draft_for_the_peers(tmp_path):
    run = _run(
        tmp_path,
        {
            "r00_p0_alice_draft.md": _md(ALICE_V1),
            "r00_p0_bob_draft.md": _md(BOB_V1),
            # what alice handed in for round 1 is bob's draft
            "r01_p0_alice_revise.md": _md(BOB_V1),
            "r01_p0_bob_revise.md": _md(BOB_V1 + "# 又补了一行\n"),
        },
    )
    report = code_adoption(run)
    assert report.measurable
    assert len(report.events) == 1
    got = report.events[0]
    assert (got.participant, got.adopted_from, got.round) == ("alice", "bob", 1)
    assert got.similarity_to_peer > ADOPTION_THRESHOLD > got.similarity_to_own


def test_revising_ones_own_draft_is_not_adoption(tmp_path):
    run = _run(
        tmp_path,
        {
            "r00_p0_alice_draft.md": _md(ALICE_V1),
            "r00_p0_bob_draft.md": _md(BOB_V1),
            "r01_p0_alice_revise.md": _md(ALICE_V1 + "# 修了个边界\n"),
            "r01_p0_bob_revise.md": _md(BOB_V1 + "# 修了个边界\n"),
        },
    )
    assert code_adoption(run).events == []


def test_having_nothing_of_ones_own_is_not_adoption(tmp_path):
    """Having nothing of your own and taking the other's is different in kind from throwing your
    own work away to copy.

    Measured, the former happened 4 times and the latter 3, and only the latter came with a drop
    in score.
    """
    run = _run(
        tmp_path,
        {
            "r00_p0_alice_draft.md": "我这轮只讨论，不交代码。\n",
            "r00_p0_bob_draft.md": _md(BOB_V1),
            "r01_p0_alice_revise.md": _md(BOB_V1),
            "r01_p0_bob_revise.md": _md(BOB_V1),
        },
    )
    assert code_adoption(run).events == []


def test_unmeasurable_is_not_the_same_as_nothing_found(tmp_path):
    """ "Could not measure" and "measured and found nothing" look identical — they have to be
    tellable apart.
    """
    run = _run(
        tmp_path,
        {
            "r00_p0_alice_draft.md": "我是 agent CLI，自己写文件，正文里没有代码块。\n",
            "r01_p0_alice_revise.md": "同上。\n",
        },
    )
    report = code_adoption(run)
    assert not report.measurable
    assert report.reason
    assert report.events == []
    assert not report
