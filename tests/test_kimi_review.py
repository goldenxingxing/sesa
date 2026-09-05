"""The defects kimi found in a whole-project review — every one the same illness: an empty value
masquerading as data.

The value of this batch is not the number but that they **cluster in one category**:
``coverage`` defaulting to 1.0, ``Usage.merge`` keeping partial sums when half is unknown,
``Budget.reset`` not clearing the bill, a resume losing the truncation flag — four different
pieces of code, all filling the "not measured" slot with a number that looks normal.
"""

import pytest

from sesa import config as config_mod
from sesa.budget import Budget
from sesa.config import ConfigError
from sesa.consensus.matrix import StanceMatrix
from sesa.consensus.stance import parse_stance
from sesa.state import DeliberationState, RoundRecord, Turn
from sesa.types import ConsensusReport, ParticipantSpec, Usage


def test_merge_with_unknown_yields_unknown():
    """With one side unknown, the merged value is not a total — partial sums must not be kept and
    passed off as totals.
    """
    merged = Usage(input_tokens=10, output_tokens=5, usd=0.1, known=True).merge(Usage.unknown())
    assert not merged.known
    assert merged.input_tokens is None and merged.usd is None


def test_merge_of_known_still_adds():
    """The fix must not disable the normal path along with it."""
    merged = Usage(input_tokens=10, output_tokens=5, usd=0.1).merge(
        Usage(input_tokens=1, output_tokens=2, usd=0.9)
    )
    assert merged.known and merged.input_tokens == 11 and merged.usd == pytest.approx(1.0)


def test_budget_reset_clears_spending():
    b = Budget()
    b.spent_usd, b.spent_tokens, b.unknown_calls = 5.0, 100, 3
    b.reset()
    assert (b.spent_usd, b.spent_tokens, b.unknown_calls) == (0.0, 0, 0)


def test_coverage_is_zero_when_nothing_was_measured():
    """Not one cell ⇒ not one peer assessment happened ⇒ coverage is 0, not 1."""
    state = DeliberationState(
        task="t", participants=[ParticipantSpec(id="solo", adapter="cli")], max_rounds=1
    )
    record = RoundRecord(0)
    record.turns = [Turn("solo", 0, 0, "draft", "ok")]
    state.rounds.append(record)
    assert StanceMatrix().assess(state).coverage == 0.0


def test_consensus_report_coverage_defaults_to_zero():
    assert (
        ConsensusReport(
            round=1, matrix={}, min_confidence=0.0, converged=False, stalled_rounds=0
        ).coverage
        == 0.0
    )


@pytest.mark.parametrize("raw", ["true", "false"])
def test_boolean_confidence_is_not_a_measurement(raw):
    """``"confidence": true`` expresses an attitude, not the measured value 1.0."""
    card = f'{{"position": "p", "confidence": {raw}, "stance_on": {{"b": "agree"}}}}'
    stance = parse_stance(card, "a", 0, ["b"])
    assert stance is not None and stance.confidence is None


def test_nan_confidence_rejected():
    """NaN compares false against every threshold and silently walks past the confidence bar."""
    card = '{"position": "p", "confidence": NaN, "stance_on": {"b": "agree"}}'
    stance = parse_stance(card, "a", 0, ["b"])
    assert stance is not None and stance.confidence is None


@pytest.mark.parametrize(
    "body",
    [
        "rounds: 三",  # should be rounds: {max: N}; silently ignored
        # before
        "rounds:\n  max: -5",  # a TypeError traceback before
        "rounds:\n  max: 没有",
        "participants: 我是个字符串",  # iterated character by character before
        "consensus:\n  min_coverage: 5",  # out of range
        "budget:\n  max_usd: 免费",
    ],
)
def test_malformed_config_raises_configerror(tmp_path, body):
    path = tmp_path / "sesa.yaml"
    path.write_text(f"task: t\n{body}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_mod.load(path)


def test_valid_config_still_loads(tmp_path):
    path = tmp_path / "sesa.yaml"
    path.write_text(
        "task: t\nrounds:\n  max: 3\nconsensus:\n  min_coverage: 0.5\n", encoding="utf-8"
    )
    config = config_mod.load(path)
    assert config.max_rounds == 3 and config.min_coverage == 0.5


def test_truncation_survives_resume(tmp_path):
    """The truncation flag has to still be there after a resume, or statements() no longer adds the
    warning and the engine no longer refuses a half-finished stance card — two defences silently
    down at once.
    """
    from sesa.record import Recorder, load_state

    specs = [ParticipantSpec(id="a", adapter="cli")]
    state = DeliberationState(task="t", participants=specs, max_rounds=2)
    recorder = Recorder(tmp_path, "run1")
    turn = Turn("a", 0, 0, "draft", "前半句", truncated=True)
    state.rounds.append(RoundRecord(0))
    state.rounds[0].turns.append(turn)

    import sesa.events as ev

    recorder.emit(
        ev.RunStart(run_id="run1", task="t", participants=["a"], protocol="debate", max_rounds=2)
    )
    recorder.save_turn(turn)
    recorder.emit(
        ev.TurnEnd(
            round=0,
            participant="a",
            chars=3,
            duration_s=1.0,
            usage={},
            truncated=True,
            phase=0,
            kind="draft",
        )
    )

    back = load_state(recorder.dir, participants=specs, max_rounds=2)
    assert back.rounds[0].turns[0].truncated, "the truncation flag was lost on resume"
    assert "cut off" in back.rounds[0].statements()["a"]
