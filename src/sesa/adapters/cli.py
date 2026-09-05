"""The configuration-driven general CLI adapter.

This is the core bet of the project's usability: wiring up codex / gemini-cli / aider /
cursor-agent **takes no Python at all**, only a description in the config of how to start
the process.

```yaml
- id: codex
  adapter: cli
  command: ["codex", "exec", "--json"]
  prompt: stdin              # stdin | argv | argv_template
  cwd: "{workspace}"
  parse: jsonl               # raw | jsonl
  extract: "message.text"    # the path to pull out when parse=jsonl
  timeout: 600
  env: {CODEX_QUIET: "1"}
```

**The artefact-file channel** (recommended for agent CLIs): rather than hoping a CLI obeys
an stdout format strictly, agree on an artefact path, have it write its turn there, and
have the engine read only that file:

```yaml
  artifact: "{workspace}/.sesa-turns/{participant}-r{round}.md"
```

What this removes is not only JSON parse failures but **instruction drift** — commentary
smuggled in, the artefact written as a natural-language patch, tool logs mixed in. stdout
falls back to being a log, used for diagnosis only when the artefact is missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..i18n import t
from ..types import Chunk, Done, TextDelta, Usage
from .base import Adapter, AdapterError, CheckResult

#: Buffer cap for the subprocess streams. The default 64KiB is too small — one JSONL line from an
#: agent CLI is often longer.
STREAM_LIMIT = 16 * 1024 * 1024


def dig(obj: Any, path: str) -> Any:
    """Take a value by dotted path: ``message.text`` / ``choices.0.delta.content``."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _both_ends(text: str, head: int = 400, tail: int = 400) -> str:
    """For long output take **both ends**, not just the tail.

    Which end holds the answer cannot be known in advance:
    * claude's ``You've hit your session limit`` is at **the tail**
    * the cause of a Node crash (``does not provide an export named 'createZstdDecompress'``,
      pointing straight at too old a Node) is at **the head**, with the tail all stack frames

    This used to take the tail only, so in the second case the user got a screenful of
    ``at async ModuleJob.run`` while the one line explaining the cause was cut off. Since which
    end holds the answer is unknown, give both.
    """
    text = text.strip()
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return text[:head] + t("\n…({n} characters omitted in the middle)…\n", n=dropped) + text[-tail:]


class CliAdapter(Adapter):
    """Treat an agent CLI as a participant."""

    name = "cli"

    def __init__(self, spec) -> None:
        super().__init__(spec)
        opts = spec.options
        command = opts.get("command")
        if not command:
            raise ValueError(t("Participant {pid}: adapter=cli requires a command", pid=spec.id))
        if isinstance(command, str):
            command = command.split()
        self.command: list[str] = [str(c) for c in command]
        self.prompt_mode: str = opts.get("prompt", "stdin")
        self.parse: str = opts.get("parse", "raw")
        self.extract: str | None = opts.get("extract")
        self.cwd_template: str | None = opts.get("cwd")
        self.env_extra: dict[str, str] = {k: str(v) for k, v in (opts.get("env") or {}).items()}
        #: Backstop for the total time of one turn, defaulting to 1800s (30 minutes).
        #: Setting it high is **deliberate**: what really constrains cost is the run-wide wall-clock
        #: budget (``budget.max_wall_seconds``, and the engine takes the smaller of the two); this
        #: only asks "has this process hung". And killing by mistake is expensive — the whole turn
        #: is voided. Measured: ``kimi --quiet`` takes 660–900 seconds to finish a round normally,
        #: and the old default was 600 — **an agent CLI I use myself would be killed under the
        #: default configuration**. Someone wiring up a slower model only fares worse, so the
        #: backstop needs plenty of room.
        self.default_timeout: float = float(opts.get("timeout", 1800))
        #: How long it may go quiet **after it has started speaking** before being judged hung.
        #: Defaults to 300s.
        #: The test is not "how long since it spoke" but "**has it spoken at all**" — and that is
        #: crucial: some CLIs buffer throughout and emit everything at the end (``kimi --quiet``
        #: does exactly that), and for them "idle" and "working" are observationally identical.
        #: Applying an idle timeout to such a process kills it on **total** elapsed time — measured,
        #: kimi takes 660 seconds to finish a round normally, and the 300s idle default would kill
        #: it by mistake.
        #: So: **before the first chunk arrives, only the total time applies**, and the idle timeout
        #: starts after that. For a process that has never spoken we cannot tell slow from dead —
        #: that is a limit of what can be observed, and it is better to wait out the total time than
        #: to kill a participant that is still working.
        self.idle_timeout: float = float(opts.get("idle_timeout", 600))
        #: Some CLIs write informational messages to stderr, and those should not get mixed into the
        #: turn's prose
        self.stderr_is_output: bool = bool(opts.get("stderr_is_output", False))
        #: The artefact-file channel: an agreed path, with the engine reading only the file and
        #: stdout falling back to a log
        self.artifact: str | None = opts.get("artifact")
        #: Whether to fall back to stdout when the artefact is missing. Off by default — silent
        #: unreliability is exactly what this feature exists to eliminate
        self.artifact_required: bool = bool(opts.get("artifact_required", True))

    # ------------------------------------------------------------------ #

    def _argv(self, prompt: str, system: str | None) -> tuple[list[str], str | None]:
        """Return (argv, stdin_payload)."""
        full = f"{system}\n\n{prompt}" if system else prompt
        if self.prompt_mode == "stdin":
            return list(self.command), full
        if self.prompt_mode == "argv":
            return [*self.command, full], None
        if self.prompt_mode == "argv_template":
            return [arg.replace("{prompt}", full) for arg in self.command], None
        raise ValueError(t("unknown prompt mode: {mode}", mode=self.prompt_mode))

    def _artifact_path(self, cwd: Path | None, context: dict[str, str] | None) -> Path:
        workspace = Path(cwd) if cwd else Path.cwd()
        rendered = (
            self.artifact.replace("{workspace}", str(workspace))
            .replace("{participant}", self.id)
            .replace("{round}", (context or {}).get("round", "0"))
            .replace("{phase}", (context or {}).get("phase", "0"))
        )
        path = Path(rendered)
        return path if path.is_absolute() else workspace / path

    @staticmethod
    def _artifact_instruction(path: Path) -> str:
        return "\n\n---\n\n" + t(
            "**How to deliver: write your full turn to the file `{path}`.**\n\n"
            "The engine **reads only that file** as your turn — the prose and the final "
            "stance-card json block alike.\nstdout is treated as a log and is never taken "
            "as your turn. If the file is missing or empty, this round is recorded as a "
            "failure.\nMake sure the parent directory exists before you write.",
            path=path,
        )

    def _resolve_cwd(self, cwd: Path | None) -> str | None:
        if self.cwd_template:
            return self.cwd_template.replace("{workspace}", str(cwd or Path.cwd()))
        return str(cwd) if cwd else None

    def _keep(self, piece: str) -> None:
        """Keep the tail of stdout, used only to explain a failure."""
        self._stdout_seen.append(piece)
        if len(self._stdout_seen) > 40:
            del self._stdout_seen[:-20]

    def _why(self, stderr_text: str) -> str:
        """Give a human-readable reason when the process fails.

        stderr is often empty while the real explanation is in stdout — claude's
        ``You've hit your session limit · resets 4:20am`` is written only to stdout. Measured, 8
        deliberations were wasted for this reason, and all the event stream held was "exit code 1
        (stderr empty)", with no sign that it was a quota problem.
        """
        parts = []
        if stderr_text:
            parts.append(f"stderr：{_both_ends(stderr_text)}")
        if body := "".join(self._stdout_seen).strip():
            parts.append(f"stdout：{_both_ends(body)}")
        return "\n".join(parts) or t("(both stdout and stderr were empty)")

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[Chunk]:
        timeout = timeout or self.default_timeout
        # The adapter is reused across rounds, so the tail must be cleared each round or the
        # previous round's output ends up in this round's error message
        self._stdout_seen: list[str] = []

        artifact: Path | None = None
        if self.artifact:
            artifact = self._artifact_path(cwd, context)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            # A file of the same name left over from the previous round would be read as this
            # round's artefact — it has to be cleared first
            artifact.unlink(missing_ok=True)
            prompt += self._artifact_instruction(artifact)

        argv, stdin_payload = self._argv(prompt, system)

        if not shutil.which(argv[0]):
            raise AdapterError(t("command is not on PATH: {cmd}", cmd=argv[0]))

        env = {**os.environ, **self.env_extra}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            # asyncio's default line cap is 64KiB, and iterating by line over an over-long line
            # raises `Separator is found, but chunk is longer than limit` outright. An agent CLI
            # fitting a whole turn into one JSONL line is routine — measured, a single line of
            # 70,000 characters was enough to kill a turn, and 80,000 bytes of stderr had the engine
            # report "both stdout and stderr were empty".
            limit=STREAM_LIMIT,
            stdin=asyncio.subprocess.PIPE
            if stdin_payload is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._resolve_cwd(cwd),
            env=env,
        )

        stderr_buf: list[str] = []

        async def drain_stderr() -> None:
            # Read in chunks rather than by line: stderr needs no line semantics, and reading by
            # line fails entirely on one over-long line, turning "the process said a great deal"
            # into "both streams were empty".
            assert proc.stderr is not None
            while chunk := await proc.stderr.read(65536):
                stderr_buf.append(chunk.decode("utf-8", "replace"))

        stderr_task = asyncio.create_task(drain_stderr())

        async def pump() -> AsyncIterator[Chunk]:
            assert proc.stdout is not None
            if stdin_payload is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_payload.encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    # Some CLIs never read stdin at all, or exit immediately on error. That must not
                    # drown out the process's real cause of failure — keep reading stdout / stderr,
                    # and let the exit-code check below give a meaningful error.
                    pass

            if artifact is not None:
                # stdout falls back to a log: drain it so the pipe does not block, but do not take
                # it as the turn's output. Still keep a tail — when the process fails it is often
                # the only clue.
                while buf := await proc.stdout.read(4096):
                    self._keep(buf.decode("utf-8", "replace"))
            elif self.parse == "raw":
                while True:
                    buf = await proc.stdout.read(4096)
                    if not buf:
                        break
                    piece = buf.decode("utf-8", "replace")
                    self._keep(piece)
                    yield TextDelta(piece)
            elif self.parse == "jsonl":
                # Read in chunks and split on newlines ourselves, sidestepping StreamReader's line
                # cap. An agent CLI fitting a whole turn into one line is common, and a whole turn
                # should not be lost to it.
                pending = ""
                while chunk := await proc.stdout.read(65536):
                    pending += chunk.decode("utf-8", "replace")
                    if len(pending) > STREAM_LIMIT:
                        # Removing StreamReader's line cap must not remove the protection with it: a
                        # process that never emits a newline would grow this until memory runs out.
                        # Over the cap, emit what we have as one line — better ugly formatting than
                        # an OOM.
                        for piece in _from_jsonl(pending, self.extract):
                            self._keep(piece)
                            yield TextDelta(piece)
                        pending = ""
                        continue
                    *ready, pending = pending.split("\n")
                    for line in ready:
                        for piece in _from_jsonl(line, self.extract):
                            self._keep(piece)
                            yield TextDelta(piece)
                for piece in _from_jsonl(pending, self.extract):
                    self._keep(piece)
                    yield TextDelta(piece)
            else:
                raise ValueError(t("unknown parse mode: {mode}", mode=self.parse))

        # Two timeouts, governing two different things:
        #
        # * **The idle timeout** — how long with nothing emitted before it is judged hung.
        # This is
        # the real question: "slow but working" and "stuck" have to be told apart. Measured,
        # one
        # participant worked for 900 seconds, was killed by the total timeout, and was
        # recorded as
        # contributing 0 characters — while its working copy did in fact hold output.
        # * **The total time** — a backstop, itself bounded by the run's wall-clock budget
        # so it
        # cannot drag on forever.
        #
        # A CLI that does not stream (buffering throughout and emitting at the end) cannot
        # separate
        # the two, and for it the idle timeout equals the total — a limit of what can be
        # observed,
        # not something to pretend about.
        # **Once it has spoken, the total time is no longer a cap.**
        #
        # A participant that is steadily producing output should not be killed for "taking
        # long" —
        # tens of minutes or more on one round of a complex task is normal (measured:
        # document review
        # with tools, slowest turn 690s; and it could easily be longer). The old
        # `limit = min(idle, remaining)` made the total a hard cap, so a process **visibly
        # working**
        # was cut off, when being visibly at work is the direct evidence that it has not
        # hung.
        #
        # The total time falls back to the thing it should govern: **never having spoken**.
        # There,
        # slow and dead really are observationally identical (a fully-buffering CLI can stay
        # silent
        # for 1100s), and only this can catch it. Once it has spoken it is idle_timeout's
        # business —
        # "went quiet in mid-sentence" is what hung looks like.
        deadline = asyncio.get_running_loop().time() + timeout
        spoken = False
        try:
            stream = pump().__aiter__()
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if not spoken and remaining <= 0:
                    raise TimeoutError("total")
                limit = self.idle_timeout if spoken else remaining
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), timeout=limit)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    # Having spoken, it can only be the idle timeout; never having spoken, only the
                    # total. The errors differ, and getting it wrong sends the user hunting a hang
                    # that never happened.
                    raise TimeoutError("idle" if spoken else "total") from None
                spoken = True
                yield chunk
            # The same for the wind-down: a process that has finished speaking should not still be
            # under the total-time cap while we wait for it to exit.
            await asyncio.wait_for(
                proc.wait(),
                timeout=self.idle_timeout
                if spoken
                else max(1.0, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            # An error message must give **an actionable next step**. "Judged hung" is very likely a
            # lie: measured, kimi takes 660–900 seconds to finish a round normally while the default
            # timeout was 600 — a participant still working gets killed, and what the user sees is
            # "hung".
            if str(exc) == "total":
                hint = t(
                    "total time exceeded {cap:.0f}s. **It may simply be slow rather than "
                    "stuck{blind}** — if it genuinely needs longer, raise this "
                    "participant's `timeout` (currently {cap:.0f}s).",
                    cap=timeout,
                    blind=t(" (it produced no output at all, so the two cannot be told apart)")
                    if not spoken
                    else "",
                )
            else:
                hint = t(
                    "no new output for {idle:.0f}s after it started speaking, judged stuck. "
                    "If it legitimately goes quiet for long stretches (running a slow tool, "
                    "say), raise `idle_timeout` (currently {idle:.0f}s).",
                    idle=self.idle_timeout,
                )
            # `timed_out=True` is **the field the layer above tests**. The engine uses it to add
            # "the cap actually comes from the run-wide wall-clock budget"; it used to match on the
            # word "exceeded" in the error text, which one translation would silently kill.
            raise AdapterError(f"{self.id}: {hint}", timed_out=True) from None
        finally:
            stderr_task.cancel()
            # **It has to be really stopped.** stderr_buf is read immediately afterwards to assemble
            # the error message, and cancel() is only a request: the task may still be appending to
            # that list, so the same failure gives different errors on two runs — the hardest kind
            # to track down.
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

        stderr_text = "".join(stderr_buf).strip()
        if proc.returncode != 0:
            # Plenty of agent CLIs write the reason for failure to **stdout**: claude's "You've hit
            # your session limit · resets 4:20am" appears only there. Reporting "exit code 1 (stderr
            # empty)" alone throws away the answer we are holding — the person operating it would
            # never guess it was a quota problem from that sentence.
            raise AdapterError(
                t("{pid}: exit code {code}", pid=self.id, code=proc.returncode)
                + f"\n{self._why(stderr_text)}"
            )
        if artifact is not None:
            body = artifact.read_text(encoding="utf-8").strip() if artifact.exists() else ""
            if body:
                yield TextDelta(body)
            elif self.artifact_required:
                raise AdapterError(
                    t(
                        "{pid}: the artefact file {path} was never written. The process "
                        "exited (code {code}). ",
                        pid=self.id,
                        path=artifact,
                        code=proc.returncode,
                    )
                    + self._why(stderr_text)
                )
        elif self.stderr_is_output and stderr_text:
            yield TextDelta("\n" + stderr_text)

        # A CLI usually cannot report token counts — record honestly as unknown, with the wall clock
        # backing the budget.
        yield Done(Usage.unknown())

    async def check(self) -> CheckResult:
        if not shutil.which(self.command[0]):
            return CheckResult(False, t("command is not on PATH: {cmd}", cmd=self.command[0]))
        return await super().check()


def _from_jsonl(line: str, extract: str | None) -> list[str]:
    """Pull the prose to be emitted out of one JSONL line. A blank line returns an empty list."""
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return [line + "\n"]  # A line that is not JSON is emitted verbatim, which beats
        # dropping it
    piece = dig(obj, extract) if extract else obj
    if isinstance(piece, str):
        return [piece] if piece else []
    if extract is None:
        # With extract unset, piece is the whole JSON object rather than a string — so **every line
        # is dropped silently and the whole turn produces nothing**, while the configuration itself
        # is perfectly legal. Fall back to emitting verbatim: better one extra line of printed JSON
        # than dropping all the output without a word.
        return [line + "\n"]
    return []
