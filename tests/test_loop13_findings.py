"""Found by the 13th round of self-review (kimi + deepseek).

What made this round worth it is that **both parties really read the code**: the task file
carried the relevant source verbatim and both saw the same thing. In the previous run
deepseek had only the scan report and could do nothing but rubber-stamp the lot with "all 81
hold"; this time it found a cross-round hole that takes three rounds of state to construct.
"""

from __future__ import annotations

import pytest

from sesa.consensus.stance import _verification_from_note
from sesa.types import Verification

# ── the line table: the verdict errs in the wrong direction ─────────────────────── #


def test_an_honest_admission_is_not_turned_into_a_reproduction():
    """`"I cannot reproduce this locally"` is **an honest admission**.

    The earlier version was "say 'could not check' and it is unable; write anything else and it
    is executed/reproduced", and the hard-coded words do not match that sentence — so the
    admission was erased and a foundation appeared out of nowhere.
    """
    got = _verification_from_note("B", "我无法在本地复现")
    assert (got[0].how, got[0].result) == ("unable", "unable")
    assert not got[0].grounds_agreement


def test_a_vague_note_does_not_fabricate_grounds():
    """`"I had a look"` claims nothing and must not become "reproduced".

    Inferring reproduced from arbitrary text is manufacturing a measurement — the hole this
    project keeps falling into, and this time it fell into it inside the "the looseness is
    deliberate" defence I wrote for it.
    """
    got = _verification_from_note("B", "我看了一下")
    assert not got[0].grounds_agreement


def test_an_explicit_successful_check_still_grants_grounds():
    """Inverting the default must not disable the normal path with it — the line table is the
    degraded path for models that cannot handle JSON, and it still has to work.
    """
    got = _verification_from_note("B", "跑了他的 pytest，与其所述一致")
    assert got[0].grounds_agreement


@pytest.mark.parametrize(
    "note",
    ["跑不起来", "我无法复现", "没能验证", "unable to run it", "运行失败，与其所述不符"],
)
def test_phrasings_that_deny_success_never_grant_grounds(note):
    """The cost of being wrong is now "withheld" rather than "conjured": the first only makes them
    add a sentence, the second disables the bar entirely.
    """
    assert not _verification_from_note("B", note)[0].grounds_agreement


def test_the_original_wording_is_always_preserved():
    """A coarse test is bound to be wrong sometimes, so the original text is always kept for the
    reader to judge.
    """
    assert _verification_from_note("B", "我看了一下")[0].detail == "我看了一下"


# ── of is unchecked: the only defence is letting the reader see ─────────────────── #


def test_an_unrelated_claim_still_counts_as_grounds_and_that_is_why_it_must_be_shown():
    """kimi pointed out that ``Verification.of`` is unchecked, and an irrelevant sentence can serve
    as a foundation.

    **This is not fixed by validating `of`**: the whole ``verified`` block is self-reported, a
    participant says they ran it and no mechanism can prove it for them, and anyone determined
    to lie need only quote something the other really said. Only one thing can stop it —
    **letting the reader see what the foundation is**.
    """
    fabricated = Verification(
        of="天空是蓝的，与本议题无关", how="executed", result="reproduced", detail="随便写的"
    )
    assert fabricated.grounds_agreement, (
        "that is the behaviour; the next test says why it is acceptable"
    )


def test_the_grounds_of_every_agreement_reach_the_deliverable():
    """If the engine hands out a "foundation" on a self-report, it has to hand over the
    self-report.

    It used to speak only when verification was **missing** ("said agree but submitted no
    verification record") and say nothing at all when it was **present** — that is, when the
    consensus really did rest on those self-reports. Which builds the consensus on something the
    reader cannot see.
    """
    from sesa.report import render_result
    from sesa.types import Outcome, Result

    rendered = render_result(
        Result(
            run_id="x",
            task="审查",
            outcome=Outcome.CONSENSUS,
            coverage=1.0,
            rounds_used=2,
            verification_grounds={
                "a → b": ["executed/reproduced｜b 的主张｜跑了 pytest，与其所述一致"],
                "b → a": ["unable/unable｜a 的主张｜环境缺依赖，跑不动"],
            },
        )
    )
    assert "这些「同意」的地基" in rendered
    assert "跑了 pytest，与其所述一致" in rendered
    assert "环境缺依赖，跑不动" in rendered
    assert "全是自述" in rendered, "it has to say these records are confirmed by no mechanism"


def test_a_failed_check_is_recorded_as_refutation_not_as_unmeasured():
    """ "The run failed, it does not match what they said" is **counter evidence**, not an absence of
    measurement.

    My first version checked the negations after the affirmatives, so that sentence matched "ran"
    and was read as "reproduced" — reading a piece of counter evidence as supporting, precisely.
    Keyword matching on free text is unreliable in both directions, so the order has to make it
    err on the safe side: **negation first**.
    """
    got = _verification_from_note("B", "运行失败，与其所述不符")[0]
    assert (got.how, got.result) == ("executed", "refuted")
    assert not got.grounds_agreement


def test_the_note_parser_is_a_heuristic_and_says_so():
    """This path is bound to be wrong sometimes. Admit it, and put the cost on the safe side."""
    from sesa.consensus import stance

    doc = stance._verification_from_note.__doc__
    assert "coarse test" in doc
    assert "withheld" in doc, (
        "it has to say which way it errs when it is wrong, and why that is acceptable"
    )


# ── boundary 4, "could not check costs nothing", not honoured in the code ───────── #


def test_an_honest_unable_is_told_apart_from_saying_nothing():
    """deepseek pointed out that boundary 4 was not honoured, and they were right.

    I promised that "``how: unable`` (could not check, with the reason) is a respectable answer
    and costs nothing", and in the code someone who honestly filled in unable and someone who
    said nothing produced **exactly the same output**.

    The downgrade of the cell is right in itself — a failed verification cannot support an
    agreement, and that does not bend.
    What is wrong is the wording of the deliverable: telling someone who honestly gave their
    reason that they "submitted no verification record" **wrongs them**, and makes the promise
    empty.
    """
    from sesa.consensus.matrix import StanceMatrix
    from sesa.state import DeliberationState, EvidenceRecord, RoundRecord, Turn
    from sesa.types import ParticipantSpec, Stance, StanceOn

    ids = ["a", "b"]

    def _assess(verified):
        state = DeliberationState(
            task="t",
            participants=[ParticipantSpec(id=i, adapter="cli") for i in ids],
            max_rounds=2,
        )
        earlier = RoundRecord(0)
        earlier.turns = [Turn(p, 0, 0, "draft", "话") for p in ids]
        earlier.evidence = [EvidenceRecord(p, "pytest", 0, "ok", source="engine") for p in ids]
        state.rounds.append(earlier)
        later = RoundRecord(1)
        later.turns = [Turn(p, 1, 0, "revise", "话") for p in ids]
        later.stances = {
            "a": Stance(
                participant="a",
                round=1,
                confidence=0.9,
                stance_on={"b": StanceOn(verdict="agree", verified=verified)},
            )
        }
        state.rounds.append(later)
        return StanceMatrix().assess(state)

    silent = _assess([])
    honest = _assess(
        [Verification(of="b 的主张", how="unable", result="unable", detail="环境缺依赖，跑不动")]
    )

    # both cells are downgraded — that does not bend
    assert silent.matrix["a"]["b"] == honest.matrix["a"]["b"] == "unknown"
    # but they are booked to different accounts
    assert silent.unverified_agreements == ["a → b"]
    assert silent.unverifiable_agreements == []
    assert honest.unverified_agreements == []
    assert honest.unverifiable_agreements == ["a → b"]
    assert "said outright they could not check" in honest.describe_unresolved()


def test_the_deliverable_does_not_accuse_an_honest_participant():
    from sesa.report import render_result
    from sesa.types import Outcome, Result

    rendered = render_result(
        Result(
            run_id="x",
            task="审查",
            outcome=Outcome.EXHAUSTED,
            coverage=0.0,
            rounds_used=2,
            unmeasured_cells=["a → b"],
            unverifiable_agreements=["a → b"],
        )
    )
    assert "也说明了查不了" in rendered
    assert "没提交核验记录" not in rendered, "they did submit one"
    assert "该去解决的是他说的那个障碍" in rendered
