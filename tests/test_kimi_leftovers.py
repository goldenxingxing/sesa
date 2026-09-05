"""The three I judged wrongly.

Re-running kimi's 15 findings verbatim during reconciliation, three that I had recorded as
"fixed / no longer applies" were still red. The common cause was **checking too shallowly**:
I tried only one shape, compared only counts, fixed only the adjacent case.
"""

from __future__ import annotations

from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.rapporteur import parse_draft, reconcile
from sesa.patch import extract_files
from sesa.state import DeliberationState, RoundRecord, Turn
from sesa.types import ParticipantSpec, Stance, StanceOn

# ── fences: I fixed "longer fences" and missed "nested fences" ──────────────────── #


def test_a_nested_fence_does_not_truncate_the_file():
    """A non-greedy regex stops at the first ``` inside the file content.

    The truncated content **may still be valid code**, so half a file is quietly written to disk
    as a whole one.
    """
    text = (
        "```python name=example.py\n"
        '"""\n'
        "Here is an example:\n"
        "```python\n"
        "pass\n"
        "```\n"
        '"""\n'
        "x = 1\n"
        "```\n"
    )
    body = extract_files(text).get("example.py", "")
    assert "x = 1" in body, f"the file was truncated to: {body!r}"
    assert "pass" in body, "the nested block's content has to stay in the file too"


def test_an_ordinary_file_still_ends_at_its_own_fence():
    """The fix must not let one block swallow the blocks after it."""
    text = "```py name=a.py\nA = 1\n```\n\n```py name=b.py\nB = 2\n```\n"
    files = extract_files(text)
    assert files["a.py"].strip() == "A = 1"
    assert files["b.py"].strip() == "B = 2"


def test_an_unclosed_fence_still_yields_what_was_written():
    """When cut off by the output budget, what was already written still has to be recoverable."""
    assert "A = 1" in extract_files("```py name=a.py\nA = 1\n").get("a.py", "")


# ── parse_draft: I only tried a string, never a list ────────────────────────────── #


def test_a_non_mapping_positions_field_does_not_crash():
    """`or {}` only catches None and empty values — a non-empty list travels all the way to
    .items() before it blows up, while this function's contract is "degrade if it cannot be
    parsed", not "kill the whole deliberation".
    """
    text = (
        "```json\n"
        '{"conclusion": "x", "disagreements": [{"topic": "t", "positions": ["a", "b"]}]}\n'
        "```"
    )
    got = parse_draft(text, ["a", "b"])
    assert got is not None
    assert got["conclusion"] == "x", (
        "the conclusion is valid content and must not be thrown away with it"
    )
    # **Keep the entry; only positions becomes empty.**
    # I once dropped such entries whole under "a partial with an empty payload is treated as
    # unknown", and that broke false-consensus detection: a rapporteur who reads the whole thing and
    # says "there is a substantive conflict here" is giving a real signal the structured stance
    # cards failed to reflect, even without a position per person. The hazard on the reconcile side
    # has its own answer (pair-by-pair coverage).
    assert len(got["disagreements"]) == 1
    assert got["disagreements"][0].positions == {}


def test_a_non_mapping_minority_field_does_not_crash():
    text = '```json\n{"conclusion": "x", "minority": ["a"]}\n```'
    assert parse_draft(text, ["a"])["minority"] == {}


# ── reconcile: I compared counts, and the problem was "they listed the wrong one" ── #


def _two_party_state(verdict_of_b: str = "agree") -> tuple[DeliberationState, object]:
    state = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=p, adapter="cli") for p in ("a", "b")],
        max_rounds=2,
    )
    record = RoundRecord(0)
    record.turns = [Turn(p, 0, 0, "draft", p.upper()) for p in ("a", "b")]
    record.stances = {
        "a": Stance(
            participant="a",
            round=0,
            stance_on={"b": StanceOn(verdict="disagree", reason="反对")},
        ),
        "b": Stance(participant="b", round=0, stance_on={"a": StanceOn(verdict=verdict_of_b)}),
    }
    state.rounds.append(record)
    return state, StanceMatrix().assess(state)


def _topics(draft: dict) -> list[str]:
    return [d.get("topic") if isinstance(d, dict) else d.topic for d in draft["disagreements"]]


def test_an_irrelevant_disagreement_does_not_satisfy_the_backfill():
    """A rapporteur can perfectly well list one **irrelevant** disagreement to make up the number:
    the counts match, and the pair that really is opposed in the matrix vanishes from the
    deliverable all the same.

    The earlier test was "did they write any", which I changed to "how many did they write" —
    neither stops this. Only a pair-by-pair check does.
    """
    state, report = _two_party_state()
    assert report.opposed == 1

    fixed = reconcile(
        {"conclusion": "", "disagreements": [{"topic": "other", "positions": {}}]},
        state,
        report,
    )
    topics = _topics(fixed)
    assert "a opposes b's claim" in topics, f"the real disagreement is still missing: {topics}"
    assert "other" in topics, (
        "the rapporteur's entry carries its reasons, and the backfill must not overwrite it"
    )
    assert "a↔b" in fixed["reconciled"], "it has to say which pair was missed, not only how many"


def test_a_disagreement_that_covers_the_pair_needs_no_backfill():
    """When the rapporteur really did cover that pair, no duplicate should be filled in
    mechanically.
    """
    state, report = _two_party_state()
    fixed = reconcile(
        {
            "conclusion": "",
            "disagreements": [{"topic": "真的写了", "positions": {"a": "甲", "b": "乙"}}],
        },
        state,
        report,
    )
    assert _topics(fixed) == ["真的写了"]
    assert "reconciled" not in fixed


def test_no_opposition_means_no_backfill():
    """With zero opposition the rapporteur must not be accused of omitting one — calling the
    unmeasured a disagreement is this project's second bottom line.
    """
    state, report = _two_party_state()
    state.rounds[0].stances["a"].stance_on["b"] = StanceOn(verdict="agree")
    report = StanceMatrix().assess(state)
    assert report.opposed == 0
    draft = {"conclusion": "", "disagreements": []}
    assert reconcile(draft, state, report) == draft


def test_reconcile_survives_either_shape_of_disagreement():
    """`reconcile` is the last check on the deliverable, and its crashing means the whole
    deliberation was for nothing.
    """
    state, report = _two_party_state()
    for listed in ([{"topic": "t", "positions": {"a": "x", "b": "y"}}], []):
        reconcile({"conclusion": "", "disagreements": listed}, state, report)


# ── found by a smoke test during reconciliation: unit tests all green, the CLI crashed ── #


def test_markup_escape_accepts_non_strings():
    """`rich.markup.escape` only accepts str, and feeding it a Path raises TypeError.

    The CLI wraps 20-odd external interpolations, several passing a Path/int/None, and most of
    them are on **error paths** — never reached on a normal run, so the crash happens only when
    the user is already in trouble.

    Measured: `sesa runs` crashed outright when `.sesa/runs` did not exist yet, the first command
    a new user runs. Not one of 593 unit tests caught it.
    """
    from pathlib import Path

    from sesa.cli import E

    assert E(Path(".sesa/runs")) == ".sesa/runs"
    assert E(None) == "None"
    assert E(3) == "3"
    assert E("[bold]不是标记[/bold]") == r"\[bold]不是标记\[/bold]"


def test_every_command_survives_an_empty_directory(tmp_path, monkeypatch):
    """A new user's first run: nothing in the directory at all."""
    from typer.testing import CliRunner

    from sesa.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    # List only those needing no network and no existing records. Exit code 2 = the command does not
    # exist, which also has to be caught — my first smoke script looked only for "Traceback" and
    # took "No such command" for a pass.
    for command in (["runs"], ["participants", "list"], ["version"], ["--help"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, f"{command} exited {result.exit_code}: {result.output[:200]}"
        assert "Traceback" not in (result.output or ""), f"{command} crashed"
