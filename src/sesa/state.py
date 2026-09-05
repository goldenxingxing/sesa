"""The state of a deliberation in progress.

The Engine and the Protocol share this one object; the Protocol only reads it to decide
"who speaks in which phase, and what they can see", and never advances it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adoption import Adoption
from .i18n import t
from .types import ConsensusReport, ParticipantSpec, Stance, Usage


@dataclass
class Turn:
    """One participant's turn, in one phase of one round."""

    participant: str
    round: int
    phase: int
    kind: str  # draft | revise | attack | rebut | crosscheck
    #: the prose with the stance card stripped out — this is the version fed to the others
    text: str
    #: The model's **raw output**, stance card included. For the archive; it enters nobody's
    #: context. Without it there is no way to check afterwards whether the parser dropped or altered
    #: a residual — "the event stream is the only source of truth" cannot hold if the source of that
    #: truth is thrown away.
    raw: str = ""
    #: The reasoning draft. By default it **does not enter the other participants' context**; it
    #: goes to disk for people to read (see DESIGN.md §4.6)
    thinking: str = ""
    usage: Usage = field(default_factory=Usage.unknown)
    duration_s: float = 0.0
    error: str | None = None
    #: The reply was cut off part-way by the output budget. The prose is kept and the code still
    #: lands, but it **does not count as a complete turn**: someone cut off has usually not reached
    #: their conclusion, and judging consensus on that is taking half a sentence for a position.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())

    @property
    def complete(self) -> bool:
        """Successful and not truncated. Only such a turn counts for the consensus assessment."""
        return self.ok and not self.truncated


@dataclass
class EvidenceRecord:
    """One piece of evidence.

    **Evidence can be wrong too**, so its source must be graded (see DESIGN.md §6.2):

    * ``engine``  — obtained by the engine itself executing in a controlled workspace; only
      this counts as evidence
    * ``claimed`` — a participant's "I ran it, the result was …", which is only **a claim
      awaiting verification**, on a par with any other assertion, and must never be taken as
      fact

    ``against`` records whose artefacts this evidence tests — cross-testing (A's tests
    against B's implementation) is what it distinguishes from "testing only yourself".
    """

    participant: str  # who produced the evidence / who wrote the test
    cmd: str
    exit_code: int
    summary: str
    source: str = "engine"  # engine | claimed
    #: the party being tested; equal to participant means "testing yourself", the weakest form
    against: str | None = None
    #: the workspace revision fingerprint; one code change invalidates old evidence
    revision: str | None = None
    #: Invalidated by a later change. Marked in one place by the engine before each round's
    #: assessment — **the consensus assessment and the prompts must see the same mark**, or you get
    #: "the prompt does not ask for verification while the assessment downgrades for not verifying",
    #: a rule the participant cannot comply with.
    stale: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    @property
    def is_fact(self) -> bool:
        """Only a result the engine executed itself is a fact."""
        return self.source == "engine"

    def is_stale(self, current: str | None) -> bool:
        """Whether this evidence has been invalidated by a later change.

        ``None`` on either side returns ``False`` — **without a measurable revision you cannot
        claim it is stale**, which would be taking "I do not know" for "I know". The cost is
        under-reporting, and under-reporting is the right direction here: over-reporting throws
        valid evidence away, and evidence is scarce as it is.

        ``revision`` used to be written and never read by any code, which made "one code change
        invalidates old evidence" an empty promise in the implementation.
        """
        if self.revision is None or current is None:
            return False
        return self.revision != current

    @property
    def is_self_test(self) -> bool:
        """Whoever writes the implementation also writes the tests — a green light says next to
        nothing.
        """
        return self.against in (None, self.participant) or self.against == self.participant


@dataclass
class RoundRecord:
    index: int
    turns: list[Turn] = field(default_factory=list)
    stances: dict[str, Stance] = field(default_factory=dict)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    consensus: ConsensusReport | None = None
    #: human interventions effective this round (interject / veto a premise / follow one side)
    injections: list[str] = field(default_factory=list)
    #: "lifted a rival's work wholesale" detections for this round
    adoptions: list[Adoption] = field(default_factory=list)

    def latest_by(self, participant: str, *, only_ok: bool = True) -> Turn | None:
        """Someone's last turn this round. By default **only a successful one counts**.

        The engine records failed turns in ``turns`` too (timeouts, crashes, empty replies), and
        ``reflect`` uses this result to fill in "your answer from the last round" — picking up
        the failed one tells the participant "you said nothing last round", erasing its own
        proposal.
        """
        # **A complete turn outranks a truncated one.** Several turns from one participant in one
        # round can only be retries; if the earlier one is complete and the later one truncated,
        # taking the later means taking half a sentence as "their answer". Look for a complete one
        # first and fall back to a truncated one — rather than always taking the last.
        candidates = [t for t in reversed(self.turns) if t.participant == participant]
        for turn in candidates:
            if (turn.ok or not only_ok) and not turn.truncated:
                return turn
        for turn in candidates:
            if turn.ok or not only_ok:
                return turn
        return None

    def statements(self) -> dict[str, str]:
        """participant -> their last valid turn this round.

        A truncated turn **is included** (the part written before the cut has value), but carries
        a note. The engine does not adopt its stance card (``turn.complete``) — and if this
        quietly passed it through on ``turn.ok``, what the others read would be an
        apparently-complete claim that may stop mid-sentence. **One turn, two inconsistent
        standards.**
        """
        out: dict[str, str] = {}
        for turn in self.turns:
            if not turn.ok:
                continue
            out[turn.participant] = (
                turn.text
                + "\n\n> ⚠️ "
                + t(
                    "**The turn above was cut off here by the output budget**; there was "
                    "more to come, and its stance card was not adopted. Take that into "
                    "account when you judge it."
                )
                if turn.truncated
                else turn.text
            )
        return out


@dataclass
class DeliberationState:
    task: str
    participants: list[ParticipantSpec]
    max_rounds: int
    rounds: list[RoundRecord] = field(default_factory=list)
    #: never | on_deadlock | always
    share_thinking: str = "never"
    #: Whether this protocol asks the participants for a stance card. ``reflect`` does not — listing
    #: its participants as "stance could not be parsed" accuses them of not answering a question
    #: nobody put to them.
    stances_requested: bool = True
    #: Whether each party's registered residuals are fed back to the other. Off, and the "the
    #: opposing side is the natural auditor" defence does not exist
    share_residuals: bool = True
    #: participant -> the branch their artefacts are on. Verifying someone's evidence needs it to
    #: locate them — a bare "exit code 0" gives the others nothing to re-run.
    branches: dict[str, str] = field(default_factory=dict)
    #: human interventions to inject into the next round
    pending_injections: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    @property
    def ids(self) -> list[str]:
        return [p.id for p in self.participants]

    @property
    def round_index(self) -> int:
        """The index of the current (about to start) round. Round 0 is the independent drafts."""
        return len(self.rounds)

    @property
    def current(self) -> RoundRecord | None:
        return self.rounds[-1] if self.rounds else None

    def previous(self) -> RoundRecord | None:
        """The last completed round's record."""
        return self.rounds[-2] if len(self.rounds) >= 2 else None

    def spec(self, participant_id: str) -> ParticipantSpec:
        for p in self.participants:
            if p.id == participant_id:
                return p
        raise KeyError(participant_id)

    def others(self, participant_id: str) -> list[str]:
        return [pid for pid in self.ids if pid != participant_id]

    def rotate(self, offset: int = 0) -> str:
        """Rotation of a solo role: it moves among the participants by round (see DESIGN.md §4.3)."""
        return self.ids[(self.round_index + offset) % len(self.ids)]

    def thinking_is_shared(self, deadlocked: bool) -> bool:
        if self.share_thinking == "always":
            return True
        if self.share_thinking == "on_deadlock":
            return deadlocked
        return False

    def total_usage(self) -> Usage:
        total = Usage(input_tokens=0, output_tokens=0, usd=0.0, known=True)
        for rnd in self.rounds:
            for turn in rnd.turns:
                total = total.merge(turn.usage)
        return total


def visible_evidence(state: DeliberationState) -> list[EvidenceRecord]:
    """All the evidence a participant **can see and that still holds** while writing this
    round's stance card.

    This is the single source of truth for the verification duty. **The consensus assessment
    and the prompts must read the same thing** — the entire reason this function exists is to
    make it impossible for "imposing the rule" and "announcing the rule" to drift apart
    (DESIGN 14.25.1). They used to be two separately maintained pieces of logic; I changed
    the assessment to accumulate and forgot the prompt side, so participants were told they
    need only verify the previous round while being judged over every round.

    Three tests, each answering a hole already fallen into:

    * **Before this round** — this round's evidence is executed by the engine only after
      everyone has spoken, while the stance card is written before that. Demanding
      verification of something that did not exist when the card was written punishes the
      impossible.
    * **Cumulative, not just the immediately preceding round** — otherwise "I produced no new
      evidence last round" becomes a ready-made way around the verification duty, while the
      earlier evidence has held all along, in plain sight of everyone.
    * **Not stale** — evidence is invalidated for exactly one reason: the code changed.
      Demanding a reproduction of a result that no longer holds leaves the other party either
      unable to reproduce it (judged "did not verify") or lying that they did.
    """
    current = state.current
    return [
        item
        for record in state.rounds
        if current is None or record.index < current.index
        for item in record.evidence
        if item.participant and item.is_fact and not item.stale
    ]
