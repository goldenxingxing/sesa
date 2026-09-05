"""debate — propose → challenge each other → revise → repeat, until it converges or
stalls.

One Phase per round, everyone in parallel: the answering happens in the **next** round,
which is exactly why parallel turns lose no discussion quality (see DESIGN.md §4.2).
"""

from __future__ import annotations

from .. import prompts
from ..i18n import t
from ..state import DeliberationState
from .base import Move, Phase, Protocol


class DebateProtocol(Protocol):
    name = "debate"

    def __init__(self, turn_taking: str = "parallel", **options) -> None:
        super().__init__(**options)
        if turn_taking not in ("parallel", "sequential"):
            # Degrading silently to parallel is the worst handling: the user believes they
            # configured sequential and gets parallel, and the two have entirely different consensus
            # semantics. A misspelt letter must not quietly change behaviour.
            raise ValueError(
                t(
                    "turn_taking must be parallel or sequential; got {value}",
                    value=repr(turn_taking),
                )
            )
        self.turn_taking = turn_taking

    def plan(self, state: DeliberationState) -> list[Phase]:
        injections = prompts.render_injections(state.pending_injections)

        # Round 0 is always parallel independent drafts — independence is the only source of
        # diversity, and turn_taking does not affect it.
        if state.round_index == 0:
            return [
                Phase(
                    t("Independent drafts"),
                    [
                        Move(
                            pid,
                            prompts.ROUND_ZERO.format(task=state.task, injections=injections),
                            kind="draft",
                        )
                        for pid in state.ids
                    ],
                )
            ]

        previous = state.rounds[-1]
        deadlocked = bool(previous.consensus and previous.consensus.stalled_rounds > 0)
        share_thinking = state.thinking_is_shared(deadlocked)

        order = self._speaking_order(state, self.turn_taking)

        def build(pid: str):
            """**Deferred rendering**: in sequential mode a later speaker must be able to see what the
            earlier speakers said this round.

            This used to build the prompt inside the loop, which made "sequential" sequential in
            name only — everyone got the same prompt, what the earlier speaker said could not get
            in, and the comment still claimed "a later speaker can see the earlier ones this
            round". Reproduced in practice: the second speaker's prompt did not contain what the
            first had just said.
            """

            def render(st: DeliberationState) -> str:
                # `current` is this round, including whoever has just spoken; `previous` is the last
                # round. Sequential needs both; in parallel nobody has spoken this round yet, which
                # is equivalent to seeing only the last round. **Only sequential looks at this
                # round.** In parallel everyone speaks at once and nobody should see anybody —
                # independence is the only source of diversity. (While editing this I once let
                # parallel see it too, which quietly turned parallel into sequential.)
                same_round = (
                    prompts.render_same_round(st.current, exclude=pid)
                    if self.turn_taking == "sequential"
                    else ""
                )
                return prompts.DEBATE_ROUND.format(
                    task=st.task,
                    others_block=prompts.render_others(
                        previous, exclude=pid, share_thinking=share_thinking
                    )
                    + same_round,
                    consensus_block=prompts.render_consensus(
                        previous, exclude=pid, share_residuals=st.share_residuals
                    ),
                    evidence_block=prompts.render_evidence(previous, branches=st.branches),
                    thinking_block="",
                    injections=injections,
                )

            return render

        moves = [Move(pid, build(pid), kind="revise") for pid in order]

        if self.turn_taking == "sequential":
            # Sequential: one Phase per person, so a later speaker sees the earlier ones this round.
            return [Phase(t("Turn: {pid}", pid=m.participant), [m]) for m in moves]
        return [Phase(t("Round {n}: challenge and revision", n=state.round_index), moves)]
