"""The verification results for the high-severity items of the full scan (all of src/, 70
findings).

**Verified one by one, not accepted wholesale.** Some hold, some point the wrong way, some
were already fixed. What is kept below are the ones verified as real and since fixed.
"""

from __future__ import annotations

import pytest

from sesa.credentials import CredentialError, _redact, resolve_api_key
from sesa.evidence.runner import CrossTestMatrix
from sesa.report import render_result
from sesa.types import Outcome, ParticipantSpec, Result


def test_an_error_never_echoes_something_that_might_be_a_real_key():
    """`CredentialError`'s docstring promises "the key itself is never echoed".

    And echoing rests on "I judge that this is not a key" — **and the judgement can be wrong**:
    the rule is `^[a-zA-Z_][a-zA-Z0-9_-]*:`, and some providers' keys are exactly of the form
    `user:token`, which would be printed into the error in full.
    **An absolute promise cannot be kept by a judgement that can be wrong.**
    """
    secret = "user:aVeryLongRealSecretToken123456"
    spec = ParticipantSpec(id="x", adapter="openai_compat", options={"api_key": secret})

    with pytest.raises(CredentialError) as caught:
        resolve_api_key(spec)

    assert secret not in str(caught.value), "the plaintext key appeared in the error"
    assert "35 characters in total" in str(caught.value), (
        "but it has to give enough of a clue to locate the problem"
    )


def test_redaction_keeps_enough_to_spot_a_typo():
    """A real-world typo is still visible: `keyring:x` is 9 characters, and its first 6 are given as
    usual.
    """
    assert "keyrin" in _redact("keyring:x")


def test_redaction_never_echoes_even_a_short_value():
    """A short value is not echoed in full either — a stated trade-off, not a default.

    It used to `repr` anything ≤6 characters verbatim, on the grounds that "nothing that short
    can be a key". But this function is called redact, and its promise is "nothing leaks", not
    "nothing leaks when I think it is safe":

    * the judgement can be wrong — a truncated key, a short passphrase, a provider's short token
    * being wrong means **writing a credential into a log**, while the loss in debugging
      convenience is small

    When the costs are asymmetric, stand on the side of not leaking.
    """
    for value in ("abc", "sk-123", "x"):
        redacted = _redact(value)
        assert value not in redacted, f"{value!r} was echoed verbatim"
        assert str(len(value)) in redacted, (
            "it has to say at least how long it is, or even a typo cannot be located"
        )


def test_the_private_assumption_signal_can_actually_fire():
    """ "Their tests pass only for themselves" needs the diagonal, and cross_test runs only off the
    diagonal.

    `only_own_tests_pass` used to be **dead code** — not one call site in the whole codebase, so
    this signal was never asked for. Not "always False", simply never asked.
    """
    matrix = CrossTestMatrix(command="pytest", results={("a", "b"): 1, ("b", "a"): 0})

    got = matrix.suspicious_testers({("a", "a"): 0, ("b", "b"): 0})

    assert got == ["a"], (
        "a's tests pass for a and for nobody else — exactly the signal that should be reported"
    )


def test_a_matrix_without_self_tests_cannot_answer_the_question():
    """Without the diagonal it must not pretend to have an answer — that would silently disable the
    signal.
    """
    matrix = CrossTestMatrix(command="pytest", results={("a", "b"): 1})

    assert matrix.suspicious_testers({}) == []
    assert matrix.only_own_tests_pass("a") is False


def test_the_deliverable_warns_that_a_green_test_may_be_self_serving():
    result = Result(
        run_id="r1",
        task="t",
        outcome=Outcome.CONSENSUS,
        suspicious_testers=["alice"],
    )

    text = render_result(result)

    assert "Only alice's own implementation passes alice's tests" in text
    assert "tests encode assumptions that hold only for their author" in text


async def test_anthropic_also_reports_truncation(monkeypatch):
    """The two adapters must give "the reply was truncated" the same semantics.

    `openai_compat` had been reading `finish_reason` for ages while `anthropic` never read
    `stop_reason` — switching provider switches failure semantics, and a truncated reply looks
    exactly like a complete one.
    """
    import httpx

    from sesa.adapters.anthropic import AnthropicAdapter
    from sesa.types import Done

    body = (
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"写到一半"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
        '"usage":{"output_tokens":8}}\n\n'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    adapter = AnthropicAdapter(
        ParticipantSpec(id="c", adapter="anthropic", model="m", options={"api_key": "k" * 20})
    )

    chunks = [c async for c in adapter.stream("写点东西")]

    assert isinstance(chunks[-1], Done)
    assert chunks[-1].truncated, "stop_reason=max_tokens has to be flagged as truncated"


def test_nobody_reporting_confidence_blocks_the_downgrade_paths():
    """Not one person reporting a confidence = the bar cannot be met, and must not count as passing.

    It used to be `if confidences_known and min < threshold` — with nobody reporting, the whole
    thing short-circuits, treating "cannot be measured" as "passed", the opposite of default-deny.
    """
    from sesa.consensus.matrix import StanceMatrix
    from sesa.types import ConsensusReport

    report = ConsensusReport(
        round=1,
        matrix={"a": {"b": "partial"}},
        min_confidence=0.0,
        converged=False,
        stalled_rounds=0,
        confidences_known=0,
        expected_confidences=2,  # both handed in a usable stance card and neither filled in
        # a confidence
        agreed=0,
        reservations=1,
        residuals={"a→b": ["x"]},
        coverage=1.0,
    )

    got = StanceMatrix().decide_outcome(report, rounds_left=0)

    assert got not in (
        Outcome.CONSENSUS,
        Outcome.CONSENSUS_WITH_RESERVATIONS,
        Outcome.PARTIAL_COVERAGE_CONSENSUS,
    ), f"nobody reported a confidence and it is judged {got}"


@pytest.mark.parametrize(
    "turn_taking,should_see",
    [("sequential", True), ("parallel", False)],
)
def test_sequential_actually_lets_later_speakers_see_earlier_ones(turn_taking, should_see):
    """`turn_taking: sequential` used to be **sequential in name only**.

    The comment said "a later speaker can see the earlier ones this round", while the prompt was
    rendered inside `plan()`'s loop — everyone got the same one, and what the earlier speaker
    said could not get in. Changed to deferred rendering.

    The parallel half matters just as much: everyone speaks at once and nobody should see
    anybody — **independence is the only source of diversity**. While editing this I once let
    parallel see it too, which quietly turned parallel into sequential.
    """
    from sesa.protocols import build
    from sesa.state import DeliberationState, RoundRecord, Turn

    state = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=i, adapter="cli") for i in ("a", "b")],
        max_rounds=3,
    )
    first_round = RoundRecord(0)
    first_round.turns = [Turn("a", 0, 0, "draft", "A 的初稿"), Turn("b", 0, 0, "draft", "B 的初稿")]
    state.rounds = [first_round]

    moves = [
        m for phase in build("debate", turn_taking=turn_taking).plan(state) for m in phase.moves
    ]
    state.rounds.append(RoundRecord(1))
    moves[0].render(state)
    state.rounds[-1].turns.append(Turn(moves[0].participant, 1, 0, "revise", "【刚说的话】"))

    assert ("【刚说的话】" in moves[1].render(state)) is should_see
