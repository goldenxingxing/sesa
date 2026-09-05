"""The Protocol abstraction.

A Protocol decides only "**who speaks in which phase, and what they can see**".
Everything else — concurrent execution, stance extraction, consensus assessment,
budget, event emission — is the Engine's job, which is why each concrete protocol is
so short.

**Phases in sequence, moves within a phase in parallel**: `plan()` returns a list of
Phases, the Engine runs them in order, and the Moves inside a Phase run concurrently.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field

from ..state import DeliberationState


@dataclass
class Move:
    """One scheduled turn.

    ``prompt`` may be a string or a ``(state) -> str`` callable. The latter is **deferred
    rendering**: ``plan()`` is called before any phase of this round has run, while later
    phases usually need what earlier phases produced (the attack phase needs the proposal
    text, the response phase needs everyone's attacks), so those can only be rendered when
    they are about to execute.
    """

    participant: str
    prompt: str | Callable[[DeliberationState], str]
    kind: str = "statement"  # draft | revise | attack | rebut | crosscheck
    expects_stance: bool = True

    def render(self, state: DeliberationState) -> str:
        return self.prompt(state) if callable(self.prompt) else self.prompt


@dataclass
class Phase:
    """A group of Moves that run concurrently. Phases run in sequence."""

    label: str
    moves: list[Move] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.moves)


class Protocol(abc.ABC):
    """Base class for deliberation protocols."""

    name: str = ""
    #: Whether this protocol produces peer assessment between participants. ``reflect`` has nobody
    #: seeing anybody, so there is no peer assessment to speak of — its "unresolved" is not
    #: disagreement, it is unmeasured.
    measures_consensus: bool = True

    def __init__(self, **options) -> None:
        self.options = options

    @abc.abstractmethod
    def plan(self, state: DeliberationState) -> list[Phase]:
        """Lay out the phases for **the round about to start**. An empty list means this protocol
        has nothing more to schedule.

        **The execution contract** (the Engine must honour it, or rotation and deferred
        rendering both go out of step):

        1. When ``plan(state)`` is called, this round's ``RoundRecord`` has **not** been pushed
           yet, so ``state.round_index`` is the index of the round about to start and
           ``state.rounds[-1]`` is the previous round.
        2. The Engine then appends this round's ``RoundRecord`` to ``state.rounds``.
        3. Then it runs the Phases in order; the Moves inside a Phase run concurrently.
           :meth:`Move.render` is called only at that point, so it can see what the earlier
           phases of this round produced.
        """

    def max_useful_rounds(self, state: DeliberationState) -> int:
        """The protocol's own round limit, taken together with the configured max_rounds as a
        minimum.
        """
        return state.max_rounds

    # ------------------------------------------------------------------ # Small helpers for
    # subclasses ------------------------------------------------------------------ #

    @staticmethod
    def _speaking_order(state: DeliberationState, turn_taking: str) -> list[str]:
        """In parallel mode the order does not matter; in sequential mode it round-robins each
        round, cancelling out position bias.
        """
        ids = state.ids
        if turn_taking != "sequential" or not ids:
            return ids
        shift = state.round_index % len(ids)
        return ids[shift:] + ids[:shift]
