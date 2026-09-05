"""git_worktree — the workspace for code tasks: one isolated worktree per participant.

**Why isolation is required**: several agents editing one repository at once trample
each other, and afterwards nobody can tell who changed what. With a worktree each:

* everyone's changes are naturally separate and can be ``git diff``-ed one by one
* **every party's branch is kept** — an implementation that was not adopted carries the
  minority opinion, and deleting it erases the disagreement (see DESIGN.md §7.3)
* A's tests can be run against B's implementation (cross-testing), which is the key
  defence against everyone testing only themselves

**Safety**: refuses to run on a repository with uncommitted changes. Code tasks usually
come with switches like ``--dangerously-skip-permissions``, so the user's unsaved work
must be banked first.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..i18n import t
from .base import Checkout, Workspace


class GitError(RuntimeError):
    """A git operation failed. The message carries git's own output, to make it locatable."""


def _git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired as exc:
        # A stuck git (a lock file, a network filesystem, a hook) raises TimeoutExpired, not
        # GitError — and every call site catches GitError, so it propagates all the way up and kills
        # the deliberation, when every caller had a degraded path available.
        raise GitError(
            t(
                "git {cmd} did not finish within 120s (repository locked? a hook hanging?)",
                cmd=" ".join(args),
            )
        ) from exc
    except OSError as exc:
        raise GitError(
            t("git {cmd} could not be executed: {exc}", cmd=" ".join(args), exc=exc)
        ) from exc
    if result.returncode != 0:
        raise GitError(
            t(
                "git {cmd} failed: {why}",
                cmd=" ".join(args),
                why=(result.stderr or result.stdout).strip()[:400],
            )
        )
    return result.stdout.strip()


class GitWorktreeWorkspace(Workspace):
    name = "git_worktree"

    def __init__(self, repo: Path, run_id: str, root: Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.run_id = run_id
        self.root = Path(root) if root else None
        self._temp: Path | None = None
        self._created: list[Checkout] = []

    # ------------------------------------------------------------------ #

    def assert_clean(self) -> None:
        """Refuse to run on a repository with uncommitted changes.

        In a code task the participants really do write files and run commands, usually with
        auto-approval on. The user's unsaved work has to be banked first — this is not fastidious,
        it is an irreversible risk.
        """
        if not (self.repo / ".git").exists():
            raise GitError(
                t(
                    "{path} is not a git repository. Code tasks need git to keep each "
                    "party's changes apart.",
                    path=self.repo,
                )
            )
        dirty = _git(["status", "--porcelain"], self.repo)
        if dirty:
            count = len(dirty.splitlines())
            raise GitError(
                t(
                    "the repository has {n} uncommitted changes; refusing to run.\n"
                    "The participants really do modify files — commit or stash first:\n",
                    n=count,
                )
                + dirty[:300]
            )

    def base_revision(self) -> str:
        return _git(["rev-parse", "HEAD"], self.repo)

    def branch_name(self, participant: str) -> str:
        return f"sesa/{self.run_id}/{participant}"

    # ------------------------------------------------------------------ #

    def prepare(self, participants: list[str]) -> dict[str, Checkout]:
        self.assert_clean()
        base = self.base_revision()
        if self.root is None:
            self._temp = Path(tempfile.mkdtemp(prefix=f"sesa-{self.run_id}-"))
            self.root = self._temp

        out: dict[str, Checkout] = {}
        for pid in participants:
            path = self.root / pid
            branch = self.branch_name(pid)
            _git(["worktree", "add", "-b", branch, str(path), base], self.repo)
            checkout = Checkout(participant=pid, path=path, branch=branch, revision=base)
            out[pid] = checkout
            self._created.append(checkout)
        return out

    def revision_of(self, checkout: Checkout) -> str | None:
        """The worktree's current HEAD plus a fingerprint of the working tree.

        Participants often **edit files without committing**, so HEAD alone is not enough — the
        uncommitted changes have to count too, or staleness checks on evidence stop working.
        """
        try:
            head = _git(["rev-parse", "HEAD"], checkout.path)
            # `-uall` expands untracked **directories**. The default `--porcelain` prints only `??
            # newdir/` — no sign of what the files inside became, and a participant creating new
            # implementation files is the commonest case of all.
            dirty = _git(["status", "--porcelain", "-uall"], checkout.path)
        except GitError:
            return None
        if not dirty:
            return head
        # Python's hash() carries a per-process random salt and is not stable across processes — a
        # fingerprint has to be reproducible, or the same change gets a different "revision" on two
        # runs and staleness checks on evidence stop working with it.
        # And hashing `status --porcelain` alone is not enough: it holds only status codes and file
        # names. **A participant editing one file twice leaves an identical status line, and an
        # identical fingerprint** — so "has the code changed" fails in the commonest case. `diff` is
        # what carries the content; untracked files are invisible to diff and are hashed separately.
        try:
            diff = _git(["diff", "HEAD"], checkout.path)
        except GitError:
            # **It must not fall back to an empty string.** The comment above just explained why the
            # status lines are not enough: one file edited twice leaves an identical status line.
            # Falling back to them when diff is unavailable weakens the fingerprint, exactly when it
            # is most needed, into the form already known to be insufficient — and leaves no trace
            # of having done so. Better to say "the revision cannot be measured" (None) than to hand
            # back a fingerprint that looks fine and cannot tell changes apart.
            return None
        untracked = _untracked_digest(checkout.path, dirty)
        digest = hashlib.sha1(f"{dirty}\0{diff}\0{untracked}".encode()).hexdigest()[:12]
        return f"{head}+dirty:{digest}"

    def commit_all(self, checkout: Checkout, message: str) -> str | None:
        """Turn a participant's changes into a commit, so the branch can be checked out and diffed."""
        try:
            if not _git(["status", "--porcelain"], checkout.path):
                return None  # A clean tree: nothing to commit, which is a successful no-op
            _git(["add", "-A"], checkout.path)
            _git(["commit", "-m", message, "--no-verify"], checkout.path)
            return _git(["rev-parse", "HEAD"], checkout.path)
        except GitError:
            # **A failed commit must not share a return value with "nothing to commit".** With both
            # returning None, the caller cannot tell "this participant changed nothing this round"
            # from "not one word of their changes survived" — and since the branches are part of the
            # deliverable, the latter means a minority opinion was lost and the layer above has to
            # know.
            raise

    def cleanup(self) -> None:
        """Remove the worktree directory but **keep the branch**.

        The branch is a deliverable: an implementation that was not adopted carries the minority
        opinion, and deleting it erases the disagreement.
        """
        for checkout in self._created:
            try:
                _git(["worktree", "remove", "--force", str(checkout.path)], self.repo)
            except GitError:
                shutil.rmtree(checkout.path, ignore_errors=True)
                # Deleting only the directory leaves a ghost registration under
                # .git/worktrees/<name>: `git worktree list` still shows it, and the branch may
                # still be marked as checked out, so checking that branch out later fails outright.
                # prune clears every registration pointing at a directory that is gone.
                # Suppressed on purpose: we did what we could, and raising here would only
                # bury the real cause of the failure.
                with contextlib.suppress(GitError):
                    _git(["worktree", "prune"], self.repo)
        self._created.clear()
        if self._temp and self._temp.exists():
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None

    def describe(self) -> str:
        return f"{self.name}({self.repo})"


def _untracked_digest(root: Path, porcelain: str) -> str:
    """Content fingerprint of the untracked files. ``git diff`` cannot see them."""
    digest = hashlib.sha1()
    for line in sorted(porcelain.splitlines()):
        if not line.startswith("?? "):
            continue
        path = root / _unquote(line[3:].strip())
        try:
            digest.update(path.read_bytes() if path.is_file() else b"")
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _unquote(path: str) -> str:
    """Undo the quoting and C-style escaping git porcelain applies to a path.

    A file name containing spaces or non-ASCII is written by git as `"a b.py"` /
    `"\346\265\213.py"`. Stripping the quotes is not enough — the escape sequences leave the
    path not matching anything, so that file's content is never read and the fingerprint
    never sees it change.
    """
    if not (path.startswith('"') and path.endswith('"')):
        return path
    # The octal escapes are encoded **per byte** (one Chinese character = three escapes like \346),
    # so they must first be restored to a byte sequence and then decoded as UTF-8. `unicode_escape`
    # treats each byte as one latin-1 character and decodes to mojibake, so the path lands somewhere
    # that does not exist, the file's content is never read, and the fingerprint never sees it
    # change — and Chinese file names are common in this project's examples, task briefs and
    # participant output.
    raw, i = bytearray(), 1
    body = path[1:-1]
    while i - 1 < len(body):
        ch = body[i - 1]
        if ch != "\\":
            raw.extend(ch.encode("utf-8"))
            i += 1
            continue
        nxt = body[i : i + 1]
        if nxt and nxt in "01234567":
            raw.append(int(body[i : i + 3], 8))
            i += 4
        else:
            raw.extend({"n": b"\n", "t": b"\t", "r": b"\r"}.get(nxt, nxt.encode("utf-8")))
            i += 2
    return raw.decode("utf-8", "replace")
