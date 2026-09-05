"""adversarial — the only protocol with asymmetric roles: one party proposes, the rest
attack full-time.

Where the proposal comes from decides the two ways of using it (see DESIGN.md §4.5):

* ``proposer: input``  — the thing under review is the task input itself (e.g.
  ``--file rfc.md``); there is no agent proposer, everyone attacks, and the last phase
  becomes a **cross-check**.
* ``proposer: rotate`` — a different proposer each round, so over N rounds everyone gets
  attacked once. The only unbiased choice.
* ``proposer: <id>``   — a named person. Legitimate but asymmetric: this is red-teaming
  one proposal, not picking the best option.

Whichever way it is used, **only an attack that was never successfully refuted reaches
the "open items" of the final report** — without the check/response step the protocol
degrades into "anyone may nitpick freely".

One full "propose → attack → respond" cycle is **one round** (three sequential phases),
so the proposer stays the same for the whole cycle and rotation happens between rounds.
"""

from __future__ import annotations

from .. import prompts
from ..i18n import t
from ..state import DeliberationState
from .base import Move, Phase, Protocol


class AdversarialProtocol(Protocol):
    name = "adversarial"

    def __init__(self, proposer: str = "rotate", **options) -> None:
        super().__init__(**options)
        self.proposer = proposer

    # ------------------------------------------------------------------ #

    def resolve_proposer(self, state: DeliberationState) -> str | None:
        """The id of this round's proposer; ``None`` means the proposal comes from the task input."""
        if self.proposer == "input":
            return None
        if self.proposer == "rotate":
            return state.rotate()
        if self.proposer in state.ids:
            return self.proposer
        raise ValueError(
            t(
                "proposer={value} is not a valid setting; use rotate, input, or one of "
                "the participant ids ({ids})",
                value=repr(self.proposer),
                ids=", ".join(state.ids),
            )
        )

    @staticmethod
    def _proposal_of(state: DeliberationState, proposer: str | None, task: str) -> str:
        """The proposal text. The proposal phase is inside this round, so take it from the
        **current** round.
        """
        if proposer is None:
            # proposer=input: the thing under review **is** the task input; this is a deliberate
            # setting.
            return task
        current = state.current
        if current:
            turn = current.latest_by(proposer)
            if turn and turn.ok:
                return turn.text
        # The proposal phase failed. **The task text must not be passed off as a proposal** — that
        # would have the attackers attack the question itself while being told it is someone's
        # proposal. Every attack would miss, and the report would not show that there was no
        # proposal at all.
        return t(
            "(the proposal phase failed: {pid} produced no proposal; the raw output is "
            "under turns/)\n\n**There is no proposal to review this round.** Do not "
            "attack out of thin air — say plainly that there is nothing to review, and "
            "name what is missing before this can go on.\n\nThe task text is **not** "
            "placed here: it is nobody's proposal, and putting it under a "
            "\u300cunder review\u300d heading would have you attack the question itself.",
            pid=proposer,
        )

    @staticmethod
    def _attacks_of(state: DeliberationState) -> str:
        current = state.current
        if not current:
            return t("(none)")
        blocks = [
            t("## Attack raised by {pid}", pid=turn.participant) + f"\n\n{turn.text.strip()}"
            for turn in current.turns
            if turn.kind == "attack" and turn.ok
        ]
        return "\n\n".join(blocks) if blocks else t("(none)")

    # ------------------------------------------------------------------ #

    def plan(self, state: DeliberationState) -> list[Phase]:
        injections = prompts.render_injections(state.pending_injections)
        proposer = self.resolve_proposer(state)
        task = state.task
        phases: list[Phase] = []

        # Phase 1: propose. Skipped when proposer=input — the proposal is the task input itself.
        if proposer is not None:
            phases.append(
                Phase(
                    t("Proposal: {pid}", pid=proposer),
                    [
                        Move(
                            proposer,
                            prompts.ROUND_ZERO.format(task=task, injections=injections),
                            kind="draft",
                        )
                    ],
                )
            )

        # Phase 2: everyone opens fire in parallel. The proposal text is rendered lazily, at
        # execution time.
        attackers = [pid for pid in state.ids if pid != proposer]
        phases.append(
            Phase(
                t("Attacks in parallel"),
                [
                    Move(
                        pid,
                        lambda st, _p=proposer: prompts.ATTACK.format(
                            proposal=self._proposal_of(st, _p, task), injections=injections
                        ),
                        kind="attack",
                        # The attack phase produces no stance card: there is no "position" yet, only
                        # accusations
                        expects_stance=False,
                    )
                    for pid in attackers
                ],
            )
        )

        # Phase 3: with a proposer, they answer point by point; otherwise the attackers cross-check
        # each other
        if proposer is not None:
            phases.append(
                Phase(
                    t("Response: {pid}", pid=proposer),
                    [
                        Move(
                            proposer,
                            lambda st, _p=proposer: prompts.REBUT.format(
                                proposal=self._proposal_of(st, _p, task),
                                attacks_block=self._attacks_of(st),
                                injections=injections,
                            ),
                            kind="rebut",
                        )
                    ],
                )
            )
        else:
            phases.append(
                Phase(
                    t("Cross-check"),
                    [
                        Move(
                            pid,
                            lambda st: prompts.CROSSCHECK.format(
                                proposal=self._proposal_of(st, None, task),
                                attacks_block=self._attacks_of(st),
                                injections=injections,
                            ),
                            kind="crosscheck",
                        )
                        for pid in state.ids
                    ],
                )
            )
        return phases
