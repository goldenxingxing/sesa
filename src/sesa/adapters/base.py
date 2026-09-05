"""The Adapter abstraction.

An Adapter answers "**how do I hand it the words, and how do I get the words back**",
which is orthogonal to "which model". The same model through different adapters is two
different participants — Claude through `cli` (with tools, able to read and write
files) does not behave like Claude through `anthropic` (plain text reasoning). That is
precisely the "agent × llm combination" axis.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from ..i18n import t
from ..types import Chunk, ParticipantSpec


@dataclass
class CheckResult:
    """The result of a ``sesa doctor`` / ``participants test`` check."""

    ok: bool
    detail: str
    latency_s: float | None = None
    #: The model the participant **claims** to be. ``None`` means it was not asked, or could not
    #: answer.
    #: **This is self-reported, not fact.** Models often get their own identity wrong — especially
    #: when wrapped or routed to another backend. It is still useful: a user's DeepSeek Harness was
    #: in fact running Kimi K3, and the participant table showed only a "—", so it stayed hidden for
    #: a long time. **A self-report can expose that kind of mismatch.**
    reported_model: str | None = None


def _reported_model(text: str) -> str | None:
    """Pull ``MODEL=<id>`` out of the probe reply.

    Returns ``None`` when it cannot — **never passing the whole reply off as a model name**.
    Plenty of CLIs answer with a paragraph of pleasantries, and putting that in the "model"
    column has the reader take it for fact.
    """
    import re

    found = re.search(r"MODEL\s*=\s*([^\s\n]+)", text)
    if not found:
        return None
    value = found.group(1).strip().strip("\"'`.,;")
    return value[:60] or None


class AdapterError(RuntimeError):
    """A failure the adapter layer expects (non-zero exit, HTTP 4xx/5xx, timeout, …)."""

    #: Whether this failure was a timeout. **Callers must test the field, not match on the message
    #: text** — the message follows the UI language, and one translation silently kills any branch
    #: that recognised it by substring.
    timed_out: bool = False

    def __init__(self, *args, timed_out: bool = False) -> None:
        super().__init__(*args)
        self.timed_out = timed_out


class Adapter(abc.ABC):
    """Base class for all adapters.

    An implementation need only care about :meth:`stream`; retries, budget and event
    emission above it are the Engine's job.
    """

    #: the name it is referred to by in the config file
    name: str = ""

    def __init__(self, spec: ParticipantSpec) -> None:
        self.spec = spec

    @property
    def id(self) -> str:
        return self.spec.id

    @abc.abstractmethod
    def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        cwd: Path | None = None,
        timeout: float = 600.0,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[Chunk]:
        """Stream :class:`TextDelta` and finish with exactly one :class:`Done`.

        It must end with ``Done`` even when usage is unavailable (use ``Usage.unknown()``).

        ``context`` holds template variables the engine provides (``round``, for one); an
        adapter may use them, and one that does not need them simply ignores them.
        """
        raise NotImplementedError

    async def check(self) -> CheckResult:
        """A light availability probe. The default implementation sends a very short message."""
        import time

        from ..types import Done, TextDelta

        started = time.perf_counter()
        got = ""
        finished = False
        try:
            probe = (
                "Reply with exactly one line in this form and nothing else:\n"
                "MODEL=<the model id you are running as>"
            )
            async for chunk in self.stream(probe, timeout=60):
                if isinstance(chunk, TextDelta):
                    got += chunk.text
                elif isinstance(chunk, Done):
                    finished = True
                    break
        except Exception as exc:
            return CheckResult(False, f"{type(exc).__name__}: {exc}")
        latency = time.perf_counter() - started
        if not got.strip():
            return CheckResult(False, t("the call succeeded but returned no content"), latency)
        reported = _reported_model(got)
        if not finished:
            # `Adapter.stream`'s contract is "it must end with Done". Reporting healthy on the
            # strength of having received some text means **the probe does not check the one
            # contract it exists to check** — and an adapter missing Done loses usage, the
            # truncation flag and the finish reason, which are exactly what the probe should be
            # catching.
            return CheckResult(
                False,
                t(
                    "the stream ended early with no Done: usage and the truncation flag "
                    "are both lost"
                ),
                latency,
                reported_model=reported,
            )
        return CheckResult(
            True, got.strip()[:60].replace("\n", " "), latency, reported_model=reported
        )
