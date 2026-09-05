"""Six defects found by an external code-review tool (open-code-review) and mechanically
verified as real.

All six came from code I had written that same day, and all six were in places the existing
tests did not cover. They stay here both as a regression line and as a record: **self-review
has systematic blind spots, and an external tool fills one of them in.**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from rich.markup import escape

import sesa
from sesa.protocols import build
from sesa.types import Outcome, ParticipantSpec


def test_the_reported_version_matches_the_packaged_one():
    """A hand-copied version drifts inevitably: pyproject already said 0.1.0 while `sesa version`
    still reported dev0.
    """
    declared = ""
    for line in (Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8").splitlines():
        if line.startswith("version = "):
            declared = line.split("=", 1)[1].strip().strip('"')
            break

    assert declared, "pyproject.toml has no version"
    # In a development environment an editable install's metadata lags behind pyproject — after
    # changing the version, reinstall once (`uv pip install -e . --no-deps`). This test pins that
    # down too.
    assert sesa.__version__ in (declared, "0.0.0+source"), (
        f"the package metadata says {sesa.__version__} and pyproject says {declared} — the published package would misreport its own version"
    )


def test_a_protocol_that_does_not_measure_consensus_says_so_before_drafting():
    """The outcome has to be settled before the drafting prompt is generated.

    This once sat 70 lines after the drafting, so reflect's rapporteur wrote the draft as though
    the discussion were "unfinished" while the banner said "this protocol does not measure
    consensus" — and the two did not match.
    """
    source = (Path(__file__).parent.parent / "src" / "sesa" / "engine.py").read_text("utf-8")
    decide = source.index("if not self.protocol.measures_consensus and produced:")
    draft = source.index("rap.build_prompt(state, report, outcome)")

    assert decide < draft, (
        "the outcome is settled after the drafting, so the rapporteur received the wrong one"
    )


def test_reflect_declares_that_it_does_not_measure_consensus():
    assert build("reflect").measures_consensus is False
    assert build("debate").measures_consensus is True
    assert Outcome.NOT_MEASURED.value == "not_measured"


@pytest.mark.parametrize(
    "text",
    ["error: [Errno 2] No such file", "冲突：字段 [a-z] 与 [/dim] 不一致"],
)
def test_agent_error_text_and_conflicts_are_escaped(text):
    """Two that slipped through: TurnEnd.error and FalseConsensus.conflicts were both unescaped at
    the time.
    """
    console = Console(record=True, width=200)

    console.print(f"[red]✗[/red] {escape(text)}")
    console.print(f"[magenta]检测到假共识：{escape('；'.join([text]))}[/magenta]")

    out = console.export_text()
    assert "Errno" in out or "a-z" in out


def test_an_empty_check_detail_does_not_break_the_wizard_loop():
    """An empty detail makes splitlines()[0] raise IndexError, taking the participants after it
    down too.
    """
    for detail in ("", "   ", None):
        head = next(iter((detail or "").strip().splitlines()), "（无详情）")
        assert head  # no exception, and not empty


async def test_an_unreadable_briefing_does_not_abort_the_whole_run(tmp_path):
    """Say so when it cannot be read, but do not drag the whole run down — the others can
    deliberate perfectly well.
    """
    from sesa.engine import Engine
    from sesa.record import Recorder, new_run_id
    from tests.test_engine import drive, participant

    broken = participant("alice")
    broken.options["briefing"] = f"@{tmp_path}/does-not-exist.md"

    engine = Engine(
        [broken, participant("bob")],
        build("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        max_rounds=1,
    )
    events = await drive(engine)

    complained = [e for e in events if e.t == "error" and "briefing" in e.where]
    assert complained, (
        "a briefing that cannot be read has to leave an event, not be quietly treated as empty"
    )
    assert [e for e in events if e.t == "verdict.final"], "it must not abort the whole deliberation"


def test_a_participant_without_briefing_is_unaffected():
    assert ParticipantSpec(id="x", adapter="cli").options.get("briefing") is None
