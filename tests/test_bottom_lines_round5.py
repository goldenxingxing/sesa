"""Verification of the README's four bottom lines (round five).

**I did not write this file.** It comes from a Sesa deliberation. This round deliberately
rescanned the modules the earlier rounds had reviewed (engine / matrix / stance / adoption /
judge / evaluate), to answer "has this way of reviewing exhausted the same ground yet".

The answer is **no**: the assertions below failed against the code at the time, and were
verified one by one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.stance import find_json_blocks, parse_stance, strip_stance_block
from sesa.engine import Engine
from sesa.evaluate import RoundMetrics, RunMetrics
from sesa.judge import parse as judge_parse
from sesa.protocols import build as build_protocol
from sesa.record import Recorder, new_run_id
from sesa.workspace import GitWorktreeWorkspace

sys.path.insert(0, str(Path(__file__).parent))
from test_bottom_lines import _state
from test_engine import drive, participant

#: A turn in **the real format**: it quotes the other party's stance card from the previous
#: round (bare JSON) in its prose, and ends with its own stance card as the prompt requires
#: (a ```json fence).
QUOTING_TURN = """deepseek 上一轮的立场卡写的是 {"position": "必须上 A 方案",
"confidence": 0.95, "stance_on": {"claude": {"verdict": "agree", "reason": "认同"}}}，
我不同意这一条：它的前提在本项目里不成立。

（正文若干……）

```json
{
  "position": "我主张 B 方案",
  "confidence": 0.4,
  "stance_on": {"deepseek": {"verdict": "disagree", "reason": "证据不成立"}},
  "changed_from_last_round": false
}
```
"""


# ═══════════════════════════════════════════════════════════════════════════ # A. Root cause A:
# detection order taken for textual order
# ═══════════════════════════════════════════════════════════════════════════ #


def test_json_blocks_come_back_in_document_order():
    """``stance.py:66-85`` collects fences first and bare objects after, discarding the positions.

    ``_balanced_objects`` does return the offsets (``stance.py:26``), and they were used only in
    ``strip_stance_block`` to cut the text, never to sort. All three callers ``reversed()`` to
    take "the last one", and what they take is "the last one detected".
    """
    text = 'A 段：{"position": "先出现的裸对象"}\n\n```json\n{"position": "后出现的围栏"}\n```\n'
    positions = [o.get("position") for o in find_json_blocks(text)]
    assert positions == ["先出现的裸对象", "后出现的围栏"], (
        f"the order returned must be textual order, and it is detection order: {positions}"
    )


def test_a_quoted_stance_card_does_not_replace_the_authors_own():
    """Outlet 1 (``stance.py:135``): what the engine records is **the quoted card**.

    The speaker's own card is in the ```json fence at the end — exactly where the prompt requires
    it — and it loses to the bare JSON quoted in the prose.
    """
    stance = parse_stance(QUOTING_TURN, "claude", 1, ["deepseek"])
    assert stance is not None
    assert stance.position == "我主张 B 方案", (
        f"recorded as someone else's position: {stance.position!r}"
    )
    assert stance.confidence == 0.4, f"recorded as someone else's confidence: {stance.confidence}"


def test_an_explicit_disagree_is_not_downgraded_to_unmeasured():
    """The downstream consequence of outlet 1, landing squarely on **bottom line 2**.

    The quoted card's ``stance_on`` points at ``claude`` (the quoter themselves), and ``claude``
    is not in ``others``, so the whole ``stance_on`` is emptied by the "ignore hallucinated
    participants" rule ⇒ claude's **explicit disagree with deepseek becomes "the engine did not
    measure it"**.

    The README: "'someone objected' and 'the engine did not measure it' are accounted
    separately". This is that conflation in the other direction — recording an explicit objection
    as missing data — and with ``degraded=False``, so not even a T2 retry fires: silent
    throughout.
    """
    stance = parse_stance(QUOTING_TURN, "claude", 1, ["deepseek"])
    assert stance is not None
    report = StanceMatrix().assess(_state(["claude", "deepseek"], {"claude": stance}))
    assert report.matrix["claude"]["deepseek"] == "disagree", (
        f"an explicit objection is recorded as {report.matrix['claude']['deepseek']!r}"
    )
    assert report.opposed >= 1, "explicit opposition has to count as opposed, not as unmeasured"


def test_the_card_that_is_recorded_is_the_card_that_is_stripped():
    """Outlet 2 (``stance.py:196``): extraction and excision chose **different blocks**.

    ``strip_stance_block`` looks backwards by fence, in textual order, and cut the real card at
    the end; ``parse_stance`` recorded the one quoted in the prose. Wrong at both ends: the
    position is recorded wrongly, and a stance card is left in the prose that everyone else can
    see.
    """
    stance = parse_stance(QUOTING_TURN, "claude", 1, ["deepseek"])
    body = strip_stance_block(QUOTING_TURN)
    assert stance is not None
    assert stance.position not in body, "the card that was recorded has to be cut out of the prose"
    assert "必须上 A 方案" in body, (
        "**This one was adjusted by the author on review.** The original assertion required that no "
        "'\"position\"' remain in the prose at all — which would cut out the card a participant "
        "**quoted from someone else** as well, deleting their evidence. Quoting is legitimate "
        "discussion content and keeping it is right. The real contract is one thing only: the card "
        "that was recorded must be cut out, and both ends must select the same one."
    )


def test_the_judge_verdict_is_not_overwritten_by_the_json_it_quotes():
    """Outlet 3 (``judge.py:174``): JSON the judge quoted displaces the judge's own verdict.

    Unlike deepseek's account ("it needs the judge to really output several candidates"): the
    judge quoting the reviewed party's JSON **bare in its prose, once**, is enough — it need not
    emit two verdicts.
    """
    raw = (
        "我先复述参与者自己交上来的汇总：\n\n"
        '{"participants": {"a": {"verdict": "held", "first_position": "甲",'
        ' "final_position": "甲"}}}\n\n'
        "以上是他们的自述。我的判定如下：\n\n"
        "```json\n"
        '{"overall": "a 中途改口", "participants": {"a": {"verdict": "shifted",'
        ' "first_position": "甲", "final_position": "乙"}}}\n'
        "```\n"
    )
    report = judge_parse(raw, "转录", "run1", "judge")
    assert report.overall == "a 中途改口", (
        f"the judge's overall verdict was lost: {report.overall!r}"
    )
    assert [(v.participant, v.verdict) for v in report.verdicts] == [("a", "shifted")]


# ═══════════════════════════════════════════════════════════════════════════ # B. Root cause B: a
# failed write is neither refused nor reported
# ═══════════════════════════════════════════════════════════════════════════ #


def test_an_illegal_path_is_rejected_rather_than_raised(tmp_path):
    """``patch.py:92``'s docstring: "anything out of bounds or otherwise illegal is **rejected and
    reported honestly**".

    In fact ``patch.py:111-112``'s ``mkdir`` / ``write_text`` have no protection at all, and two
    **purely model-controlled** paths raise outright:

    * ``name=.``: the model omits the file name ⇒ ``IsADirectoryError``
    * ``name=<an existing file>/x.py``: the model treats an existing file as a directory ⇒
      ``FileExistsError``
    """
    from sesa import patch

    root = tmp_path / "wd"
    root.mkdir()
    (root / "semver.py").write_text("x = 1\n", encoding="utf-8")

    for fence, why in (
        ("```python name=.\nprint(1)\n```\n", "name=."),
        ("```python name=semver.py/inner.py\nprint(1)\n```\n", "父目录是已存在的文件"),
    ):
        try:
            result = patch.apply_files(fence, root)
        except Exception as exc:
            raise AssertionError(
                f"{why}：应当进 rejected 并如实报告，实际抛出 {type(exc).__name__}: {exc}"
            ) from None
        assert result.rejected, f"{why}: neither written in nor recorded in rejected"


async def test_a_crash_inside_a_turn_never_disappears_from_the_event_stream(tmp_path):
    """``engine.py:369`` + ``engine.py:857``: an exception escaping ``_run_move`` vanishes in
    silence.

    ``_run_move``'s ``try/except`` (engine.py:330) wraps only the ``adapter.stream`` loop;
    ``patch.apply_files`` is **outside** it. And ``_merge``'s ``pump`` has only ``try/finally`` —
    the exception ends the coroutine, ``finally`` emits the sentinel as usual, and
    ``gather(return_exceptions=True)`` swallows it.

    So the whole round looks exactly like "nothing needed changing this round": no
    ``files.applied``, no ``error``, not even a ``turn.end``. Which is precisely the state
    engine.py:371's own comment denies — "emit the event even when not one character landed".
    """
    from sesa import adapters
    from sesa.types import Done, TextDelta, Usage

    async def stream(self, prompt, **kw):
        yield TextDelta("```python name=.\nprint(1)\n```\n我这一轮改了实现。")
        yield Done(Usage.unknown())

    original = adapters.cli.CliAdapter.stream
    adapters.cli.CliAdapter.stream = stream
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
        (repo / "impl.py").write_text("VALUE = 0\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)

        specs = [participant(pid) for pid in ("alice", "bob")]
        for spec in specs:
            spec.options["apply_code_blocks"] = True

        engine = Engine(
            specs,
            build_protocol("debate"),
            recorder=Recorder(tmp_path / "runs", new_run_id()),
            workspace=GitWorktreeWorkspace(repo, "run1"),
            max_rounds=1,
        )
        events = await drive(engine, task="把 VALUE 改对")
    finally:
        adapters.cli.CliAdapter.stream = original

    kinds = [e.t for e in events]
    spoke = {e.participant for e in events if e.t == "turn.end" and not e.error}
    applied = {e.participant for e in events if e.t == "files.applied"}
    stanced = {e.participant for e in events if e.t == "stance.emit"}
    errors = [e for e in events if e.t == "error"]

    assert applied or errors, (
        "both participants handed in code fences, every write failed, and the event stream holds neither files.applied "
        f"nor error — indistinguishable from 'nothing needed changing this round': {kinds}"
    )
    assert spoke <= stanced or errors, (
        f"{sorted(spoke - stanced)} finished speaking with no stance-card event and no error at all: {kinds}"
    )


# ═══════════════════════════════════════════════════════════════════════════ # C. "What the debate
# changed": two metrics exported side by side use incompatible rulers
# ═══════════════════════════════════════════════════════════════════════════ #


def test_toward_agreement_and_verdict_transitions_use_the_same_ruler():
    """``evaluate.py:212`` vs ``evaluate.py:187``.

    ``real_transitions`` explicitly excludes ``unknown → a position`` ("that is speaking for the
    first time, not changing one's mind"), while ``toward_agreement`` rests on
    ``verdict_transitions`` and counts every first position as "moved towards agreement". The two
    are written side by side into the same summary at ``evaluate.py:591-592``.

    The construction: in round 1 both take a first position of partial (not a change of mind);
    in round 2 both fall back to disagree (the only real move, and it is **away** from
    agreement).
    The right answer is transitions=2, toward_agreement=0.
    """
    rounds = [
        RoundMetrics(index=0, matrix={"a": {"b": "unknown"}, "b": {"a": "unknown"}}),
        RoundMetrics(index=1, matrix={"a": {"b": "partial"}, "b": {"a": "partial"}}),
        RoundMetrics(index=2, matrix={"a": {"b": "disagree"}, "b": {"a": "disagree"}}),
    ]
    m = RunMetrics(run_id="x", task="t", protocol="debate", participants=["a", "b"], rounds=rounds)
    assert len(m.real_transitions) == 2
    assert m.toward_agreement == 0, (
        f"every real move was away from agreement and it reports {m.toward_agreement} moves towards agreement"
    )


# ═══════════════════════════════════════════════════════════════════════════ # D. Low severity: a
# stringified boolean (raised by deepseek, factually right, metrics only)
# ═══════════════════════════════════════════════════════════════════════════ #


def test_a_stringified_false_is_not_a_change():
    """``stance.py:176`` ``bool(obj.get("changed_from_last_round"))``.

    ``bool("false") is True``. The same error is repeated at ``record.py:241``
    (``bool(raw.get("changed"))``), catching replays of the archive too.

    The blast radius is **metrics only**: this field flows to
    ``evaluate.RoundMetrics.changed`` and ``total_changes`` alone, taking no part in the
    convergence assessment and none in default-deny.
    """
    text = (
        "```json\n"
        + json.dumps(
            {
                "position": "p",
                "confidence": 0.5,
                "stance_on": {"b": {"verdict": "agree", "reason": "r"}},
                "changed_from_last_round": "false",
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    stance = parse_stance(text, "a", 1, ["b"])
    assert stance is not None
    assert stance.changed_from_last_round is False, (
        "the string 'false' was recorded as 'changed position'"
    )
