"""Workspace registry: where the participants do their work.

* ``local``        — the directory you invoked from; everyone shares it (default)
* ``ephemeral``    — text topics: a temp directory, touching no repository
* ``git_worktree`` — code tasks: one isolated worktree each, branches always kept
"""

from __future__ import annotations

from .base import Checkout, Workspace
from .ephemeral import EphemeralWorkspace
from .local import LocalWorkspace
from .worktree import GitError, GitWorktreeWorkspace

__all__ = [
    "Checkout",
    "EphemeralWorkspace",
    "GitError",
    "GitWorktreeWorkspace",
    "LocalWorkspace",
    "Workspace",
]
