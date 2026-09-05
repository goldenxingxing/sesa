"""council — an all-see-all round table.

What differs from debate is the **visibility**: the prototype's pairwise feeding
degrades once there are ≥3 participants (A sees B but not C). council guarantees that
everyone sees every turn and the full disagreement record.
"""

from __future__ import annotations

from .debate import DebateProtocol


class CouncilProtocol(DebateProtocol):
    name = "council"

    def __init__(self, **options) -> None:
        # All-see-all semantics require everyone to speak from the same snapshot, so turns are
        # forced to run in parallel.
        options.pop("turn_taking", None)
        super().__init__(turn_taking="parallel", **options)
