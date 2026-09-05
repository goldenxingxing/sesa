"""The event stream — the Engine's only outward contract.

The CLI, the TUI, the SDK, MCP and third-party products all consume the same stream.
The Engine knows nothing about terminals: it only yields Events, and rendering is the
consumer's business.

Every event's payload serialises losslessly to JSONL, so once on disk it can be replayed
and evaluated.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


def _plain(value: Any) -> Any:
    """Recursively reduce dataclasses and Enums to plain JSON-able structures."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass
class Event:
    """Base class of all events. Subclasses are distinguished by ``t``."""

    t: str = field(init=False, default="event")
    ts: float = field(default_factory=time.time, init=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        # Microseconds are enough: `time.time()` has only microsecond-level effective precision on
        # most platforms anyway, further digits are noise from the float representation, and the
        # event stream has to compare byte for byte (the premise of replay). **But the word
        # "lossless" applies to the payload, not to the timestamp** — the module docstring has been
        # corrected accordingly.
        data = {"t": self.t, "ts": round(self.ts, 6)}
        for key, value in self.__dict__.items():
            if key in ("t", "ts"):
                continue
            data[key] = _plain(value)
        return data


def _event(name: str):
    """A small decorator that pins ``t`` onto the subclass."""

    def wrap(cls):
        cls.t = name
        return cls

    return wrap


# --------------------------------------------------------------------------- # Lifecycle
# --------------------------------------------------------------------------- #


@_event("run.start")
@dataclass
class RunStart(Event):
    run_id: str
    task: str
    participants: list[str]
    protocol: str
    max_rounds: int


@_event("run.resume")
@dataclass
class RunResume(Event):
    """Resuming: a person adds one piece of information and the run continues from the deadlock."""

    from_run: str
    inject: str


@_event("round.start")
@dataclass
class RoundStart(Event):
    round: int
    rapporteur: str | None = None


# --------------------------------------------------------------------------- # Turns
# --------------------------------------------------------------------------- #


@_event("turn.start")
@dataclass
class TurnStart(Event):
    round: int
    participant: str


@_event("turn.delta")
@dataclass
class TurnDelta(Event):
    """A streamed chunk of text. It is what lets the TUI show several models writing side by
    side.
    """

    round: int
    participant: str
    text: str


@_event("turn.thinking")
@dataclass
class TurnThinking(Event):
    """A reasoning draft. It goes to disk and to people only; whether it enters anyone else's
    context is decided by share_thinking.
    """

    round: int
    participant: str
    text: str


@_event("turn.end")
@dataclass
class TurnEnd(Event):
    round: int
    participant: str
    chars: int
    duration_s: float
    usage: dict[str, Any]
    error: str | None = None
    #: The reply was cut off part-way by the output budget. The prose is valid and the code still
    #: lands, but it **does not count as a complete turn** — someone cut off has usually not reached
    #: their conclusion, and judging consensus on that is taking half a sentence for a position.
    truncated: bool = False
    #: Within one round both draft and revise are turns, and (round, participant) alone cannot tell
    #: which — on resume the truncation flag would attach to the wrong one. Older event streams lack
    #: these two fields, so reading them back falls back to matching on (round, participant).
    phase: int = 0
    kind: str = "draft"


# --------------------------------------------------------------------------- # Consensus
# --------------------------------------------------------------------------- #


@_event("stance.emit")
@dataclass
class StanceEmit(Event):
    round: int
    participant: str
    stance: dict[str, Any]
    #: obtained only after a retry, or finally recorded as unknown
    degraded: bool = False


@_event("consensus.update")
@dataclass
class ConsensusUpdate(Event):
    round: int
    unresolved: int
    min_confidence: float
    matrix: dict[str, dict[str, str]]
    state: str  # open | converged | stalled


@_event("false_consensus")
@dataclass
class FalseConsensus(Event):
    """Every stance card says agree, but on drafting the prose turns out to conflict
    substantively — back for another round.
    """

    round: int
    detected_by: str
    conflicts: list[str]


# --------------------------------------------------------------------------- # Evidence / budget /
# human in the loop --------------------------------------------------------------------------- #


@_event("writer.mismatch")
@dataclass
class WriterMismatch(Event):
    """The rapporteur's conclusion does not match the disagreement matrix.

    The rapporteur is one of the participants on rotation, so this is a **conflict of role**
    rather than a question of diligence; the mismatch is therefore a first-class fact that
    must land in the event stream for review, not an ordinary warning.
    """

    round: int
    writer: str
    kind: str  # omitted_disagreements | claimed_disagreements
    detail: str


@_event("briefing")
@dataclass
class Briefing(Event):
    """One participant was given private material the others cannot see.

    With asymmetric material, **a disagreement may be only an information gap and not a
    difference in judgement** — without this record the reader cannot tell. The content
    lives in the run directory; only a summary and a length are recorded here.
    """

    participant: str
    chars: int
    source: str = ""
    excerpt: str = ""


@_event("adoption")
@dataclass
class AdoptionEvent(Event):
    """Someone lifted a rival's previous round wholesale.

    It states a fact only. Good or bad is answered by ``evidence_before`` /
    ``evidence_after`` — self-tests going from passing to failing after the copy is what
    "converging on the wrong side" means. ``None`` for both means the run had no execution
    evidence to go on, not that the evidence never changed.
    """

    round: int
    participant: str
    adopted_from: str
    path: str
    similarity_to_peer: float
    similarity_to_own: float
    evidence_before: int | None = None
    evidence_after: int | None = None

    @property
    def evidence_regressed(self) -> bool:
        """After the copy, the self-tests went from passing to failing."""
        return (
            self.evidence_before == 0
            and self.evidence_after is not None
            and self.evidence_after != 0
        )


@_event("files.applied")
@dataclass
class FilesApplied(Event):
    """Code blocks from a participant's output were written into its working directory.

    An API model cannot write files and can only produce text; this event makes "who changed
    which files" checkable — without it, where the code came from is a black box.
    """

    round: int
    participant: str
    files: list[str]
    rejected: list[str]
    #: Total number of code fences in that round's output. ``files`` empty while this number is not
    #: small means the participant wrote code without marking the path in ``name=`` form — that is
    #: non-compliance with the format, not "nothing needed changing this round", and the two have to
    #: be distinguishable.
    fences_seen: int = 0

    @property
    def silently_dropped(self) -> bool:
        return not self.files and not self.rejected and self.fences_seen > 0


@_event("evidence")
@dataclass
class Evidence(Event):
    """In a code task, the real result of running the verify command.

    A position that contradicts an execution result does not count as a valid agree in the
    consensus assessment.
    """

    round: int
    participant: str
    cmd: str
    exit_code: int
    summary: str


@_event("budget.warn")
@dataclass
class BudgetWarn(Event):
    spent_usd: float | None
    limit_usd: float | None
    elapsed_s: float
    limit_s: float | None
    reason: str


@_event("human.inject")
@dataclass
class HumanInject(Event):
    """Human in the loop: interject / veto a premise / follow one side / wrap up early."""

    round: int
    kind: str  # inject | veto | follow | wrap_up
    text: str


# --------------------------------------------------------------------------- # The outcome
# --------------------------------------------------------------------------- #


@_event("verdict.final")
@dataclass
class VerdictFinal(Event):
    outcome: str
    run_id: str
    drafted_by: str | None
    rounds_used: int
    unresolved: int
    result_path: str | None = None


@_event("run.aborted")
@dataclass
class RunAborted(Event):
    """The run was aborted from outside (SIGTERM / SIGINT / reaped by a parent process).

    Without this record, an aborted run is merely "the event stream stopped" — you have to
    infer that it did not finish, and inference misses cases. Recording it explicitly is what
    gives review and statistics something to stand on.
    """

    reason: str
    round: int | None = None


@_event("error")
@dataclass
class ErrorEvent(Event):
    where: str
    message: str
