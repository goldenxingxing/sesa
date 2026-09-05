"""The consensus layer: stance extraction's tolerance, and the honesty of the three-state
assessment.

The property that matters most here is **no pretending to be united**: an unparseable stance,
insufficient confidence, and anyone still explicitly objecting all mean it cannot be judged a
consensus.
"""

from __future__ import annotations

from sesa.consensus.matrix import StanceMatrix, render_matrix
from sesa.consensus.stance import parse_stance, strip_stance_block
from sesa.state import DeliberationState, RoundRecord
from sesa.types import Outcome, ParticipantSpec, Stance, StanceOn

IDS = ["claude", "kimi", "gpt5"]


# --------------------------------------------------------------------------- # Extraction
# --------------------------------------------------------------------------- #


def test_parses_fenced_json_block():
    text = (
        '正文。\n\n```json\n{"position":"用 Postgres","confidence":0.8,'
        '"stance_on":{"kimi":{"verdict":"agree","reason":""}}}\n```'
    )
    stance = parse_stance(text, "claude", 1, ["kimi"])
    assert stance.position == "用 Postgres"
    assert stance.stance_on["kimi"].verdict == "agree"


def test_falls_back_to_bare_balanced_object():
    text = '结论 {"position":"用 SQLite","stance_on":{"kimi":"disagree"}} 完'
    stance = parse_stance(text, "claude", 1, ["kimi"])
    assert stance.position == "用 SQLite"
    assert stance.stance_on["kimi"].verdict == "disagree"


def test_braces_inside_strings_do_not_break_scanning():
    """A regex is always wrong on nesting, so scan with bracket balancing."""
    text = '{"a":{"b":"}"}} 然后 {"position":"真立场","stance_on":{}}'
    assert parse_stance(text, "claude", 1, ["kimi"]).position == "真立场"


def test_percent_confidence_is_coerced():
    text = '{"position":"p","confidence":85,"stance_on":{"kimi":"agree"}}'
    assert parse_stance(text, "claude", 1, ["kimi"]).confidence == 0.85


def test_partial_with_residuals_is_kept():
    text = (
        '{"position":"p","stance_on":{"kimi":{"verdict":"部分同意",'
        '"reason":"r","residuals":["尚未接受的点 A","尚未接受的点 B"]}}}'
    )
    on = parse_stance(text, "claude", 1, ["kimi"]).stance_on["kimi"]
    assert on.verdict == "partial"
    assert on.residuals == ["尚未接受的点 A", "尚未接受的点 B"]


def test_partial_without_residuals_degrades_to_unknown():
    """A "partial agreement" with an empty payload cannot be checked: it says neither what is agreed
    nor what is held back.
    """
    text = '{"position":"p","stance_on":{"kimi":{"verdict":"partial","reason":"含糊"}}}'
    assert parse_stance(text, "claude", 1, ["kimi"]).stance_on["kimi"].verdict == "unknown"


def test_hallucinated_participants_are_dropped():
    text = '{"position":"p","stance_on":{"kimi":"agree","ghost":"agree"}}'
    stance = parse_stance(text, "claude", 1, ["kimi"])
    assert "ghost" not in stance.stance_on


def test_returns_none_when_nothing_parseable():
    assert parse_stance("一段完全没有 JSON 的话", "claude", 1, ["kimi"]) is None


def test_quoted_json_does_not_hide_the_real_stance_card():
    """Regression: when discussing a JSON-based system, a participant quoting some other JSON is all
    but inevitable.

    An early version returned as soon as any parseable fence was found, so the real stance card
    could never be found again. Measured, claude quoted our own consensus.update event and its
    stance card was ruled unparseable — a defect in the extractor, not the participant breaking
    the format.
    """
    text = (
        "我引用一下引擎的日志：\n\n"
        '```json\n{"t": "consensus.update", "round": 2, "unresolved": 0}\n```\n\n'
        "基于这条日志，我的立场是：\n\n"
        '```json\n{"position":"真正的立场卡","confidence":0.8,'
        '"stance_on":{"kimi":{"verdict":"agree","reason":""}}}\n```'
    )
    stance = parse_stance(text, "claude", 1, ["kimi"])
    assert stance is not None
    assert stance.position == "真正的立场卡"
    assert stance.stance_on["kimi"].verdict == "agree"


def test_stance_card_outside_a_fence_is_still_stripped():
    """The stance card is not necessarily inside a tidy fence, and leaving it in the prose makes it
    noise for everyone else.
    """
    text = '正文结论。\n\n{"position":"p","confidence":0.7,"stance_on":{}}'
    assert "position" not in strip_stance_block(text)
    assert strip_stance_block(text).startswith("正文结论")


def test_stance_block_is_stripped_from_human_facing_text():
    text = '给人看的正文。\n\n```json\n{"position":"p","stance_on":{}}\n```'
    assert strip_stance_block(text) == "给人看的正文。"


def test_unrelated_code_block_is_not_stripped():
    text = '看这段代码：\n\n```json\n{"unrelated": true}\n```'
    assert "unrelated" in strip_stance_block(text)


# --------------------------------------------------------------------------- # The matrix and the
# assessment --------------------------------------------------------------------------- #


def make_state(**kw) -> DeliberationState:
    return DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in IDS],
        max_rounds=4,
        **kw,
    )


def stance(pid: str, verdicts: dict[str, str], confidence=0.8, changed=False) -> Stance:
    return Stance(
        participant=pid,
        round=0,
        position=f"{pid} 的立场",
        confidence=confidence,
        stance_on={
            t: StanceOn(verdict=v, residuals=["尚未接受的点"] if v == "partial" else [])
            for t, v in verdicts.items()
        },
        changed_from_last_round=changed,
    )


def commit(state: DeliberationState, index: int, stances: dict[str, Stance]) -> RoundRecord:
    record = RoundRecord(index, stances=stances)
    state.rounds.append(record)
    matrix = StanceMatrix()
    record.consensus = matrix.assess(state)
    return record


def all_agree() -> dict[str, Stance]:
    return {pid: stance(pid, {o: "agree" for o in IDS if o != pid}) for pid in IDS}


def test_full_agreement_converges():
    state = make_state()
    record = commit(state, 0, all_agree())
    assert record.consensus.unresolved == 0
    assert record.consensus.converged
    assert record.consensus.blockers == []


def test_one_disagreement_blocks_consensus():
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    record = commit(state, 0, stances)
    assert record.consensus.unresolved == 1
    assert not record.consensus.converged
    assert record.consensus.disagreeing_pairs() == [("gpt5", "claude")]


def test_partial_blocks_full_consensus_but_is_not_a_hard_disagreement():
    """partial blocks the label "consensus", not termination.

    Measured twice: with both sides partial on each other the matrix judged "converged" while the
    rapporteur read a substantive disagreement out of the prose. Under default-deny, only an
    explicit agree counts as resolved.
    """
    state = make_state()
    stances = all_agree()
    stances["kimi"] = stance("kimi", {"claude": "partial", "gpt5": "agree"})
    report = commit(state, 0, stances).consensus
    assert report.reservations == 1
    assert report.unresolved == 0  # not hard disagreement
    assert not report.converged  # but not full consensus either
    assert report.residuals == {"kimi → claude": ["尚未接受的点"]}


def test_reservations_alone_downgrade_rather_than_fail():
    """With only reservations left it is not a failure — it is "broadly agreed, with the reservations
    in black and white".
    """
    state = make_state()
    stances = all_agree()
    stances["kimi"] = stance("kimi", {"claude": "partial", "gpt5": "agree"})
    report = commit(state, 0, stances).consensus
    matrix = StanceMatrix()
    assert matrix.decide_outcome(report, rounds_left=0) is Outcome.CONSENSUS_WITH_RESERVATIONS
    assert (
        matrix.decide_outcome(report, rounds_left=9, budget_exhausted=True)
        is Outcome.CONSENSUS_WITH_RESERVATIONS
    )


def test_hard_disagreement_still_fails_even_with_reservations_present():
    state = make_state()
    stances = all_agree()
    stances["kimi"] = stance("kimi", {"claude": "partial", "gpt5": "agree"})
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    report = commit(state, 0, stances).consensus
    assert report.unresolved == 1
    assert StanceMatrix().decide_outcome(report, rounds_left=0) is Outcome.EXHAUSTED


def test_unknown_stance_counts_as_unresolved_not_as_zero():
    """default-deny: when the engine does not hold someone's judgement, it must not assume agreement
    on their behalf.

    Regression: in an early version unknown only added a blocker and did not count towards
    unresolved, so the log showed the misleading pair "the matrix has unknown cells and unresolved
    reports 0".
    """
    state = make_state()
    stances = all_agree()
    stances["kimi"] = Stance.as_unknown("kimi", 0)
    report = commit(state, 0, stances).consensus
    assert report.unknown_participants == ["kimi"]
    assert report.unresolved > 0  # no longer a misleading 0
    assert not report.converged


def test_a_participant_who_reported_nothing_still_blocks_consensus():
    """Dropping the unknown makes "lowest confidence" look higher than it is.

    The earlier approach forced the unknown to 0.0, so "I am very unsure" and "I did not say" were
    compressed into one number — while the two mean opposite things for the assessment. The
    measured consequence: a run reporting 0.00 explicitly was judged "consensus with reservations"
    while one reporting 0.01 was judged "unfinished".

    They are accounted separately now: ``min_confidence`` counts only those who **reported**, and
    those who did not get their own blocker. The protection has not weakened; it merely no longer
    rests on a sentinel with two meanings.
    """
    state = make_state()
    stances = all_agree()
    stances["kimi"] = Stance.as_unknown("kimi", 0)

    report = commit(state, 0, stances).consensus

    assert report.min_confidence == 0.8, (
        "the lowest among those who reported is 0.8; say so honestly"
    )
    assert report.confidences_known < len(state.ids), "but someone did not report"
    assert not report.converged
    assert any(
        "no participant reported a confidence" in b or "could not be parsed" in b
        for b in report.blockers
    )


def test_low_confidence_blocks_consensus():
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "agree", "kimi": "agree"}, confidence=0.2)
    report = commit(state, 0, stances).consensus
    assert not report.converged
    assert any("confidence" in b for b in report.blockers)


def test_deadlock_after_stability_window():
    """K consecutive rounds with nobody changing position and the disagreements not falling — stuck
    is not the same as united.
    """
    state = make_state()
    stuck = all_agree()
    stuck["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "disagree"})
    matrix = StanceMatrix(stability_window=2)
    for r in range(3):
        record = RoundRecord(r, stances=stuck)
        state.rounds.append(record)
        record.consensus = matrix.assess(state)
    assert state.rounds[-1].consensus.stalled_rounds >= 2
    assert matrix.decide_outcome(state.rounds[-1].consensus, rounds_left=5) is Outcome.DEADLOCK


def test_self_report_alone_does_not_reset_the_stall_counter():
    """A self-reported "I changed my position" is not enough to reset the stall counter.

    Measured: across 33 deliberations there were 33 self-reported changes while the category
    judgement actually moved 3 times — roughly 11× over-reporting. Allowing a self-report to reset
    it on its own lets one participant fond of saying they changed defer deadlock detection
    indefinitely.
    """
    state = make_state()
    matrix = StanceMatrix(stability_window=2)
    stuck = all_agree()
    stuck["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "disagree"})
    for r in range(2):
        record = RoundRecord(r, stances=stuck)
        state.rounds.append(record)
        record.consensus = matrix.assess(state)

    # only says it changed; neither the judgement nor the residuals moved
    claimed = dict(stuck)
    claimed["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "disagree"}, changed=True)
    record = RoundRecord(2, stances=claimed)
    state.rounds.append(record)
    record.consensus = matrix.assess(state)
    assert record.consensus.stalled_rounds > 0  # the stall accumulates as usual


def test_objective_movement_resets_the_stall_counter():
    """Disagreements falling, or the set of residuals really moving — these are objective signals that
    do not depend on self-report.
    """
    state = make_state()
    matrix = StanceMatrix(stability_window=2)
    stuck = all_agree()
    stuck["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "disagree"})
    for r in range(2):
        record = RoundRecord(r, stances=stuck)
        state.rounds.append(record)
        record.consensus = matrix.assess(state)

    moved = dict(stuck)
    moved["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})  # disagreements fell
    record = RoundRecord(2, stances=moved)
    state.rounds.append(record)
    record.consensus = matrix.assess(state)
    assert record.consensus.stalled_rounds == 0


def test_residual_movement_also_counts_as_objective_progress():
    """The residuals changed while the category did not — when everyone sits at partial for a long
    time, this is the only signal of progress.
    """
    state = make_state()
    matrix = StanceMatrix(stability_window=2)

    def with_residuals(items: list[str]) -> dict[str, Stance]:
        out = all_agree()
        out["kimi"] = Stance(
            participant="kimi",
            round=0,
            confidence=0.8,
            stance_on={
                "claude": StanceOn(verdict="partial", residuals=items),
                "gpt5": StanceOn(verdict="agree"),
            },
        )
        return out

    for r, items in enumerate([["A", "B"], ["A", "B"]]):
        record = RoundRecord(r, stances=with_residuals(items))
        state.rounds.append(record)
        record.consensus = matrix.assess(state)
    assert state.rounds[-1].consensus.stalled_rounds > 0  # not an inch moved → stalled

    record = RoundRecord(2, stances=with_residuals(["A"]))  # one reservation withdrawn
    state.rounds.append(record)
    record.consensus = matrix.assess(state)
    assert record.consensus.stalled_rounds == 0


def test_outcome_priority_consensus_beats_exhaustion():
    state = make_state()
    report = commit(state, 0, all_agree()).consensus
    matrix = StanceMatrix()
    assert matrix.decide_outcome(report, rounds_left=0) is Outcome.CONSENSUS


def test_running_out_of_rounds_is_exhausted_not_consensus():
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    report = commit(state, 0, stances).consensus
    assert StanceMatrix().decide_outcome(report, rounds_left=0) is Outcome.EXHAUSTED


def test_budget_exhaustion_reported_honestly():
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    report = commit(state, 0, stances).consensus
    assert (
        StanceMatrix().decide_outcome(report, rounds_left=9, budget_exhausted=True)
        is Outcome.EXHAUSTED
    )


def test_matrix_renders_readably():
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    text = render_matrix(commit(state, 0, stances).consensus)
    assert "oppose" in text and "claude" in text


# --------------------------------------------------------------------------- # T2 degradation: the
# line-by-line verdict table
# Asking for the same JSON card a second time repeats an identical hard problem to a component that
# has proved it cannot handle that format. The line table has a tiny output space, and a bad line
# costs one cell — whereas one syntax error voids a whole JSON card.
# --------------------------------------------------------------------------- #

from sesa.consensus.stance import parse_verdict_lines  # noqa: E402


def lines(text: str):
    return parse_verdict_lines(text, "claude", 1, ["kimi", "gpt5"])


def test_line_format_is_parsed():
    stance = lines("confidence: 0.7\nkimi: agree\ngpt5: disagree | 他假设了单机部署")
    assert stance.confidence == 0.7
    assert stance.stance_on["kimi"].verdict == "agree"
    assert stance.stance_on["gpt5"].verdict == "disagree"
    assert stance.stance_on["gpt5"].reason == "他假设了单机部署"


def test_a_bad_line_only_loses_that_cell():
    """This is exactly the line table's core advantage over JSON."""
    stance = lines("kimi: agree\n@@@ 一行乱码 @@@\ngpt5: disagree | r")
    assert set(stance.stance_on) == {"kimi", "gpt5"}


def test_markdown_bullets_and_chinese_punctuation_are_tolerated():
    stance = lines("- kimi：同意\n- gpt5：反对 | 理由")
    assert stance.stance_on["kimi"].verdict == "agree"
    assert stance.stance_on["gpt5"].verdict == "disagree"


def test_partial_without_a_note_degrades_to_unknown_here_too():
    """The same rule as the JSON path; a degraded format must not become a back door around
    validation.
    """
    assert lines("kimi: partial\ngpt5: agree").stance_on["kimi"].verdict == "unknown"


def test_partial_with_a_note_becomes_a_residual():
    stance = lines("kimi: partial | 规模假设仍未定")
    assert stance.stance_on["kimi"].residuals == ["规模假设仍未定"]


def test_hallucinated_participants_are_dropped_in_line_format():
    assert "ghost" not in lines("kimi: agree\nghost: agree").stance_on


def test_returns_none_when_no_line_parses():
    assert lines("我不想按格式回答") is None


# --------------------------------------------------------------------------- # "Opposed" and "not
# measured" must be accounted separately
# --------------------------------------------------------------------------- #


def test_opposition_and_missing_measurement_are_counted_separately():
    """Compressing the two into one scalar is labelling missing data as disagreement."""
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    stances["kimi"] = Stance.as_unknown("kimi", 0)
    report = commit(state, 0, stances).consensus
    assert report.opposed == 1
    assert report.unmeasured == 2  # kimi's cells on both others were not measured
    assert report.unresolved == 3  # the derived quantity is still available
    assert 0 < report.coverage < 1
    assert report.unmeasured_cells == ["kimi → claude", "kimi → gpt5"]


def test_missing_measurement_alone_yields_partial_coverage_not_failure():
    state = make_state()
    stances = all_agree()
    stances["kimi"] = Stance.as_unknown("kimi", 0)
    report = commit(state, 0, stances).consensus
    assert report.opposed == 0 and report.unmeasured > 0
    assert (
        StanceMatrix().decide_outcome(report, rounds_left=0) is Outcome.PARTIAL_COVERAGE_CONSENSUS
    )


def test_zero_coverage_is_never_any_kind_of_consensus():
    """In round 0 nobody has taken a position yet, and judging that "consensus with partial coverage"
    is absurd.
    """
    state = make_state()
    nothing = {pid: Stance(participant=pid, round=0, confidence=0.8) for pid in IDS}
    report = commit(state, 0, nothing).consensus
    assert report.coverage == 0.0
    assert StanceMatrix().decide_outcome(report, rounds_left=0) is Outcome.EXHAUSTED


def test_opposition_still_fails_even_when_coverage_is_full():
    state = make_state()
    stances = all_agree()
    stances["gpt5"] = stance("gpt5", {"claude": "disagree", "kimi": "agree"})
    report = commit(state, 0, stances).consensus
    assert StanceMatrix().decide_outcome(report, rounds_left=0) is Outcome.EXHAUSTED


def test_min_coverage_threshold_is_configurable_not_hardcoded():
    """The quorum is not hard-coded in the engine — coverage is handed to the caller and the bar is a
    setting.
    """
    state = make_state()
    stances = all_agree()
    stances["kimi"] = Stance.as_unknown("kimi", 0)
    report = commit(state, 0, stances).consensus
    assert (
        StanceMatrix().decide_outcome(report, rounds_left=0) is Outcome.PARTIAL_COVERAGE_CONSENSUS
    )
    strict = StanceMatrix(min_coverage=0.9)
    assert strict.decide_outcome(report, rounds_left=0) is Outcome.EXHAUSTED


def test_a_plain_agreement_with_a_reason_stays_a_plain_agreement():
    """Explaining "why I agree" is entirely normal and must not be counted as a reservation.

    A measured overcorrection: to align the two extraction paths I treated every `reason` as
    residuals, so "your QPS estimate matches mine, I fully agree" was recorded as an **open
    reservation** — manufacturing disagreement out of nothing.
    """
    from sesa.consensus.stance import parse_stance, parse_verdict_lines

    card = (
        '```json\n{"position":"p","confidence":0.9,'
        '"stance_on":{"b":{"verdict":"agree","reason":"你的 QPS 估算和我一致"}}}\n```'
    )
    structured = parse_stance(card, "a", 1, ["b"]).stance_on["b"]
    lined = parse_verdict_lines("b: agree | 你的 QPS 估算和我一致", "a", 1, ["b"]).stance_on["b"]

    assert structured.verdict == "agree"
    assert structured.residuals == []
    assert lined.verdict == "agree", "the two extraction paths have to give the same judgement"


def test_an_agreement_carrying_a_reservation_is_downgraded_on_both_paths():
    """Agreement with reservations is not agreement without — whichever extraction path it came in on."""
    from sesa.consensus.stance import parse_stance, parse_verdict_lines

    card = (
        '```json\n{"position":"p","confidence":0.9,'
        '"stance_on":{"b":{"verdict":"agree","reason":"同意，但我仍不接受成本估算"}}}\n```'
    )
    structured = parse_stance(card, "a", 1, ["b"]).stance_on["b"]
    lined = parse_verdict_lines("b: agree | 同意，但我仍不接受成本估算", "a", 1, ["b"]).stance_on[
        "b"
    ]

    assert structured.verdict == "partial" == lined.verdict
    assert structured.residuals, (
        "a reservation has to be registered, or it counts as neither disagreement nor reservation"
    )


def _blockers_about_confidence(stances: dict, ids: str = "ab") -> list[str]:
    state = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ids],
        max_rounds=2,
    )
    record = RoundRecord(0)
    record.stances = stances
    state.rounds = [record]
    return [b for b in StanceMatrix().assess(state).blockers if "confidence" in b]


def test_participants_who_never_submitted_are_not_blamed_twice():
    """Those who handed in nothing were already reported under "stance could not be parsed" and must
    not be counted again under "did not report a confidence".

    A measured self-contradictory output: of three people only one handed in a card and filled in a
    confidence, the other two's cards did not parse, and it reported "**0** participants did not
    report a confidence".
    """
    got = _blockers_about_confidence(
        {
            "a": Stance("a", 0, confidence=0.9, stance_on={"b": StanceOn("agree")}),
            "b": Stance.as_unknown("b", 0),
            "c": Stance.as_unknown("c", 0),
        },
        "abc",
    )

    assert got == [], f"those who handed in nothing were accused twice: {got}"


def test_a_partial_report_of_confidence_says_how_many_are_missing():
    got = _blockers_about_confidence(
        {
            "a": Stance("a", 0, confidence=0.9, stance_on={"b": StanceOn("agree")}),
            "b": Stance("b", 0, stance_on={"a": StanceOn("agree")}),
        }
    )

    assert got == ["1 participants reported no confidence, so how sure they are cannot be judged"]


def test_nobody_reporting_confidence_is_said_plainly():
    got = _blockers_about_confidence(
        {
            "a": Stance("a", 0, stance_on={"b": StanceOn("agree")}),
            "b": Stance("b", 0, stance_on={"a": StanceOn("agree")}),
        }
    )

    assert got == [
        "no participant reported a confidence, so there is no reading of how sure anyone is"
    ]
