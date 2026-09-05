"""local — **the directory you started in is the working directory**. This is the default.

Why not an isolated temp directory:

The earlier default was :class:`EphemeralWorkspace` — an empty temp directory per
participant, for the reason written in its own docstring: "participants still need a
writable cwd, otherwise they will scribble in the user's repository". The concern is
real, but what it bought was **worse**.

Measured, on the first real user's first deliberation: they ran, inside a directory of
documents,

    sesa run --tui "review the PRD in this folder, you may need the user-requirements
                    document in the same folder"

All three agent CLIs were placed in ``/var/folders/.../sesa-XXXX/<id>/`` — **empty
directories**. Not one of the six documents was visible. And the deliberation ran on,
the interface scrolled on, the deliverables were produced, and **nothing anywhere told
them that the three models were reviewing a document they had never seen**.

This is the same lesson this project already records in DESIGN — "the spec never
reached the participants, and the deliverable looked perfectly normal" — except that
here it was hiding in the **default configuration**, where every new user walks into it.

So the default is now this: **the participants work in the directory you typed the
command in**. Ask for isolation explicitly with ``--repo`` (a git worktree each,
branches kept).

The cost, stated plainly: **all participants share one directory and are not isolated
from each other**. For text topics they only read, which is fine; to have them each
write code, use ``--repo``.
"""

from __future__ import annotations

from pathlib import Path

from .base import Checkout, Workspace


class LocalWorkspace(Workspace):
    """Every participant shares the caller's current directory."""

    name = "local"
    #: **Not isolated**: everyone shares one directory. Adoption detection and cross-testing
    #: therefore do not hold.
    isolates_participants = False

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or Path.cwd()).resolve()

    def prepare(self, participants: list[str]) -> dict[str, Checkout]:
        # The same path for everyone — **that is what "not isolated" literally means**. Do not
        # quietly split it into subdirectories here; that would look like isolation while providing
        # none.
        return {pid: Checkout(participant=pid, path=self.root) for pid in participants}

    def describe(self) -> str:
        return f"{self.name}({self.root})"
