"""reflect — the no-speaker control group: everyone sees only their own last round,
and nobody sees anybody.

**This is the baseline for what debate achieves; it is not a way of deliberating.**

The literature (2026) gives two independent findings: simply "having a model think it
over again" produces about 37% change of position; and once a no-speaker control
condition is added to existing peer-pressure benchmarks, most of the measured
"conformity" is still there — which is to say **they over-attributed the change to
social influence**.

So the sentence "the debate changed X% of the participants' positions" **does not hold**
without this control group: how much of that X% would have changed anyway is unknown.

How to use it: same topic, same participants, same number of rounds, with ``protocol``
switched to ``reflect``, and then judge both runs the same way with ``sesa judge``.
**Only the change beyond this baseline can be attributed to the debate.**
"""

from __future__ import annotations

from .. import prompts
from ..i18n import t
from ..prompts import Template
from ..state import DeliberationState
from .base import Move, Phase, Protocol

REFLECT_ROUND = Template("""# Task

{task}

# Your answer from the last round

{previous}
{evidence_block}
# What to do

Look at your own last answer again and give your full position for this round.

**You cannot see anyone else's view — that is deliberate.** The point of this round is \
to make you think it through once more:

- Is there a claim you now think does not hold? If so, say it plainly and change it
- Is there a premise, a boundary case, or a counterexample you skipped last round?
- If the conclusion stands, restate it and say why it survives a second look

Changing your conclusion is not a loss of face. Holding a line to look consistent is.
{injections}""")


class ReflectProtocol(Protocol):
    """Each participant reflects on their own, seeing nobody, throughout."""

    name = "reflect"
    #: Nobody sees anybody ⇒ no peer assessment ⇒ consensus is not a meaningful question. Its
    #: outcome is not_measured, not exhausted — "not measured" and "not agreed" are two different
    #: things.
    measures_consensus = False

    def plan(self, state: DeliberationState) -> list[Phase]:
        injections = prompts.render_injections(state.pending_injections)

        if state.round_index == 0:
            return [
                Phase(
                    t("Independent drafts"),
                    [
                        Move(
                            pid,
                            prompts.ROUND_ZERO.format(task=state.task, injections=injections),
                            kind="draft",
                            # Never seeing the others means never being able to take a position on
                            # them — demanding a stance card anyway only forces invented judgements.
                            expects_stance=False,
                        )
                        for pid in state.ids
                    ],
                )
            ]

        previous = state.rounds[-1]
        return [
            Phase(
                t("Round {n}: self-review", n=state.round_index),
                [
                    Move(
                        pid,
                        REFLECT_ROUND.format(
                            task=state.task,
                            previous=(
                                turn.text.strip()
                                if (turn := previous.latest_by(pid))
                                else t("(none)")
                            ),
                            # Only their own execution results. Not seeing the others is the
                            # definition of this protocol; not seeing your own test results is
                            # simply a defect — that is not social information.
                            evidence_block=prompts.render_evidence(previous, only=pid),
                            injections=injections,
                        ),
                        kind="revise",
                        expects_stance=False,
                    )
                    for pid in state.ids
                ],
            )
        ]
