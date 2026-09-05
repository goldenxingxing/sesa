"""The core data types.

A design constraint: nothing here depends on IO, rendering or a third-party SDK, so that
the Engine, the TUI and SDK users share one set of contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .i18n import t

# --------------------------------------------------------------------------- # Participants
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParticipantSpec:
    """One participant = Adapter (how to call it) × Model (which brain) × Role (what stance).

    ``options`` holds adapter-specific configuration (command / base_url / api_key ...),
    interpreted by each Adapter itself, which is what makes "adding a new agent = writing a
    few lines of YAML" true.
    """

    id: str
    adapter: str
    model: str | None = None
    role: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        parts = [self.adapter]
        if self.model:
            parts.append(self.model)
        return f"{self.id} ({' / '.join(parts)})"


# --------------------------------------------------------------------------- # Usage and cost
# --------------------------------------------------------------------------- #


@dataclass
class Usage:
    """The usage of one call.

    CLI adapters usually cannot report token counts — then ``known=False`` and the budget
    falls back to the wall clock. Never pass an estimate off as real usage.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None
    known: bool = True

    @classmethod
    def unknown(cls) -> Usage:
        return cls(known=False)

    def merge(self, other: Usage) -> Usage:
        """Merge two usage records.

        **With one side unknown, the merged number is not a total.** An earlier implementation
        set ``known`` to False while keeping the partial sums, so the report accumulated "10
        tokens" while the real usage might be ten times that — an empty value masquerading as
        data. If it is not known, hand back ``unknown()`` and make the consumer face "this was
        not measured".
        """
        if not (self.known and other.known):
            return Usage.unknown()

        def add(a: int | float | None, b: int | float | None):
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        return Usage(
            input_tokens=add(self.input_tokens, other.input_tokens),
            output_tokens=add(self.output_tokens, other.output_tokens),
            usd=add(self.usd, other.usd),
            known=True,
        )


# --------------------------------------------------------------------------- # Stream chunks
# --------------------------------------------------------------------------- #


@dataclass
class TextDelta:
    """A chunk of streamed text."""

    text: str


@dataclass
class ThinkingDelta:
    """A chunk of reasoning draft.

    **By default it does not enter the other participants' context** (see DESIGN.md §4.6);
    it goes to disk for people to read.
    Keeping it as a separate kind of chunk from the prose is what makes "share it or not" a
    switch that can really be turned on and off, instead of a convention in the prompt.
    """

    text: str


@dataclass
class Done:
    """One call finished, carrying its usage."""

    usage: Usage = field(default_factory=Usage.unknown)
    #: The output budget ran out and the reply was cut off part-way. **The prose is still valid and
    #: must be kept** — truncation often happens after the code block is complete, and discarding
    #: the lot throws away usable work. But it must never pass for a complete turn: consensus may
    #: not be judged on it.
    truncated: bool = False


Chunk = TextDelta | ThinkingDelta | Done


@dataclass
class TurnResult:
    """One participant's complete turn in one round."""

    participant: str
    round: int
    text: str
    usage: Usage
    duration_s: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------- # The stance card
# --------------------------------------------------------------------------- #

StanceVerdict = Literal["agree", "partial", "disagree", "unknown"]

#: How a claim was verified.
#:
#: - ``executed``: really ran the other's test/command and got an exit code
#: - ``cited``: checked the source they cited (file line, original text) and confirmed it
#: exists and says that
#: - ``unable``: **could not check, with the reason given**. This is a respectable answer
#: and
#: costs nothing — forcing people to dress "did not check" up as "checked" is far worse than
#: admitting they could not.
VerifyMethod = Literal["executed", "cited", "unable"]

#: The conclusion of a verification. Always ``unable`` when how is ``unable``.
VerifyResult = Literal["reproduced", "refuted", "unable"]


@dataclass
class Verification:
    """One verification of someone else's claim.

    This is the line between "rating a conclusion" and "checking the evidence". The earlier
    stance card only let people rate conclusions, so **agreeing with a claim you never checked
    was entirely compliant** — measured in run 20260901-103359: one party wrote in premises
    "my execution happened before they wrote the test file, so I cannot verify it myself and
    am relying on their reported output" while giving a partial. The protocol had no slot for
    saying "I did not check", so it went into the premises, and the consensus assessment could
    not see it at all.
    """

    #: Which of the other's claims was verified. A verbatim excerpt or a number, so it can be
    #: matched.
    of: str
    how: VerifyMethod
    result: VerifyResult
    #: How it was checked and what was seen. For ``unable``, why it could not be checked.
    detail: str = ""

    @property
    def grounds_agreement(self) -> bool:
        """Whether this verification can be a foundation for an agreement.

        Only **reproducing it yourself** counts. ``refuted`` is counter evidence and ``unable`` is
        an absence of measurement — neither can support an agreement.
        """
        return self.result == "reproduced" and self.how in ("executed", "cited")


@dataclass
class StanceOn:
    """A position on another participant.

    ``partial`` **requires** a non-empty ``residuals`` (the specific points still unresolved).
    A "partial agreement" with an empty payload is an unverifiable position — it states
    neither what is agreed nor what is held back — and is therefore treated as unknown.

    ``verified`` records the process of checking the other's evidence. **When the other party
    produced something checkable and you agreed without checking any of it, that agreement
    does not count** — see ``Verification``.
    """

    verdict: StanceVerdict
    reason: str = ""
    residuals: list[str] = field(default_factory=list)
    verified: list[Verification] = field(default_factory=list)

    @property
    def has_grounds(self) -> bool:
        return any(v.grounds_agreement for v in self.verified)

    def __post_init__(self) -> None:
        # An invariant written in a docstring and enforced by nobody is not written at all. The
        # extraction layer downgrades an empty-payload partial to unknown, but **any code that
        # constructs one directly bypasses that path** — including resume recovery, the judge, and
        # the tests. A non-empty list is not enough: `[""]` and `["   "]` both pass, while what the
        # contract asks for is a **specific, stateable** reservation. A blank residual is synonymous
        # with no residual.
        self.residuals = [r for r in self.residuals if str(r).strip()]
        if self.verdict == "partial" and not self.residuals:
            raise ValueError(
                t(
                    "partial requires a non-empty residuals: a \u300cpartial agreement\u300d "
                    "that says neither what is agreed nor what is held back cannot be "
                    "checked. Either write the reservation down, or use "
                    "agree/disagree/unknown."
                )
            )


@dataclass
class Stance:
    """The structured stance card — the only input to the consensus assessment.

    ``unknown=True`` means extraction failed and failed again on retry: that participant's
    position for that round is recorded as unknown and listed explicitly in the report. No
    guessing, no writing on their behalf.
    """

    participant: str
    round: int
    position: str = ""
    #: How sure they are of their own position, 0–1. ``None`` means **not reported**.
    #: 0.0 cannot double as "not reported": that would make "I am very unsure" and "I did not say"
    #: indistinguishable, while the two mean opposite things for the consensus assessment — the
    #: first should block consensus, the second is merely missing data. Measured consequence: a run
    #: reporting 0.00 explicitly was judged "consensus with reservations" while one reporting 0.01
    #: was judged "unfinished".
    confidence: float | None = None

    def __post_init__(self) -> None:
        # The documentation says 0–1 and nobody enforced it. Models filling in 0–100 is common and
        # the extraction layer divides by 100, but **paths that construct a Stance directly bypass
        # that step** — resume recovery, the judge, the tests. A confidence of 7.5 makes every bar
        # meaningless.
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(t("confidence must be between 0 and 1; got {v}", v=self.confidence))

    #: The premises this position depends on, one per item.
    #: Listing them separately rather than burying them in the prose is what makes them **attackable
    #: one by one** — most disagreements come from differing premises rather than a wrong
    #: conclusion, and "I think you are wrong" is an attack nobody can check. They are also where
    #: `resume --inject`'s "veto a premise" intervention gets its purchase.
    premises: list[str] = field(default_factory=list)
    key_claims: list[str] = field(default_factory=list)
    stance_on: dict[str, StanceOn] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    changed_from_last_round: bool = False
    unknown: bool = False
    raw: str | None = None

    @classmethod
    def as_unknown(cls, participant: str, round: int, raw: str | None = None) -> Stance:
        return cls(participant=participant, round=round, unknown=True, raw=raw)


# --------------------------------------------------------------------------- # Consensus
# --------------------------------------------------------------------------- #


class Outcome(StrEnum):
    """The three outcomes, labelled honestly — stuck is not the same as united."""

    CONSENSUS = "consensus"

    #: Nobody explicitly opposed and no stance is unknown, but someone holds specific unresolved
    #: points. This is not a failure — it is "broadly agreed, with reservations explicitly on
    #: record".
    CONSENSUS_WITH_RESERVATIONS = "consensus_with_reservations"

    #: Nobody explicitly opposed, but some cells **could not be measured** (stance-card extraction
    #: failed, and so on). It must carry coverage — "not measured" is never "agreed".
    PARTIAL_COVERAGE_CONSENSUS = "partial_coverage_consensus"

    DEADLOCK = "deadlock"
    EXHAUSTED = "exhausted"

    #: Every stance card says agree, but the prose holds a substantive conflict; back for another
    #: round.
    FALSE_CONSENSUS = "false_consensus"
    #: This protocol **does not measure consensus** (reflect, say: nobody sees anybody throughout,
    #: so peer assessment is structurally impossible). Strictly distinct from ``exhausted`` — that
    #: is "measured, did not settle it", this is "never measured". Treating the unmeasured as
    #: disagreement is labelling missing data as disagreement.
    NOT_MEASURED = "not_measured"


@dataclass
class Disagreement:
    """One open disagreement.

    Merely listing each side's position is not enough — that hands the work of making sense of
    it back to the reader.
    ``root_cause`` / ``decisive_question`` are filled in by the rapporteur (see DESIGN.md §7.2).
    """

    topic: str
    positions: dict[str, str]  # participant -> a summary of their position
    reasons: dict[str, str] = field(default_factory=dict)
    root_cause: str = ""
    decisive_question: str = ""


@dataclass
class ConsensusReport:
    """A snapshot of the consensus at the end of a round.

    The assessment is **default-deny**: a cell counts as "resolved" if and only if there is a
    **parseable, explicit ``agree``**.

    But "no agree confirmed" has two entirely different causes, and they must be accounted
    separately — compressing them into one scalar is labelling missing data as disagreement:

    * ``opposed``    — someone really did object (``disagree``)
    * ``unmeasured`` — they were asked, but nothing was measured (stance-card extraction
      failed, the other's turn not yet read)

    A ``partial`` with residuals counts separately under ``reservations``: neither opposition
    nor missing measurement, it only takes the outcome one step down from ``consensus``.
    """

    round: int
    matrix: dict[str, dict[str, StanceVerdict]]
    min_confidence: float
    converged: bool
    stalled_rounds: int
    #: How many participants **reported** a confidence.
    #: ``min_confidence > 0`` cannot stand in for it: 0.0 is both the sentinel for "nobody reported"
    #: and the legitimate value "reported 0". The consequence of conflating them was measured — a
    #: confidence of 0.00 judged "consensus with reservations" while 0.01 was judged "unfinished",
    #: **less certainty buying a better outcome**.
    confidences_known: int = 0
    #: How many were supposed to report a confidence (the participants who handed in a usable stance
    #: card). Only together with ``confidences_known`` can "nobody reported" be told from "only some
    #: reported".
    expected_confidences: int = 0
    #: how many explicit agree cells were measured. With none at all, nothing can be called
    #: consensus
    agreed: int = 0
    #: how many cells hold real opposition
    opposed: int = 0
    #: how many cells were asked about but not measured — **never to be taken for agreement**
    unmeasured: int = 0
    #: the share of cells measured, delivered with the outcome
    coverage: float = 0.0
    #: the unmeasured cells, as a structured field rather than report prose
    unmeasured_cells: list[str] = field(default_factory=list)
    #: Cells where "they said agree and submitted not one verification record", of the form ``"a →
    #: b"``.
    #: These cells already count as unmeasured in the matrix under ``unknown`` — **but leaving it at
    #: an unknown is not enough**: the reader would think the other party took no position, when in
    #: fact they did and it merely has no foundation. The two mean entirely different things for
    #: what to do next: an ordinary unmeasured cell calls for asking them, this one calls for making
    #: them verify.
    unverified_agreements: list[str] = field(default_factory=list)
    #: Cells where "they said agree and submitted a verification record, and the record says they
    #: **could not check**".
    #: The cell is downgraded to unmeasured all the same — a failed verification cannot support an
    #: agreement, and that does not bend. But **the report must separate these two kinds of
    #: people**: this project promised the participants that "``how: unable`` is a respectable
    #: answer and costs nothing", and writing someone who honestly gave their reason and someone who
    #: said nothing into the same sentence "submitted no verification record" makes that promise
    #: empty — they did submit one.
    #: (Round 13 self-review: deepseek pointed out that boundary 4 was not honoured in the code.
    #: They were right; the place to honour it is not the downgrade logic but the wording of the
    #: deliverable.)
    unverifiable_agreements: list[str] = field(default_factory=list)
    #: how many partials with residuals. Not hard disagreement, but it takes the outcome down to
    #: "consensus with reservations"
    reservations: int = 0
    #: every residual on record, entering the deliverable verbatim
    residuals: dict[str, list[str]] = field(default_factory=dict)
    partials: int = 0
    unknown_participants: list[str] = field(default_factory=list)
    #: what prevents a verdict of converged, used to explain honestly to the user "why it is not
    #: settled yet"
    blockers: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> int:
        """Total number of unresolved cells. Kept as a derived quantity — the accounting still goes by
        the two separate figures.

        .. warning::
           **Do not call this number "open disagreements" directly.** It equals
           ``opposed + unmeasured``, and "someone objected" and "the engine did not measure it" are
           two different things — compressing them into one number and labelling it "disagreement"
           is exactly what this project's second bottom line exists to prevent.
           All user-facing text goes through :meth:`describe_unresolved`.
        """
        return self.opposed + self.unmeasured

    def describe_unresolved(self) -> str:
        """Spell out the unresolved cells, for every piece of user-facing text.

        A lesson from practice: the same error of "calling the unmeasured a disagreement" was
        committed once at each of **four outlets** — the RESULT.md prose, the terminal progress
        output, the consensus blockers, and the REPORT.md minutes — and fixing one had it emerge
        from the next. Fixing them one by one does not work; the correct wording has to have a
        single source.
        """
        if not self.unresolved:
            return t("no unresolved cells")
        parts = []
        if self.opposed:
            parts.append(t("{n} cells hold explicit opposition", n=self.opposed))
        if self.unmeasured:
            hints = []
            if self.unverified_agreements:
                # "Not measured" has "said agree but did not verify" mixed into it, and that has to
                # be said out loud — otherwise the reader sees only an unknown and assumes the other
                # party took no position.
                hints.append(
                    t(
                        "{n} are agreements with no verification submitted",
                        n=len(self.unverified_agreements),
                    )
                )
            if self.unverifiable_agreements:
                hints.append(
                    t(
                        "{n} said outright they could not check",
                        n=len(self.unverifiable_agreements),
                    )
                )
            hint = t(", of which ") + t("\u3001").join(hints) if hints else ""
            parts.append(
                t("{n} cells not measured (which is not objection)", n=self.unmeasured) + hint
            )
        return " · ".join(parts)

    def matrix_rows(self) -> list[tuple[str, dict[str, StanceVerdict]]]:
        return sorted(self.matrix.items())

    def disagreeing_pairs(self) -> list[tuple[str, str]]:
        return [
            (source, target)
            for source, row in sorted(self.matrix.items())
            for target, verdict in sorted(row.items())
            if verdict == "disagree"
        ]


@dataclass
class Result:
    """The structured form of the final deliverable; ``RESULT.md`` is its rendering.

    The skeleton is constant: agreement and disagreement change the proportions of the sections,
    not the shape of the document.
    """

    run_id: str
    task: str
    outcome: Outcome
    conclusion: str = ""
    drafted_by: str | None = None
    grounds: list[str] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    minority: dict[str, str] = field(default_factory=dict)
    #: reservations on record, entering the deliverable verbatim
    residuals: dict[str, list[str]] = field(default_factory=dict)
    #: participant -> how many of their turns were cut off by the output budget.
    #: A participant cut off **in every round** has not really taken part — and that fact left no
    #: trace in the outcome. Measured: one participant wrote 36,271 characters over two rounds,
    #: landed zero files, had its stance card adopted not once, and the word "truncated" appeared 0
    #: times in `RESULT.md`; it came out only because another participant happened to mention it.
    #: **The engine knew, and did not say.**
    truncated_turns: dict[str, int] = field(default_factory=dict)
    #: participant -> the length of that party's private material. Non-empty means **the parties'
    #: material is asymmetric**, in which case a disagreement may be only an information gap rather
    #: than a difference in judgement, and the reader has to know.
    briefings: dict[str, int] = field(default_factory=dict)
    #: participant -> the premises that party finally declared. Always in the deliverable: **the
    #: conclusion holds only under these premises**, and a reader who changes a premise has to judge
    #: it again.
    premises: dict[str, list[str]] = field(default_factory=dict)
    #: the rendered cross-test matrix (code tasks)
    cross_test: str = ""
    #: participant -> branch name. **Always kept**; an implementation that was not adopted carries
    #: the minority opinion
    branches: dict[str, str] = field(default_factory=dict)
    #: participants whose implementation passes **everyone's** tests — the hardest form of evidence
    universally_passing: list[str] = field(default_factory=list)
    #: participants whose tests pass only for themselves — the signal that **the tests encode
    #: private assumptions**. A pair with universally_passing: one says "this implementation is the
    #: most solid", the other says "these tests may hold only under their own assumptions".
    suspicious_testers: list[str] = field(default_factory=list)
    #: the share of cells measured. Must be delivered whenever the outcome is
    #: partial_coverage_consensus
    #: **The default is 0.0, not 1.0.** Nobody filling it in = nothing measured, not everything
    #: measured; the default stands on the "unproven" side, the same principle this project applies
    #: to consensus (only an explicit agree counts).
    coverage: float = 0.0
    unmeasured_cells: list[str] = field(default_factory=list)
    #: Cells where "they said agree and submitted not one verification record". A subset of
    #: ``unmeasured_cells``, but listed separately — **the cause differs and so does what to do
    #: next**: an ordinary unmeasured cell calls for asking them, this one calls for making them
    #: verify.
    unverified_agreements: list[str] = field(default_factory=list)
    #: Cells where "they said agree, submitted a verification, and the record says they could not
    #: check". Kept apart from the previous one: the first calls for chasing them to verify, the
    #: second for solving the obstacle they named.
    unverifiable_agreements: list[str] = field(default_factory=list)
    #: The verification records supporting each "agree", of the form ``"a → b"`` -> the
    #: verifications that party claims to have done.
    #: **They must reach the deliverable.** The engine judges "this agreement has a foundation" on
    #: these records, and every one of them is **self-reported** — a participant says they ran it,
    #: and nobody can prove it for them. Speaking up only when verification is missing and saying
    #: nothing when it is present builds the consensus on something the reader cannot see. (Round 13
    #: self-review: kimi pointed out that ``Verification.of`` is unchecked and any irrelevant
    #: sentence can serve as a foundation. Validating `of` cannot stop someone determined to lie —
    #: the whole block is self-reported — **the only thing that can is letting the reader see it**.)
    verification_grounds: dict[str, list[str]] = field(default_factory=dict)
    rounds_used: int = 0
    usage: Usage = field(default_factory=Usage.unknown)
    #: "Someone lifted a rival's work wholesale". Each item is ``(round, copier, copied-from, file,
    #: similarity to the rival, similarity to self, did self-tests go from passing to failing)``.
    #: Always in the deliverable — converging on the other party and converging on the right answer
    #: are two different things.
    adoptions: list[tuple[int, str, str, str, float, float, bool]] = field(default_factory=list)

    @property
    def regressive_adoptions(self) -> list[tuple[int, str, str, str, float, float, bool]]:
        """Those where the self-tests went from passing to failing after the copy. Always empty for a
        run with no execution evidence.
        """
        return [a for a in self.adoptions if a[6]]
