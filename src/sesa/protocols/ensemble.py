"""ensemble — the cheapest protocol: one round, each drafting independently, **no peer
assessment**.

It runs only round 0, and round 0 is always independent drafts in parallel: nobody sees
anybody. So it **produces no disagreement matrix**, and there is nothing to call
"agreement" — the outcome is ``not_measured``, the drafts are presented side by side,
and a person picks.

It suits "give me a few independent views and I will decide". **If what you want is
"they reached agreement", this protocol cannot give it to you** — that needs at least
two rounds, so use ``debate`` or ``council``.

(This docstring used to describe "peer assessment via stance cards ... a rapporteur
writes it up once they agree", and the code never implemented peer assessment. The
documentation promised what the protocol could not do, and a user would take a
not_measured result and use it as consensus.)
"""

from __future__ import annotations

from .. import prompts
from ..i18n import t
from ..state import DeliberationState
from .base import Move, Phase, Protocol


class EnsembleProtocol(Protocol):
    name = "ensemble"

    def max_useful_rounds(self, state: DeliberationState) -> int:
        return 1

    def plan(self, state: DeliberationState) -> list[Phase]:
        if state.round_index > 0:
            return []
        injections = prompts.render_injections(state.pending_injections)
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
