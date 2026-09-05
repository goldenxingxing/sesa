"""On resume, rounds at the end that produced nothing are not carried into the context.

Measured: a deliberation ran out of wall-clock budget, and in round 3 all three ran for 309
seconds and produced 0 characters. Read back as-is, the parties would find "total silence
last round" in their context on resume — and that is an artefact of a budget failure, not
anyone's opinion. Asking them to explain a silence that never happened injects noise into
the deliberation.
"""

from __future__ import annotations

import sesa.events as ev
from sesa.record import Recorder, load_state
from sesa.state import Turn
from sesa.types import ParticipantSpec

SPECS = [ParticipantSpec(id=p, adapter="cli") for p in ("a", "b")]


def _record(tmp_path, rounds: list[list[tuple[str, str | None]]]):
    """rounds[i] = [(participant, text_or_None), ...]; None means they failed that round."""
    recorder = Recorder(tmp_path, "run1")
    recorder.emit(
        ev.RunStart(
            run_id="run1",
            task="t",
            participants=["a", "b"],
            protocol="debate",
            max_rounds=len(rounds),
        )
    )
    for index, turns in enumerate(rounds):
        for pid, text in turns:
            recorder.save_turn(
                Turn(pid, index, 0, "draft", text or "", error=None if text else "超时")
            )
    recorder.close()
    return recorder.dir


def test_a_trailing_barren_round_is_dropped(tmp_path):
    path = _record(
        tmp_path,
        [
            [("a", "第一轮的话"), ("b", "第一轮的话")],
            [("a", "第二轮的话"), ("b", "第二轮的话")],
            [("a", None), ("b", None)],  # wiped out
        ],
    )
    state = load_state(path, participants=SPECS, max_rounds=2)
    assert len(state.rounds) == 2, "a barren final round should be dropped"
    assert state.current.index == 1


def test_several_trailing_barren_rounds_are_all_dropped(tmp_path):
    path = _record(
        tmp_path,
        [
            [("a", "有内容"), ("b", "有内容")],
            [("a", None), ("b", None)],
            [("a", None), ("b", None)],
        ],
    )
    assert len(load_state(path, participants=SPECS, max_rounds=2).rounds) == 1


def test_a_barren_round_in_the_middle_is_kept(tmp_path):
    """**Trim only from the end.** A round wiped out in the middle really did happen (everyone
    timing out, say), belongs to the deliberation's own history, and must not be erased.
    """
    path = _record(
        tmp_path,
        [
            [("a", "有内容"), ("b", "有内容")],
            [("a", None), ("b", None)],  # wiped out in the middle
            [("a", "又有内容"), ("b", "又有内容")],
        ],
    )
    state = load_state(path, participants=SPECS, max_rounds=2)
    assert len(state.rounds) == 3, "the middle round is real history and must not be erased"


def test_a_round_where_only_one_spoke_survives(tmp_path):
    """One person speaking means it was not barren — do not take "partial failure" for "wiped
    out".
    """
    path = _record(
        tmp_path,
        [
            [("a", "有内容"), ("b", "有内容")],
            [("a", "只有我说了"), ("b", None)],
        ],
    )
    assert len(load_state(path, participants=SPECS, max_rounds=2).rounds) == 2


def test_everything_barren_yields_no_rounds(tmp_path):
    path = _record(tmp_path, [[("a", None), ("b", None)]])
    assert load_state(path, participants=SPECS, max_rounds=2).rounds == []


def test_a_round_with_stances_but_no_turn_files_is_not_barren(tmp_path):
    """The test has to look at **both turns and stance cards**.

    Looking only at turns would wrongly cut old records and externally assembled event streams —
    those may hold only stance.emit with no turns/*.md, and a stance card is solid content.
    (My first version looked only at turns and knocked out two existing tests on the spot.)
    """
    import json

    run_dir = tmp_path / "runs" / "run1"
    (run_dir / "turns").mkdir(parents=True)
    events = [
        {"t": "run.start", "run_id": "run1", "task": "T", "participants": ["a", "b"]},
        {
            "t": "stance.emit",
            "round": 0,
            "participant": "a",
            "stance": {"position": "p", "confidence": 0.8, "stance_on": {"b": "agree"}},
        },
    ]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )

    state = load_state(run_dir, participants=SPECS, max_rounds=2)
    assert len(state.rounds) == 1, "a stance card means it was not barren"
    assert state.rounds[0].stances["a"].position == "p"
