"""ephemeral — the workspace for text topics: a temp directory, touching no repository.

Text topics need no file isolation, but the participants (agent CLIs especially) still
need a writable cwd, or they will scribble in the user's repository.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..i18n import t
from .base import Checkout, Workspace


class EphemeralWorkspace(Workspace):
    name = "ephemeral"

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else None
        self._temp: Path | None = None

    def prepare(self, participants: list[str]) -> dict[str, Checkout]:
        if self.root is None:
            self._temp = Path(tempfile.mkdtemp(prefix="sesa-")).resolve()
            self.root = self._temp
        else:
            # Store resolved paths throughout. On macOS /var is a symlink to /private/var, and
            # mixing the two forms makes "is this directory inside the workspace" true sometimes and
            # false other times.
            self.root = Path(self.root).resolve()
        out = {}
        root = self.root.resolve()
        for pid in participants:
            # `root / pid` **discards root entirely** for an absolute path (pathlib's semantics),
            # and `..` climbs out one level at a time — a participant id comes from a config file
            # and must not have the power to decide where the workspace lands. Measured: with
            # pid="/etc" the workspace was /etc.
            path = (root / pid).resolve()
            if not path.is_relative_to(root) or path == root:
                raise ValueError(
                    t(
                        "this participant id would put the working directory outside the "
                        "workspace: {pid} → {path}. Use a plain name, not a path.",
                        pid=repr(pid),
                        path=path,
                    )
                )
            path.mkdir(parents=True, exist_ok=True)
            out[pid] = Checkout(participant=pid, path=path)
        return out

    def cleanup(self) -> None:
        # Clean up only temp directories we created ourselves; never touch a directory the user
        # named
        if self._temp and self._temp.exists():
            shutil.rmtree(self._temp, ignore_errors=True)
            # root must be cleared along with it. Left pointing at a deleted path, the next
            # prepare() sees `self.root is not None`, skips creating a new temp directory, and
            # rebuilds under the **deleted old path**; by then `_temp` is None, so cleanup() can
            # never remove it — and the temp directory leaks for good.
            self._temp = None
            self.root = None
