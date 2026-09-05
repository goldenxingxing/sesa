"""Follow a deliberation as it happens — **without waiting for it to finish**.

The event stream is written to `events.jsonl` throughout, and there used to be no way in to
look at it: `report` waits for RESULT.md and `runs` skipped anything unfinished. So once a
deliberation started it was a black box.

**Many problems are precisely in the middle**: one round timing out, one participant failing
every round, evidence red throughout. None of that need leave a trace in the outcome —
measured, a deliberation whose outcome was `exhausted` had, in the middle, "one participant
timed out after 900 seconds in round 0, and its evidence was red for two rounds", none of
which is visible anywhere in RESULT.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_dir(tmp_path: Path, events: list[dict], *, finished: bool) -> Path:
    run = tmp_path / "runs" / "20260831-000000-abcdef"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )
    if finished:
        (run / "RESULT.json").write_text('{"outcome": "consensus", "task": "t"}', encoding="utf-8")
    return run


def _cli(args: list[str], cwd: Path) -> str:
    got = subprocess.run(
        [sys.executable, "-m", "sesa.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return got.stdout + got.stderr


IN_FLIGHT = [
    {
        "t": "run.start",
        "task": "该用 Postgres 还是 SQLite？",
        "participants": ["a", "b"],
        "protocol": "debate",
        "max_rounds": 2,
        "run_id": "x",
    },
    {"t": "round.start", "round": 0, "rapporteur": "a"},
    {
        "t": "turn.end",
        "round": 0,
        "participant": "a",
        "chars": 900,
        "duration_s": 12.0,
        "usage": {},
        "error": None,
    },
    {
        "t": "turn.end",
        "round": 0,
        "participant": "b",
        "chars": 0,
        "duration_s": 900.0,
        "usage": {},
        "error": "AdapterError: b: 超过 900s 未完成",
    },
    {
        "t": "evidence",
        "round": 0,
        "participant": "b",
        "cmd": "pytest -q",
        "exit_code": 1,
        "summary": "3 failed",
    },
]


def test_the_middle_of_a_run_is_visible_without_waiting_for_the_end(tmp_path):
    _run_dir(tmp_path, IN_FLIGHT, finished=False)

    out = _cli(["watch", "--no-follow", "--root", "."], tmp_path)

    assert "超过 900s 未完成" in out, "a failure part-way has to be visible"
    assert "failed(1)" in out, "evidence gone red has to be visible"
    assert "Round 0" in out


def test_an_unfinished_run_is_listed_rather_than_skipped(tmp_path):
    """`runs` used to `continue` straight past a record with no RESULT.json, so "the run in
    progress" did not exist in the list at all.
    """
    _run_dir(tmp_path, IN_FLIGHT, finished=False)

    out = _cli(["runs", "--root", "."], tmp_path)

    assert "20260831-000000-abcdef" in out
    assert "running" in out or "silent for" in out


def test_a_finished_run_still_shows_its_outcome(tmp_path):
    _run_dir(
        tmp_path,
        [
            *IN_FLIGHT,
            {
                "t": "verdict.final",
                "outcome": "consensus",
                "run_id": "x",
                "rounds_used": 1,
                "unresolved": 0,
                "result_path": "RESULT.md",
                "drafted_by": "a",
            },
        ],
        finished=True,
    )

    assert "consensus" in _cli(["runs", "--root", "."], tmp_path)


def test_watching_stops_at_the_verdict(tmp_path):
    """Follow it to the outcome and stop; it must not hang on forever."""
    _run_dir(
        tmp_path,
        [
            *IN_FLIGHT,
            {
                "t": "verdict.final",
                "outcome": "deadlock",
                "run_id": "x",
                "rounds_used": 1,
                "unresolved": 2,
                "result_path": "RESULT.md",
                "drafted_by": None,
            },
        ],
        finished=False,
    )

    out = _cli(["watch", "--root", "."], tmp_path)  # note: no --no-follow

    assert "僵局" in out or "deadlock" in out


def test_a_missing_run_fails_loudly(tmp_path):
    (tmp_path / "runs").mkdir()

    out = _cli(["watch", "nope", "--root", "."], tmp_path)

    assert "Cannot find" in out


BUMPY = [
    {
        "t": "run.start",
        "task": "t",
        "participants": ["a", "b"],
        "protocol": "debate",
        "max_rounds": 2,
        "run_id": "x",
    },
    {"t": "round.start", "round": 0, "rapporteur": "a"},
    {
        "t": "turn.end",
        "round": 0,
        "participant": "a",
        "chars": 23570,
        "duration_s": 47.0,
        "usage": {},
        "error": None,
        "truncated": True,
    },
    {
        "t": "files.applied",
        "round": 0,
        "participant": "a",
        "files": [],
        "rejected": [],
        "fences_seen": 3,
    },
    {
        "t": "turn.end",
        "round": 0,
        "participant": "b",
        "chars": 0,
        "duration_s": 900.0,
        "usage": {},
        "error": "AdapterError: 总时长超过 900s",
    },
    {
        "t": "adoption",
        "round": 1,
        "participant": "b",
        "adopted_from": "a",
        "path": "impl.py",
        "similarity_to_peer": 0.97,
        "similarity_to_own": 0.08,
    },
    {
        "t": "verdict.final",
        "outcome": "exhausted",
        "run_id": "x",
        "rounds_used": 2,
        "unresolved": 2,
        "result_path": "RESULT.md",
        "drafted_by": None,
    },
]


def test_the_things_that_went_wrong_are_pulled_out_of_the_stream(tmp_path):
    """**The event stream holds everything; the problem is that nobody reads it.**

    One deliberation has tens of thousands of `turn.delta` events and an anomaly drowns among
    them. Measured, one participant was truncated in both rounds and landed zero files, and the
    only mention of it came from **another participant** in passing — the engine knew, and did
    not say. The author found it only after half an hour of reading raw events.

    The answer is not to print more logs (which would only drown it further); it is to pick the
    anomalies out.
    """
    _run_dir(tmp_path, BUMPY, finished=False)

    out = _cli(["watch", "--root", "."], tmp_path)

    assert "What went wrong" in out
    assert "cut off by the output budget" in out
    assert "landed no files" in out
    assert "the turn failed" in out
    assert "wholesale" in out


def test_a_clean_run_reports_no_incidents(tmp_path):
    _run_dir(
        tmp_path,
        [
            {
                "t": "run.start",
                "task": "t",
                "participants": ["a"],
                "protocol": "debate",
                "max_rounds": 1,
                "run_id": "x",
            },
            {
                "t": "turn.end",
                "round": 0,
                "participant": "a",
                "chars": 100,
                "duration_s": 1.0,
                "usage": {},
                "error": None,
            },
            {
                "t": "verdict.final",
                "outcome": "consensus",
                "run_id": "x",
                "rounds_used": 1,
                "unresolved": 0,
                "result_path": "RESULT.md",
                "drafted_by": "a",
            },
        ],
        finished=False,
    )

    assert "出的岔子" not in _cli(["watch", "--root", "."], tmp_path)


def test_no_anomalies_does_not_claim_everything_is_fine(tmp_path):
    """**"No anomaly reported" is not "everything is fine".**

    The anomaly list is a closed set distilled from **holes already fallen into**. The two
    defects actually caught today (`fences_seen` lying, someone truncated in both rounds) were
    both of a kind nobody had thought of in advance — which is to say, this list could not have
    recognised them on the day they happened.

    Presenting an empty list as "every turn was complete and the self-tests passed" is the very
    error this project keeps guarding against: **an empty value masquerading as data**. And the
    author committed it ten minutes after writing this feature.
    """
    _run_dir(
        tmp_path,
        [
            {
                "t": "run.start",
                "task": "t",
                "participants": ["a"],
                "protocol": "debate",
                "max_rounds": 1,
                "run_id": "x",
            },
            {
                "t": "turn.end",
                "round": 0,
                "participant": "a",
                "chars": 100,
                "duration_s": 1.0,
                "usage": {},
                "error": None,
            },
            {
                "t": "verdict.final",
                "outcome": "consensus",
                "run_id": "x",
                "rounds_used": 1,
                "unresolved": 0,
                "result_path": "RESULT.md",
                "drafted_by": "a",
            },
        ],
        finished=False,
    )

    out = _cli(["watch", "--root", "."], tmp_path)

    assert "所有发言都完整" not in out, "it must not claim to have checked what it did not check"


def test_the_report_says_which_categories_it_actually_checked():
    """List what was checked, so the reader knows **what was not**."""
    from sesa.report import render_report
    from sesa.state import DeliberationState
    from sesa.types import Outcome, ParticipantSpec, Result

    state = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ("a", "b")],
        max_rounds=1,
    )

    text = render_report(state, Result(run_id="r", task="t", outcome=Outcome.CONSENSUS))

    assert "No anomaly of a known kind was found" in text
    assert "not the same as «everything is fine»" in text
    assert "cannot recognise a single new kind of problem" in text
    assert "events.jsonl" in text, "it has to say where to look at the raw record"
