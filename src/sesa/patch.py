"""Extract file contents from model output and write them to disk.

**Why it is needed**: an API model can only produce text; it cannot write files. Without
this layer, code tasks would be limited to agent CLIs — while it is exactly the **weaker
models** most likely to show what debate is worth (the settings where the literature
finds a clear benefit are mostly weak models), and weaker models usually have only an
API.

**Why whole-file replacement and not a diff**: LLM-produced diffs go wrong on offsets
very easily, and they fail silently; a whole-file replacement is either right or
obviously wrong. This is the same reasoning behind DESIGN.md §4.4 choosing a full
rewrite over incremental edits.

The agreed form (stated to the participants in the prompt):

````
```python name=semver.py
<the entire contents of the file>
```
````
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

from .i18n import t
from .prompts import Template

#: A fence must occupy a line of its own; the info string names the target file with name= or path=
_FENCE = re.compile(
    # The attribute must be **a segment of its own**, not the tail of `filename=` /
    # `x-name=`.
    #
    # `\b` is not enough (`-` in `x-name=` is a non-word character, so \b holds); requiring
    # "preceded by start-of-line or whitespace" overcorrects — ```name=a.py, hard against
    # the
    # fence, is perfectly legal and commonly produced by models, and would be dropped for
    # being preceded by neither, **making the file vanish silently**.
    # The correct test is: preceded either by the fence itself or by whitespace.
    # Match **the opening line only**. The extent of the body is decided line by line by
    # `_body_until_close` — a non-greedy regex stops at the first ``` inside the file
    # content
    # (extremely common when writing Markdown or pasting nested code blocks), truncating the
    # file silently to half of it.
    r"^(?P<fence>`{3,})(?:[^\n]*?\s)?(?:name|path)\s*=\s*(?P<path>[^\s`]+)[^\n]*$",
    re.MULTILINE,
)

#: Whether a line is a fence line; a non-empty ``info`` means it carries an info string
#: (``` ```python ```).
_FENCE_LINE = re.compile(r"^(?P<ticks>`{3,})(?P<info>.*)$")


def _body_until_close(lines: list[str], start: int, fence: str) -> tuple[str, int]:
    """From line ``start``, take everything up to the line that closes ``fence``.

    Nesting is told apart by CommonMark's rule: **a fence line with an info string opens a
    nesting level, and only a bare fence (nothing but backticks apart from whitespace)
    closes one**. So a file like this — opening line ```` ```python name=example.py ````, a
    line with an info string ```` ```python ```` inside the body (opening a nested level), a
    bare ```` ``` ```` (closing the nested one), and finally a bare ```` ``` ```` closing the
    outer one — is read in full.

    With the earlier non-greedy regex it was truncated at **the first inner fence**. And the
    truncated content may still be valid code, so half a file was quietly written to disk as
    a whole one.

    Returns ``(body, index of the closing line)``; with no closing line the index is
    ``len(lines)``, and the caller treats it as truncated.
    """
    depth = 0
    body: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if (found := _FENCE_LINE.match(line)) and len(found.group("ticks")) >= len(fence):
            if found.group("info").strip():
                depth += 1  # with an info string ⇒ opens a nesting level
            elif depth:
                depth -= 1  # a bare fence ⇒ closes the nested level
            else:
                return "".join(body), index  # a bare fence and not nested ⇒ closes the
                # outer one
        body.append(line)
        index += 1
    return "".join(body), len(lines)


#: Character cap for injecting one file into the context. Too long and it squeezes out the
#: discussion itself.
MAX_FILE_CHARS = 20000

CONTEXT_HEADER = Template("""
---

# Your working directory (you cannot read or write files directly; here is what is in it)

""")

INSTRUCTION = Template("""

---

**You cannot write files directly. Output each file you want to change in full, in the \
format below** (the whole file, not a fragment):

```python name=thefile.py
<the entire contents of the file go here>
```

The engine writes it into your working directory and really runs the verification. \
Output only the files you actually mean to change.
""")


@dataclass
class AppliedFile:
    path: str
    bytes_written: int
    created: bool


@dataclass
class PatchResult:
    applied: list[AppliedFile]
    rejected: list[tuple[str, str]]  # (path, reason)

    @property
    def ok(self) -> bool:
        return bool(self.applied)


def extract_files(text: str) -> dict[str, str]:
    """Extract ``path -> file contents``. When one path appears more than once, **the last one
    wins**.

    The last one wins because models commonly give a fragment first and the complete version
    afterwards; taking the last is closer to their final intent.
    """
    out: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    starts = {}
    offset = 0
    for index, line in enumerate(lines):
        starts[offset] = index
        offset += len(line)

    for match in _FENCE.finditer(text):
        path = match.group("path").strip().strip("\"'")
        if not path:
            continue
        opening = starts.get(match.start())
        if opening is None:
            continue
        body, _ = _body_until_close(lines, opening + 1, match.group("fence"))
        out[path] = body
    return out


def _is_safe(root: Path, target: Path) -> bool:
    """The target must land inside the working directory. A model may well emit
    ``../../etc/passwd``.
    """
    try:
        return target.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def apply_files(text: str, root: Path, *, allow_new: bool = True) -> PatchResult:
    """Write the extracted files into ``root``. Anything out of bounds or otherwise illegal is
    rejected and reported honestly.
    """
    applied: list[AppliedFile] = []
    rejected: list[tuple[str, str]] = []

    for raw_path, body in extract_files(text).items():
        if Path(raw_path).is_absolute():
            rejected.append((raw_path, t("absolute path")))
            continue
        target = root / raw_path
        if not _is_safe(root, target):
            rejected.append((raw_path, t("escapes the working directory")))
            continue
        existed = target.exists()
        if not existed and not allow_new:
            rejected.append((raw_path, t("creating new files is not allowed")))
            continue
        # The target must be **a file under the root**, not the root itself.
        # `_is_safe(root, root)` is true (a path is its own descendant), so `name=.` passes
        # the
        # check while staging lands in the root's **parent** — outside the sandbox.
        # Measured: a
        # model writing ```name=. could delete any .sesa-partial file outside the workspace.
        if target.resolve() == root.resolve():
            rejected.append((raw_path, t("points at the working directory itself, not a file")))
            continue
        staging = target.with_name(target.name + ".sesa-partial")
        if not _is_safe(root, staging):
            rejected.append(
                (raw_path, t("the staging file would land outside the working directory"))
            )
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write a temp file and replace atomically: `write_text` truncates before writing, and a
            # failure part-way (disk full, quota) leaves a **truncated** original — which is worse
            # than not writing at all, because it looks like a successful change.
            staging.write_text(body, encoding="utf-8")
            staging.replace(target)
        except (OSError, ValueError) as exc:
            # Clear the half-written file, or the next scan reads it into the context as part of the
            # working copy.
            with contextlib.suppress(OSError, NameError, UnboundLocalError):
                staging.unlink(missing_ok=True)
            # A failed write must be **reported honestly**, not allowed to escape as an exception
            # and kill the whole turn. Measured: `name=.` raises IsADirectoryError; the paths models
            # produce are endlessly varied, and one bad path must not stop the other perfectly good
            # files in the same turn from landing.
            rejected.append((raw_path, t("write failed: {kind}", kind=type(exc).__name__)))
            continue
        applied.append(AppliedFile(raw_path, len(body.encode()), created=not existed))

    return PatchResult(applied=applied, rejected=rejected)


#: Files not worth injecting into the context: binaries, dependency directories, repository
#: internals
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
_SKIP_SUFFIXES = {".pyc", ".so", ".dylib", ".png", ".jpg", ".gif", ".pdf", ".zip", ".lock"}


def _walk(root: Path):
    """Walk the working directory, **without descending into what should be skipped**.

    ``rglob("*")`` walks into ``.venv`` / ``node_modules`` first and filters afterwards —
    30,000 entries under this repository's root, and this function runs once per turn.
    Once the default workspace became "the directory you started in", this path went from
    "scan an empty temp directory" to "scan the user's entire project", which took the test
    suite from 150s to 850s.

    A real user hit the same thing, and worse: running inside a large repository, merely
    listing the directory took a wait, and it could blow the prompt out entirely.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                # **Prune here**, rather than descending and filtering afterwards.
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def render_workspace(root: Path, max_files: int = 40) -> str:
    """Render the contents of the working directory into the context.

    **A participant that cannot write files cannot read them either.** Given only "implement
    what SPEC.md in the repository says", it can only guess — measured, two DeepSeek
    participants both stated plainly that "we have not seen the actual contents of SPEC.md",
    then each guessed an implementation, one of which "deliberately did not support" the
    ``^`` and ``~`` the spec explicitly required. That is not a lapse of judgement; it did
    not know what the spec asked for.
    """
    root = Path(root)
    blocks: list[str] = []
    for path in _walk(root):
        if len(blocks) >= max_files:
            blocks.append(t("\n(too many files; the rest are omitted)"))
            break
        rel = path.relative_to(root)
        if path.suffix in _SKIP_SUFFIXES:
            continue
        if not _is_safe(root, path):
            # A symlink can point outside the working copy. The write side has always had this check
            # and **the read side did not** — and what is read here goes straight into a model's
            # context.
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS] + t("\n…(truncated)")
        blocks.append(f"```text name={rel}\n{body}\n```")
    return CONTEXT_HEADER.format() + "\n\n".join(blocks) if blocks else ""


_ANY_FENCE = re.compile(r"^```", re.MULTILINE)


def count_fences(text: str) -> int:
    """How many code fences the output **opened**.

    It separates "there was no code to hand in" from "code was written but never handed in" —
    the two look identical in the working directory (nothing landed either way) and this
    count is the only thing that tells them apart.

    Count **opening fences**, not pairs. It used to be ``total // 2``, so a truncated output
    (the opening fence written, out of tokens before the close) came out as 0 — indistinguishable
    from "pure discussion, not one line of code". Measured: a participant wrote 28,000
    characters, most of it code, and the event stream recorded ``fences_seen=0``, making it
    look as though it had never intended to hand code over.
    """
    opens = 0
    for index, _ in enumerate(_ANY_FENCE.findall(text)):
        if index % 2 == 0:
            opens += 1
    return opens
