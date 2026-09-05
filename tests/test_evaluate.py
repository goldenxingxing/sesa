"""Evaluation metrics.

What this layer answers is an existence question: **what did the debate actually change?**
If a participant's position never changes from round 0 to the last round, extra rounds of
debate are just three times the cost for one first draft.
"""

from __future__ import annotations

import json

from sesa.evaluate import collect, measure, to_dict


def write_run(tmp_path, run_id: str, events: list[dict]):
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
    )
    return run_dir


def stance_event(round: int, pid: str, position: str, *, changed=False, degraded=False, on=None):
    return {
        "t": "stance.emit",
        "ts": 1000.0 + round,
        "round": round,
        "participant": pid,
        "degraded": degraded,
        "stance": {
            "position": position,
            "confidence": 0.8,
            "stance_on": on or {},
            "changed": changed,
            "unknown": False,
        },
    }


def basic_run(**overrides):
    events = [
        {
            "t": "run.start",
            "ts": 1000.0,
            "run_id": "r1",
            "task": "该用 A 还是 B？",
            "participants": ["alice", "bob"],
            "protocol": "debate",
            "max_rounds": 3,
        },
        {"t": "round.start", "ts": 1000.0, "round": 0},
        stance_event(0, "alice", "我主张 A，因为运维成本低"),
        stance_event(0, "bob", "我主张 B，因为扩展性好"),
        {
            "t": "consensus.update",
            "ts": 1001.0,
            "round": 0,
            "unresolved": 2,
            "min_confidence": 0.8,
            "matrix": {},
            "state": "open",
        },
        {"t": "round.start", "ts": 1002.0, "round": 1},
        stance_event(
            1, "alice", "我改为主张 B，被扩展性论据说服", changed=True, on={"bob": "agree"}
        ),
        stance_event(1, "bob", "我主张 B，因为扩展性好", on={"alice": "agree"}),
        {
            "t": "consensus.update",
            "ts": 1003.0,
            "round": 1,
            "unresolved": 0,
            "min_confidence": 0.8,
            "matrix": {},
            "state": "converged",
        },
        {
            "t": "verdict.final",
            "ts": 1100.0,
            "outcome": "consensus",
            "run_id": "r1",
            "drafted_by": "bob",
            "rounds_used": 2,
            "unresolved": 0,
        },
    ]
    return events


def test_measures_stance_changes(tmp_path):
    metrics = measure(write_run(tmp_path, "r1", basic_run()))
    assert metrics.rounds_used == 2
    assert metrics.stance_changes == 1
    assert metrics.rounds[1].changed == ["alice"]


def test_position_drift_detects_a_changed_mind(tmp_path):
    """Someone can decline to admit it while their position quietly moves — drift and "admitting they
    were convinced" are complementary.
    """
    metrics = measure(write_run(tmp_path, "r1", basic_run()))
    assert metrics.position_drift["alice"] > 0.5  # from claim A to claim B
    assert metrics.position_drift["bob"] == 0.0  # not one word changed


def test_no_movement_at_all_shows_up_as_zero_drift(tmp_path):
    """This is precisely the bad case that has to be detected: three rounds of debate equalling three
    times the cost for one first draft.
    """
    events = [
        {
            "t": "run.start",
            "ts": 1000.0,
            "run_id": "r2",
            "task": "t",
            "participants": ["alice", "bob"],
            "protocol": "debate",
            "max_rounds": 3,
        },
    ]
    for r in range(3):
        events.append({"t": "round.start", "ts": 1000.0 + r, "round": r})
        events.append(stance_event(r, "alice", "我的立场不变"))
        events.append(stance_event(r, "bob", "我的立场也不变"))
    metrics = measure(write_run(tmp_path, "r2", events))
    assert metrics.stance_changes == 0
    assert metrics.mean_drift == 0.0
    assert metrics.stance_change_rate == 0.0


def test_degraded_rate_is_the_real_format_compliance_reading(tmp_path):
    events = basic_run()
    events[2] = stance_event(0, "alice", "p", degraded=True)
    metrics = measure(write_run(tmp_path, "r1", events))
    assert metrics.rounds[0].degraded == ["alice"]
    assert metrics.degraded_rate == 0.25  # 1 of 4 positions took the degraded path


def test_counts_cross_checks_and_usage(tmp_path):
    events = basic_run()
    events.insert(
        -1,
        {
            "t": "false_consensus",
            "ts": 1050.0,
            "round": 1,
            "detected_by": "bob",
            "conflicts": ["x"],
        },
    )
    events.insert(
        -1,
        {
            "t": "writer.mismatch",
            "ts": 1051.0,
            "round": 1,
            "writer": "bob",
            "kind": "omitted_disagreements",
            "detail": "d",
        },
    )
    events.insert(
        -1,
        {
            "t": "turn.end",
            "ts": 1052.0,
            "round": 1,
            "participant": "bob",
            "chars": 100,
            "duration_s": 1.0,
            "usage": {"known": True, "in": 50, "out": 20},
            "error": None,
        },
    )
    events.insert(
        -1,
        {
            "t": "turn.end",
            "ts": 1053.0,
            "round": 1,
            "participant": "alice",
            "chars": 80,
            "duration_s": 1.0,
            "usage": {"known": False, "in": None, "out": None},
            "error": None,
        },
    )
    metrics = measure(write_run(tmp_path, "r1", events))
    assert metrics.false_consensus == 1
    assert metrics.writer_mismatch == 1
    assert (metrics.input_tokens, metrics.output_tokens) == (50, 20)
    assert metrics.unmeasured_usage_calls == 1  # a CLI cannot report usage, so count it
    # honestly
    assert metrics.chars == 180
    assert metrics.wall_seconds == 100.0


def test_collect_skips_broken_records(tmp_path):
    """One bad record must not destroy a whole report."""
    write_run(tmp_path, "good", basic_run())
    broken = tmp_path / "runs" / "broken"
    broken.mkdir(parents=True)
    (broken / "events.jsonl").write_text('{"t": "round.start", "round": 0}\n', encoding="utf-8")
    assert [m.run_id for m in collect(tmp_path)] == ["r1"]


def test_to_dict_is_json_serialisable(tmp_path):
    metrics = measure(write_run(tmp_path, "r1", basic_run()))
    json.dumps(to_dict(metrics), ensure_ascii=False)


def test_interrupted_runs_are_excluded_from_statistics(tmp_path):
    """An interrupted run has an empty outcome and drift of 0 — mixing it into the mean silently
    pulls the conclusion towards zero.

    The most dangerous thing in statistics has never been noise, it is an empty value taken for
    data. This was walked into on a real run: too much concurrency got processes killed, the event
    stream stopped at turn.delta, and `sesa eval` took them into the average as normal records.
    """
    write_run(tmp_path, "complete", basic_run())
    truncated = basic_run()
    truncated = truncated[
        : truncated.index(next(e for e in truncated if e["t"] == "verdict.final"))
    ]
    write_run(tmp_path, "killed", truncated)

    assert [m.run_id for m in collect(tmp_path)] == ["r1"]

    both = collect(tmp_path, only_usable=False)
    assert len(both) == 2
    assert [m.usable for m in both] == [True, False]


def test_completed_flag_reflects_verdict_final(tmp_path):
    metrics = measure(write_run(tmp_path, "r1", basic_run()))
    assert metrics.completed is True
    assert to_dict(metrics)["completed"] is True


def test_divergence_measures_difference_between_participants_not_over_time(tmp_path):
    """Drift measures "how much one person changed"; divergence measures "how different several
    people are right now".

    The latter corresponds directly to the "homogeneous assent" failure mode: a table of people
    saying nearly the same thing while the disagreement matrix reports "agreement".
    """
    events = [
        {
            "t": "run.start",
            "ts": 1000.0,
            "run_id": "r1",
            "task": "t",
            "participants": ["alice", "bob"],
            "protocol": "debate",
            "max_rounds": 2,
        },
        {"t": "round.start", "ts": 1000.0, "round": 0},
        stance_event(0, "alice", "我主张 A，因为运维成本最低"),
        stance_event(0, "bob", "我主张 B，因为扩展性优先"),
        {"t": "round.start", "ts": 1001.0, "round": 1},
        stance_event(1, "alice", "我主张 A，因为运维成本最低"),
        stance_event(1, "bob", "我主张 A，因为运维成本最低"),
        {
            "t": "verdict.final",
            "ts": 1002.0,
            "outcome": "consensus",
            "run_id": "r1",
            "drafted_by": "bob",
            "rounds_used": 2,
            "unresolved": 0,
        },
    ]
    metrics = measure(write_run(tmp_path, "r1", events))
    by_round = metrics.divergence_by_round
    # assert a relationship rather than a magic number: the two sentences share an "I argue for X
    # because …" skeleton, so the absolute value was never going to be high
    assert by_round[0] > 0.4  # in round 0 the two argue for different things
    assert by_round[1] == 0.0  # in round 1 bob paraphrases alice entirely
    assert by_round[0] > by_round[1]  # homogenisation happened during the discussion
    assert metrics.final_divergence == 0.0
    # over the same data alice changed not one word while bob did — the two metrics measure
    # different directions
    assert metrics.position_drift["alice"] == 0.0
    assert metrics.position_drift["bob"] > 0.4


def test_divergence_needs_at_least_two_positions(tmp_path):
    events = [
        {
            "t": "run.start",
            "ts": 1000.0,
            "run_id": "r1",
            "task": "t",
            "participants": ["alice", "bob"],
            "protocol": "debate",
            "max_rounds": 1,
        },
        {"t": "round.start", "ts": 1000.0, "round": 0},
        stance_event(0, "alice", "只有我一个人表了态"),
        {
            "t": "verdict.final",
            "ts": 1001.0,
            "outcome": "exhausted",
            "run_id": "r1",
            "drafted_by": None,
            "rounds_used": 1,
            "unresolved": 1,
        },
    ]
    metrics = measure(write_run(tmp_path, "r1", events))
    assert metrics.divergence_by_round == {}
    # **Not 0.0.** 0.0 means "a table of people paraphrasing each other" — a strong conclusion. With
    # only one person taking a position, that thing was never measured at all, and missing
    # measurement must not pass for a finding.
    assert metrics.final_divergence is None


# --------------------------------------------------------------------------- # Residual churn
# Measured, the vast majority of cells sit at partial for a long time and the categorical metric has
# no resolution there — while the movement is in the residuals ("which points I have not yet
# accepted") appearing and disappearing.
# --------------------------------------------------------------------------- #


def stance_with_residuals(round: int, pid: str, position: str, residuals: dict[str, list[str]]):
    event = stance_event(round, pid, position, on={t: "partial" for t in residuals})
    event["stance"]["residuals"] = residuals
    return event


def residual_run(rounds: list[dict[str, dict[str, list[str]]]]):
    """rounds[i] = {participant: {target: [residual, ...]}}"""
    events = [
        {
            "t": "run.start",
            "ts": 1000.0,
            "run_id": "r1",
            "task": "t",
            "participants": ["alice", "bob"],
            "protocol": "debate",
            "max_rounds": len(rounds),
        },
    ]
    for index, per_participant in enumerate(rounds):
        events.append({"t": "round.start", "ts": 1000.0 + index, "round": index})
        for pid, residuals in per_participant.items():
            events.append(stance_with_residuals(index, pid, f"{pid} 的立场", residuals))
        events.append(
            {
                "t": "consensus.update",
                "ts": 1000.5 + index,
                "round": index,
                "unresolved": 0,
                "min_confidence": 0.8,
                "matrix": {p: {t: "partial" for t in r} for p, r in per_participant.items()},
                "state": "open",
            }
        )
    events.append(
        {
            "t": "verdict.final",
            "ts": 2000.0,
            "outcome": "consensus_with_reservations",
            "run_id": "r1",
            "drafted_by": "bob",
            "rounds_used": len(rounds),
            "unresolved": 0,
        }
    )
    return events


def test_residual_flow_reports_counts_not_content_moves(tmp_path):
    """Items are compared as exact strings, and an LLM almost never repeats itself verbatim —
    measured, 0 of 51 were identical.

    So "withdrawn/added" always equals the two rounds' entire counts: a structurally inevitable
    constant, not a measurement. The flow reports counts only and no longer poses as progress at
    the level of content.
    """
    metrics = measure(
        write_run(
            tmp_path,
            "r1",
            residual_run(
                [
                    {"alice": {"bob": ["甲", "乙", "丙"]}},
                    {"alice": {"bob": ["甲改", "乙改"]}},
                ]
            ),
        )
    )
    assert metrics.residual_flow == [(1, 3, 2, 2)]  # 3 items last round, 2 this round
    assert metrics.residual_trend == -1  # a net decrease of one
    assert metrics.residual_counts == {0: 3, 1: 2}


def test_abort_reason_is_carried_through(tmp_path):
    """An aborted run should state its reason explicitly, rather than leaving people to infer it from
    "verdict.final is missing".

    On real runs a background batch was taken away from outside three times, and the event stream
    simply stopped at turn.delta — the only way to tell it had not finished was inference, and
    inference cannot say who aborted it or why.
    """
    truncated = basic_run()
    truncated = truncated[
        : truncated.index(next(e for e in truncated if e["t"] == "verdict.final"))
    ]
    truncated.append(
        {"t": "run.aborted", "ts": 1099.0, "reason": "收到 SIGTERM，运行被外部中止", "round": 1}
    )
    metrics = measure(write_run(tmp_path, "killed", truncated))
    assert metrics.completed is False
    assert metrics.usable is False
    assert "SIGTERM" in metrics.aborted
    assert to_dict(metrics)["aborted"] == metrics.aborted


def test_missing_abort_record_leaves_the_reason_empty(tmp_path):
    """An abort that left no record is reported honestly as "reason unknown", never invented."""
    truncated = basic_run()
    truncated = truncated[
        : truncated.index(next(e for e in truncated if e["t"] == "verdict.final"))
    ]
    metrics = measure(write_run(tmp_path, "killed", truncated))
    assert metrics.usable is False
    assert metrics.aborted == ""


def _granularity_run(rounds: list[dict[str, list[str]]]):
    """rounds[i] = {target: [residual, ...]}, all raised by alice."""
    return residual_run([{"alice": r} for r in rounds])


def test_granularity_is_reported_alongside_similarity(tmp_path):
    """The similarity correlates with the count at r≈0.83 and with item length at r≈0.86 — read on its
    own, this number is fooled by writing style.
    """
    metrics = measure(
        write_run(
            tmp_path,
            "r1",
            _granularity_run(
                [
                    {"bob": ["短" * 10, "短" * 10]},
                    {"bob": ["短" * 10, "短" * 10, "短" * 10]},
                ]
            ),
        )
    )
    count, length, cv = metrics.residual_granularity
    assert count == 2.5  # 2 and 3 items per round
    assert length == 10
    assert cv == 0.0  # length unchanged


def test_runs_with_different_granularity_are_flagged_incomparable(tmp_path):
    """When granularity differs too much, comparing similarity compares writing styles rather than
    debates.

    This project once drew a "the two groups are strikingly different" conclusion from exactly that
    and withdrew it: the claude run averaged about 150 characters an item and the DeepSeek run
    about 57, which explains nearly all of the similarity difference.
    """
    verbose = measure(
        write_run(
            tmp_path,
            "verbose",
            _granularity_run(
                [
                    {"bob": ["长" * 150, "长" * 150]},
                    {"bob": ["长" * 150, "长" * 150]},
                ]
            ),
        )
    )
    terse = measure(
        write_run(
            tmp_path,
            "terse",
            _granularity_run(
                [
                    {"bob": ["短" * 50, "短" * 50]},
                    {"bob": ["短" * 50, "短" * 50]},
                ]
            ),
        )
    )
    assert not verbose.comparable_with(terse)
    assert verbose.comparable_with(verbose)


def test_unstable_granularity_within_a_run_is_visible(tmp_path):
    """When length fluctuates sharply within a run, the cross-round similarity trend is polluted by
    style changes just the same.
    """
    metrics = measure(
        write_run(
            tmp_path,
            "r1",
            _granularity_run(
                [
                    {"bob": ["短" * 40]},
                    {"bob": ["长" * 160]},
                ]
            ),
        )
    )
    assert metrics.residual_granularity[2] > 0.25  # the coefficient of variation is over
    # the warning line
