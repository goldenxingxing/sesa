"""The Workspace abstraction: where the participants do their work.

Text topics and code tasks differ at exactly two seams (see DESIGN.md §6); this is the
first: **isolation**. Text topics touch no repository; code tasks must have each
participant edit inside their own git worktree, or they trample each other and nobody
can tell who changed what.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Checkout:
    """One participant's working copy."""

    participant: str
    path: Path
    #: a branch name for code tasks; None for text tasks
    branch: str | None = None
    #: the revision fingerprint at creation time, used to decide whether evidence has gone stale
    revision: str | None = None


class Workspace(abc.ABC):
    """Prepare working copies for the participants and hand them back afterwards."""

    name: str = ""

    #: Whether the participants **each have their own working copy**.
    #:
    #: It decides whether two things can be done at all:
    #:
    #: * **Adoption detection** — "who swapped their own work for a rival's". In a shared
    #: directory everyone's snapshot is identical by construction, so the comparison is
    #: meaningless.
    #: * **Cross-testing** — "run A's tests against B's implementation". In a shared
    #: directory
    #: there is only one implementation.
    #:
    #: Without isolation both must be **skipped explicitly and said so**, not computed
    #: anyway —
    #: computing costs time (measured: sharing a whole repository took the test suite from
    #: 150s to 850s) and produces an empty conclusion that looks normal.
    isolates_participants: bool = True

    @abc.abstractmethod
    def prepare(self, participants: list[str]) -> dict[str, Checkout]:
        """Prepare one working copy per participant."""

    def revision_of(self, checkout: Checkout) -> str | None:
        """The **current** revision fingerprint; ``None`` means this workspace cannot measure one.

        The default returns ``None`` rather than ``checkout.revision``, which was captured at
        ``prepare`` time and never moves however the code changes afterwards. Passing that off
        as "the current revision" makes every staleness check pass unconditionally: **a probe
        that always says "unchanged" is worse than no probe**, because it looks like it is
        working.

        If it cannot be measured, say so, and let the caller decide what to do (see
        ``EvidenceRecord.is_stale``).
        """
        return None

    def cleanup(self) -> None:
        """Wrap up. **Nothing is deleted by default** — everyone's artefacts are part of the
        deliverable.

        A subclass with nothing to clean up need not override this: the empty implementation is
        the correct behaviour, not an omission.
        """
        return

    def describe(self) -> str:
        return self.name
