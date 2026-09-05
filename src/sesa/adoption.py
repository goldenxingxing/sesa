"""Detecting "someone lifted a rival's work wholesale".

The risk in a debate is not failing to converge; **it is converging on the wrong side**.

Measured background (DeepSeek ×2 × semver × 24 runs, in three batches, the third
pre-registered as confirmation): all three cells in the debate group that crossed the
similarity line lost points, one of them falling from 34/34 to 23/34 — the code it
handed in had similarity 0.97 to its rival's first draft and 0.08 to its own, inheriting
even the error messages verbatim. In all three cases the party copied from scored no
higher than the copier. In the reflect control group, where nobody sees anybody, this
metric peaked at 0.16 and copying is structurally impossible.

The full evidence and the limits on its strength are in DESIGN.md 14.18. **This module
reports facts and does not judge them good or bad** — that is answered by execution
evidence, not by similarity.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

#: The similarity floor for calling something a copy.
#: **Calibrated in exactly one setting**: DeepSeek ×2 on the semver task. The basis is the reflect
#: group, where nobody sees anybody — two independent first drafts measured 0.03 to 0.16 similarity,
#: the natural overlap of one model under one prompt, and 0.5 sits far above that baseline. A
#: different model, task or language needs recalibrating; do not carry this number over.
THRESHOLD = 0.5

#: Directories and suffixes excluded from a snapshot. Repository internals and binaries take no part
#: in the comparison.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".sesa"}
SKIP_SUFFIXES = {".pyc", ".so", ".dylib", ".png", ".jpg", ".gif", ".pdf", ".zip", ".lock"}

#: Size cap for comparing a single file. Beyond it, skip — line similarity on a large file means
#: nothing, and it slows every round down.
MAX_BYTES = 200_000


@dataclass
class Adoption:
    """What someone handed in this round looks more like their rival's last round than like
    their own.

    The premise is that **they had this file last round too**. Having nothing of your own
    and taking the other's is not copying — measured, the former happened 4 times and the
    latter 3, and only the latter came with a drop in score.
    """

    round: int
    participant: str
    adopted_from: str
    path: str
    similarity_to_peer: float
    similarity_to_own: float


@dataclass
class Report:
    """The detection result.

    ``measurable`` has to be kept apart from "nothing detected" — a run where it cannot be
    measured also returns an empty list, and without this flag it looks exactly like
    "measured, found nothing". This project has been caught four times by an empty value
    masquerading as data (see DESIGN.md 14.5, 14.17).
    """

    measurable: bool
    reason: str = ""
    events: list[Adoption] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.events)


Snapshot = dict[str, str]
"""One participant's working copy at the end of a round: relative path -> contents."""


def snapshot(root: Path) -> Snapshot:
    """Read the text files in a working copy into a snapshot.

    Reading the directory rather than extracting code blocks from the turns is what
    **covers agent CLIs** — they write their own files, and the code never appears in the
    turn at all. A code task already gives every participant their own git worktree, and
    this reads exactly that directory.
    """
    out: Snapshot = {}
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if set(rel.parts) & SKIP_DIRS or path.suffix in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            out[rel.as_posix()] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return out


def ratio(left: str, right: str) -> float:
    """Line-wise similarity. By line rather than by character — reflowed code structure is what
    gets copied.
    """
    # `autojunk=False` is required: with the default on, any element appearing in more than 1% of
    # the lines is treated as "junk" and excluded — and in code that is exactly ` return`, `}` and
    # blank lines. The larger the file, the more of the substance is quietly hollowed out,
    # similarity comes out systematically low, and **copy detection fails on the large files that
    # need it most**.
    return difflib.SequenceMatcher(
        None, left.splitlines(), right.splitlines(), autojunk=False
    ).ratio()


def detect(
    previous: dict[str, Snapshot],
    current: dict[str, Snapshot],
    *,
    round_index: int,
    threshold: float = THRESHOLD,
) -> list[Adoption]:
    """Compare the snapshots of two adjacent rounds and find the copies.

    ``previous`` and ``current`` are both participant -> snapshot.
    """
    found: list[Adoption] = []
    for pid, files in sorted(current.items()):
        for path, body in sorted(files.items()):
            own = previous.get(pid, {}).get(path)
            # `not own` conflates an **empty file** (``""``) with **no such file** (``None``). They
            # mean different things: the first is "they wrote an empty file last round", the second
            # is "they had nothing of their own". Only the second is the premise for "copying does
            # not apply".
            if own is None or body == own:
                # own missing = they had nothing of their own, which is not copying. body == own =
                # they did not touch this file at all this round.
                continue
            best: Adoption | None = None
            best_raw = 0.0
            to_own = ratio(body, own)  # independent of foe, so hoist it out of the inner
            # loop
            for foe, foe_files in sorted(previous.items()):
                if foe == pid:
                    continue
                theirs = foe_files.get(path)
                if not theirs:
                    continue
                to_peer = ratio(body, theirs)
                if (
                    to_peer > threshold
                    and to_peer > to_own
                    # Compare against the **unrounded** value: best stores round(x, 3), and using
                    # that as the baseline makes 0.9994 and 0.9996 indistinguishable.
                    and (best is None or to_peer > best_raw)
                ):
                    best_raw = to_peer
                    best = Adoption(
                        round_index, pid, foe, path, round(to_peer, 3), round(to_own, 3)
                    )
            if best is not None:
                found.append(best)
    return found
