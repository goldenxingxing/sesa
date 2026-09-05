"""The disagreement matrix and the three-state assessment — the heart of the consensus
layer.

The test is **computable, printable and reviewable**, rather than some model saying "I
think they have settled it":

```
unresolved = #{ ordered pairs (i, j) : i's verdict on j == "disagree" }
```

- **consensus**: ``unresolved == 0``, no ``unknown`` stance, and
  ``min(confidence) >= threshold``
- **deadlock**: K consecutive rounds with nobody changing position and ``unresolved``
  not falling — **stuck is not the same as united**
- **exhausted**: the rounds ran out or the budget tripped

All three outcomes are labelled honestly in the report; a deadlock is never dressed up
as consensus.
"""

from __future__ import annotations

import unicodedata

from ..i18n import scoped, t
from ..prompts import pick_language
from ..state import DeliberationState, RoundRecord
from ..types import ConsensusReport, Outcome, StanceVerdict

DEFAULT_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_STABILITY_WINDOW = 2


class StanceMatrix:
    """Compute the disagreement matrix and the convergence state from the structured stance
    cards.
    """

    def __init__(
        self,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        stability_window: int = DEFAULT_STABILITY_WINDOW,
        min_coverage: float = 0.0,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.stability_window = stability_window
        #: The quorum is not hard-coded in the engine — coverage is handed to the caller and the bar
        #: is a setting. The default of 0.0 applies only the structural floor (at least one agree
        #: measured).
        self.min_coverage = min_coverage

    # ------------------------------------------------------------------ #

    def build_matrix(
        self, record: RoundRecord, ids: list[str], *, verifiable: set[str] | None = None
    ) -> dict[str, dict[str, StanceVerdict]]:
        """Build the disagreement matrix.

        ``verifiable`` is "whoever produced checkable evidence". Expressing **agreement without
        reservation** towards one of them requires at least one verification you reproduced
        yourself, or it degrades to ``unknown`` — this is default-deny extended from "stance
        parsing" to "evidence": **saying you agree without having checked their evidence is not
        agreement, it is not measured.**

        Why it has to be conditional on ``verifiable``: a pure design debate contains nothing
        executable and nobody can verify anybody. Demanding verification unconditionally would
        make such deliberations **permanently unable to reach consensus** — and those are
        exactly the occasions that most need several parties. When the other side produced
        nothing checkable, agreement stands as before — but then the whole conclusion rests on
        claims that could not be checked, which the report notes separately.
        """
        verifiable = verifiable or set()
        matrix: dict[str, dict[str, StanceVerdict]] = {}
        for source in ids:
            stance = record.stances.get(source)
            row: dict[str, StanceVerdict] = {}
            for target in ids:
                if target == source:
                    continue
                if stance is None or stance.unknown:
                    row[target] = "unknown"
                    continue
                on = stance.stance_on.get(target)
                if on is None:
                    row[target] = "unknown"
                    continue
                if on.verdict == "agree" and target in verifiable and not on.has_grounds:
                    row[target] = "unknown"
                    continue
                row[target] = on.verdict
            matrix[source] = row
        return matrix

    @staticmethod
    def _verifiable(state: DeliberationState) -> set[str]:
        """Who produced something checkable this round.

        The test is evidence the engine **executed itself** (``is_fact``). A participant's
        self-report of "I ran it, it passed" (``source="claimed"``) is only a claim awaiting
        verification — treating it as "checkable evidence" lets someone impose a verification
        duty on everyone else by mere assertion, over an "evidence" that was never executed.

        The test comes from :func:`sesa.state.visible_evidence` and nowhere else — **the
        consensus assessment and the prompts must read the same thing**, or a participant is
        degraded by a rule they were never told (DESIGN 14.25.1). They did drift apart once: I
        changed this to accumulate and forgot the prompt side.

        The one extra thing done here: a self-report (``source="claimed"``) does not count.
        Treating it as checkable evidence lets someone impose a verification duty on everyone
        else by mere assertion, over an "evidence" that was never executed.
        """
        from ..state import visible_evidence

        return {e.participant for e in visible_evidence(state)}

    # ------------------------------------------------------------------ #

    def assess(self, state: DeliberationState) -> ConsensusReport:
        # blockers are **the words that go into REPORT.md**, so they follow the deliberation
        # language, not the interface language: reading a Chinese deliberation's report under an
        # English interface, "why it did not converge" should not suddenly be in English.
        with scoped(pick_language(state.task)):
            return self._assess(state)

    def _assess(self, state: DeliberationState) -> ConsensusReport:
        record = state.current
        if record is None:
            raise ValueError(t("assess() needs at least one completed round on record"))

        ids = state.ids
        verifiable = self._verifiable(state)
        matrix = self.build_matrix(record, ids, verifiable=verifiable)

        # default-deny, but keeping "opposition" and "not measured" in separate accounts:
        # compressing them into one scalar is labelling missing data as disagreement.
        cells = sorted((src, tgt, v) for src, row in matrix.items() for tgt, v in row.items())
        agreed = sum(1 for _, _, v in cells if v == "agree")
        opposed = sum(1 for _, _, v in cells if v == "disagree")
        unmeasured = sum(1 for _, _, v in cells if v == "unknown")
        reservations = sum(1 for _, _, v in cells if v == "partial")
        # No cells ⇒ not one peer assessment happened ⇒ coverage is 0, not 1. It used to say ``else
        # 1.0``, so a single-participant deliberation, or one where no stance card parsed, reported
        # "100% coverage" — **nothing measured at all, reported as fully measured**.
        coverage = (len(cells) - unmeasured) / len(cells) if cells else 0.0
        unmeasured_cells = [f"{src} → {tgt}" for src, tgt, v in cells if v == "unknown"]
        # Which "not measured" cells are really "said agree but did not verify" — the downgrade
        # happens inside build_matrix and is invisible by this point, so it has to be recovered from
        # the original stance cards.
        unverified_agreements: list[str] = []
        unverifiable_agreements: list[str] = []
        for source in ids:
            stance = record.stances.get(source)
            if stance is None or stance.unknown:
                continue
            for target, on in stance.stance_on.items():
                if on.verdict != "agree" or target not in verifiable or on.has_grounds:
                    continue
                # Saying nothing and honestly saying "I could not check" have to be accounted
                # separately. Both cells degrade, but the first calls for chasing them to verify,
                # the second for solving the obstacle they named.
                if on.verified:
                    unverifiable_agreements.append(f"{source} → {target}")
                else:
                    unverified_agreements.append(f"{source} → {target}")
        unverified_agreements.sort()
        unverifiable_agreements.sort()
        residuals: dict[str, list[str]] = {}
        for source in ids:
            stance = record.stances.get(source)
            if stance is None or stance.unknown:
                continue
            for target, on in stance.stance_on.items():
                if on.verdict == "partial" and on.residuals:
                    residuals.setdefault(f"{source} → {target}", []).extend(on.residuals)

        unknown = sorted(pid for pid in ids if (s := record.stances.get(pid)) is None or s.unknown)
        # Someone with an unknown stance counts as 0.0 rather than being dropped from the aggregate
        # — dropping the unknown makes "lowest confidence" look higher than it is.
        confidences = [
            None if (st := record.stances.get(pid)) is None or st.unknown else st.confidence
            for pid in ids
        ]
        # **It must use the same set as `confidences`.** That one is taken over `ids`; taking this
        # one over `record.stances.values()` would include extras (an old card left by a participant
        # who has since dropped out), making expected larger than known and triggering a "not
        # everyone reported a confidence" downgrade out of nowhere.
        usable_cards = sum(
            1 for pid in ids if (st := record.stances.get(pid)) is not None and not st.unknown
        )
        # Only what was **reported** counts. 0.0 is a legitimate value ("I am very unsure"), not a
        # missing-value sentinel.
        known_conf = [c for c in confidences if c is not None]
        min_confidence = min(known_conf) if known_conf else 0.0

        blockers: list[str] = []
        if opposed:
            pairs = [
                t("{src} opposes {tgt}", src=src, tgt=tgt)
                for src, tgt, v in sorted(cells)
                if v == "disagree"
            ]
            blockers.append(
                t(
                    "{n} explicit oppositions ({pairs})",
                    n=opposed,
                    pairs=t("\uff1b").join(pairs[:3]),
                )
            )
        if unmeasured:
            # Not measured blocks full consensus too — not knowing what A thinks of B means you
            # cannot claim they agree. It also structurally rules out "round 0, nobody has read
            # anybody, call it consensus".
            blockers.append(
                t(
                    "{n} cells not measured ({cells}{more}) — the other's turn was not "
                    "read, or the stance card could not be parsed",
                    n=unmeasured,
                    cells=t("\uff1b").join(unmeasured_cells[:3]),
                    more=t(" and others") if unmeasured > 3 else "",
                )
            )
        # Declaring consensus with a stance unknown is deciding on behalf of someone who did not
        # speak. But **someone who was never asked for a stance card has not failed to parse** —
        # that accuses them of not answering a question nobody put to them. This accusation used to
        # have two exits (here and in the rapporteur), and plugging one was useless.
        if unknown and getattr(state, "stances_requested", True):
            blockers.append(
                t(
                    "the stance of {ids} could not be parsed; nobody may take a position "
                    "on their behalf",
                    ids=", ".join(unknown),
                )
            )
        if known_conf and min_confidence < self.confidence_threshold:
            blockers.append(
                t(
                    "lowest confidence {low:.2f} is below the threshold {threshold:.2f}",
                    low=min_confidence,
                    threshold=self.confidence_threshold,
                )
            )
        # default-deny applies to confidence too: **nobody reported = the bar cannot be
        # met**, and
        # "cannot be measured" must not be taken for "passed".
        #
        # But the wording has to be precise. These three cases are three different things,
        # and
        # they were once compressed into one sentence:
        # · not a single usable card       — calling that "no confidence reported" blames
        # the
        #   wrong thing
        # · cards, but nobody filled it in — the bar cannot be met
        # · some filled it in, some did not — count only those who **handed in a usable card
        # and
        #   left it blank**; those who handed in nothing were already reported under "stance
        # could
        #   not be parsed", and counting them again is a duplicate accusation — and when
        # everyone
        #   who left it blank is someone who handed in nothing, it produces the
        # self-contradictory
        #   "0 participants did not report".
        if len(known_conf) < len(confidences):
            missing = usable_cards - len(known_conf)
            if not usable_cards:
                blockers.append(t("there is not a single usable stance card"))
            elif not known_conf:
                blockers.append(
                    t(
                        "no participant reported a confidence, so there is no reading of "
                        "how sure anyone is"
                    )
                )
            elif missing > 0:
                blockers.append(
                    t(
                        "{n} participants reported no confidence, so how sure they are "
                        "cannot be judged",
                        n=missing,
                    )
                )

        if reservations:
            blockers.append(
                t(
                    "{n} reservations are unresolved ({total} residual items in all)",
                    n=reservations,
                    total=sum(len(v) for v in residuals.values()),
                )
            )

        # No blockers **is not the same as** consensus reached: an empty matrix (one participant, or
        # nobody taking a position on anybody) also has no blockers. The floor for convergence is
        # "at least one explicit agree cell" — default-deny made structural.
        converged = not blockers and agreed > 0
        stalled = 0 if converged else self._stalled_rounds(state, record, opposed + unmeasured)

        return ConsensusReport(
            round=record.index,
            matrix=matrix,
            min_confidence=min_confidence,
            confidences_known=len(known_conf),
            expected_confidences=usable_cards,
            converged=converged,
            stalled_rounds=stalled,
            agreed=agreed,
            opposed=opposed,
            unmeasured=unmeasured,
            coverage=coverage,
            unverified_agreements=unverified_agreements,
            unverifiable_agreements=unverifiable_agreements,
            unmeasured_cells=unmeasured_cells,
            reservations=reservations,
            residuals=residuals,
            partials=reservations,
            unknown_participants=unknown,
            blockers=blockers,
        )

    # ------------------------------------------------------------------ #

    def _stalled_rounds(
        self, state: DeliberationState, record: RoundRecord, unresolved: int
    ) -> int:
        """How many consecutive rounds have had "nobody changing position and disagreements not
        falling".
        """
        previous = state.previous()
        if previous is None or previous.consensus is None:
            return 0
        # A self-reported "I changed my position" is **not enough** to reset the stall counter.
        # Measured: across 33 deliberations there were 33 self-reported changes while the category
        # judgement actually moved 3 times — roughly 11× over-reporting. Allowing a self-report to
        # reset it on its own lets one participant fond of saying they changed defer deadlock
        # detection indefinitely.
        objectively_moved = unresolved < previous.consensus.unresolved or self._residuals_changed(
            previous, record
        )
        if objectively_moved:
            return 0
        return previous.consensus.stalled_rounds + 1

    @staticmethod
    def _residuals_changed(previous, record) -> bool:
        """Whether the **number** of residuals changed.

        .. warning::
           This used to compare the **set** of residuals, and the documentation called it "an
           objective signal that does not depend on self-report". That was wrong: **the
           residuals are themselves self-reported text**. So relisting the same reservation in
           fresh wording each round reset the stall counter forever — deadlock detection never
           fired. The front door was shut against self-report and the side door let it back in,
           under the name "objective".

           Measured comparison (both parties' positions identical every round, the only variable
           being the wording): without rewording → deadlock at round 2; rewording every round →
           stalled stayed 0 for five consecutive rounds.

           Now only a **change in the count** registers: withdrawing one, or raising a new one,
           counts as movement. A count can be gamed too, by splitting sentences, but that
           requires really changing the structure of the statement, which costs far more than
           swapping a few words; and it is written down here rather than pretended to be
           airtight.
        """

        def snapshot(rec):
            return {
                (pid, target): len(on.residuals)
                for pid, stance in rec.stances.items()
                if not stance.unknown
                for target, on in stance.stance_on.items()
            }

        return snapshot(previous) != snapshot(record)

    # ------------------------------------------------------------------ #

    def decide_outcome(
        self, report: ConsensusReport, *, rounds_left: int, budget_exhausted: bool = False
    ) -> Outcome | None:
        """Decide whether to wrap up; ``None`` means keep debating."""
        if report.converged:
            return Outcome.CONSENSUS

        terminating = budget_exhausted or rounds_left <= 0
        stalled = report.stalled_rounds >= self.stability_window
        if not (terminating or stalled):
            return None

        # Reaching here means it is time to wrap up. Explicit opposition → it really was not
        # settled; otherwise downgrade by cause rather than calling it a loss across the board —
        # "not measured" and "opposed" are not the same thing. **Deadlock outranks running out of
        # rounds.** When both hold (several rounds without an inch given, and the round limit
        # reached at the same moment), reporting exhausted downgrades the diagnosis to "a few more
        # rounds would have done it", when the truth is that no number of rounds would. Measured:
        # taking max_rounds from 4 to 5 on the same deliberation flipped the outcome from exhausted
        # to deadlock — the difference being only whether there were enough rounds, which is exactly
        # what must not affect the diagnosis.
        failed = Outcome.DEADLOCK if stalled else Outcome.EXHAUSTED
        if report.opposed:
            return failed
        # The structural floor: with **not one cell measured**, nothing can be called consensus.
        # Otherwise "round 0, nobody has taken a position yet" is judged "consensus with partial
        # coverage", which is absurd. Note the floor is "something was measured", not "an agree was
        # measured" — everyone partial with residuals on record is still consensus with
        # reservations, and must not be called a loss.
        if report.coverage <= 0.0:
            return failed
        if report.coverage < self.min_coverage:
            return failed
        # The confidence bar has to hold on **every** downgrade path, or monotonicity inverts: both
        # sides agreeing without reservation but with low confidence → exhausted; both sides giving
        # only a partial with residuals → consensus_with_reservations. **A weaker agreement bought a
        # better outcome.** With the bar hung only on converged, any downgrade walks straight past
        # it. **Partial reporting and no reporting at all are treated alike.**
        # It used to be `if someone reported: check the minimum / elif nobody did: block`. So three
        # people all leaving it blank was deadlock, while **one** of them writing 0.9 turned it into
        # "consensus with reservations" — the other two still reporting nothing, riding on that one
        # cell's bar. An optional field flipped the whole outcome.
        # In deliberation, deepseek argued "use the information when you have it". But that
        # conflicts with the second bottom line: **not reported is not "no opinion", it is "not
        # measured"**, and default-deny requires that not measured cannot count as passing. Either
        # both places block or both places let through.
        if report.confidences_known and report.min_confidence < self.confidence_threshold:
            return failed
        if report.confidences_known < report.expected_confidences and (
            report.agreed or report.reservations
        ):
            # **Not one person reporting a confidence = the bar cannot be met.** This used to be
            # short-circuited along with everything else by `confidences_known and ...`: with nobody
            # reporting, the bar was skipped entirely, treating "cannot be measured" as "passed" —
            # the opposite of default-deny. Block only when there really are decidable cells; a run
            # that is empty throughout is caught by other gates.
            return failed
        if report.unmeasured:
            # **A "consensus" with partial coverage still needs a consensus first.** With not one
            # agree cell measured, what remains is "someone has reservations + someone was not
            # measured", and calling that any form of consensus is endorsing something that never
            # happened. Measured: a's stance on b was a partial with residuals and b's stance card
            # did not parse ⇒ zero agrees, yet it reported partial_coverage_consensus.
            return Outcome.PARTIAL_COVERAGE_CONSENSUS if report.agreed else failed
        if report.reservations:
            return Outcome.CONSENSUS_WITH_RESERVATIONS
        return failed


def _display_width(text: str) -> int:
    """Terminal display width: CJK characters occupy two columns, so len() will not do."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_matrix(report: ConsensusReport) -> str:
    """Render the disagreement matrix as monospaced text, for the CLI, the report and the TUI."""
    ids = sorted(report.matrix)
    if not ids:
        return t("(no stance data)")
    symbols = {
        "agree": t("agree"),
        "partial": t("partly"),
        "disagree": t("oppose"),
        "unknown": t("unknown"),
    }
    width = max([_display_width(i) for i in ids] + [4])
    lines = [_pad("", width) + "  " + "  ".join(_pad(i, width) for i in ids)]
    for source in ids:
        cells = [
            _pad("—" if source == target else symbols[report.matrix[source][target]], width)
            for target in ids
        ]
        lines.append(_pad(source, width) + "  " + "  ".join(cells))
    return "\n".join(lines)
