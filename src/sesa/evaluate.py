"""Evaluation metrics for a deliberation.

What this module answers is an **existence question**: what did the debate actually change?

    If a participant's position never changes from round 0 to the last round,
    then three rounds of debate = three times the cost for one first draft.

Every metric is computed from the persisted event stream, needing **no ground truth and no
further model calls**. This is the second delivery on "the event stream is the only source
of truth" (the first was resume).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import adoption as adopt
from .i18n import t
from .patch import extract_files
from .record import read_events


@dataclass
class RoundMetrics:
    index: int
    verdicts: dict[str, int] = field(default_factory=dict)  # counts of
    # agree/partial/disagree/unknown
    changed: list[str] = field(default_factory=list)  # those who changed position this
    # round
    degraded: list[str] = field(default_factory=list)  # those whose stance card took the
    # degraded extraction path
    unresolved: int = 0
    min_confidence: float = 0.0
    state: str = ""
    #: participant -> their statement of position this round, used to compute divergence between
    #: participants
    positions: dict[str, str] = field(default_factory=dict)
    #: this round's disagreement matrix, used to compute judgement change without depending on
    #: wording
    matrix: dict[str, dict[str, str]] = field(default_factory=dict)
    #: the verification results the engine executed this round: (participant, exit code)
    evidence: list[tuple[str, int]] = field(default_factory=list)
    #: this round's **self-test** results: (participant, exit code) cross-test results: (test
    #: author, exit code). A failure here is **a valuable signal**, not a problem
    cross_evidence: list[tuple[str, int]] = field(default_factory=list)
    #: "source → target" -> the residual items registered that round
    residuals: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class RunMetrics:
    """The computable metrics of one deliberation."""

    run_id: str
    task: str
    protocol: str
    participants: list[str]
    outcome: str = ""
    rounds: list[RoundMetrics] = field(default_factory=list)
    #: participant -> (round 0 position, final position)
    positions: dict[str, tuple[str, str]] = field(default_factory=dict)
    false_consensus: int = 0
    writer_mismatch: int = 0
    wall_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    unmeasured_usage_calls: int = 0
    chars: int = 0
    #: whether verdict.final ever appeared in the event stream. An interrupted run lacks it
    completed: bool = False
    #: the explicitly recorded reason for the abort (if the run left a run.aborted)
    aborted: str = ""

    # ------------------------------------------------------------------ # Existence metrics
    # ------------------------------------------------------------------ #

    @property
    def rounds_used(self) -> int:
        return len(self.rounds)

    @property
    def usable(self) -> bool:
        """Whether it can enter the statistics.

        An interrupted run (process killed, network lost, Ctrl+C) leaves an incomplete event
        stream: no ``verdict.final``, an empty outcome, drift of 0. **Those zeros silently poison
        the means**, so they have to be excluded before aggregating — the most dangerous thing in
        statistics has never been noise, it is an empty value taken for data.
        """
        return self.completed and bool(self.participants)

    @property
    def stance_changes(self) -> int:
        """How many times "someone changed position" happened over the whole run.

        **Round 0 is excluded**: that round has no "previous round" to compare against, and
        whatever the model puts in ``changed_from_last_round`` can only be noise. With round 0 in
        the numerator and not in the denominator (``rounds[1:]``), the ratio exceeds 1 — so the
        single most important number can go over 100%, and nobody would think to go back and check
        its definition.
        """
        return sum(len(r.changed) for r in self.rounds[1:])

    @property
    def stance_change_rate(self) -> float:
        """The share of positions that changed among all positions taken.

        **This is the single most important number**: a persistent 0 means the debate is only
        adding detail, nobody is actually being convinced, and the money spent on extra rounds is
        unjustified.
        """
        opportunities = sum(len(self.participants) for r in self.rounds[1:])
        return self.stance_changes / opportunities if opportunities else 0.0

    @property
    def position_drift(self) -> dict[str, float]:
        """Each person's **surface-form** difference between round 0 and their final position
        (0 = not one word changed).

        .. warning::
           **This is not a valid measure of change of position; it measures change of wording.**
           Calibration:

           ========================== ======
           case                        value
           ========================== ======
           same conclusion, reworded   0.733
           opposite conclusions        0.394
           ========================== ======

           "Said differently" scores higher than "opposite conclusion" — across the interval that
           matters it is **inverted**. Real deliberations land at 0.62–0.92, right in the
           "same conclusion, reworded" band.

           To measure whether the debate really moved anyone, use :attr:`verdict_movement`
           (category change, independent of wording) and :attr:`stance_change_rate` (their own
           report).
        """
        out: dict[str, float] = {}
        for pid, (first, last) in self.positions.items():
            if not first or not last:
                continue
            out[pid] = round(1 - difflib.SequenceMatcher(None, first, last).ratio(), 3)
        return out

    @property
    def mean_drift(self) -> float:
        drifts = list(self.position_drift.values())
        return round(sum(drifts) / len(drifts), 3) if drifts else 0.0

    @property
    def divergence_by_round(self) -> dict[int, float]:
        """The average pairwise difference **between** participants' positions each round
        (0 = saying the same thing, 1 = nothing in common).

        .. warning::
           Like :attr:`position_drift` it rests on surface-form similarity and **has the same
           validity problem**: "same conclusion, reworded" scores high. It is usable only as a
           rough screen for "are they just paraphrasing each other" (reliable as it approaches 0),
           and not for comparing how substantively two configurations disagree.

        It corresponds directly to the "homogeneous assent" failure mode — a table of people saying
        nearly the same thing while the disagreement matrix reports agreement. Assigning different
        stances (Roles) to different participants is claimed to raise exactly this number.
        """
        out: dict[int, float] = {}
        for record in self.rounds:
            positions = [p for p in record.positions.values() if p.strip()]
            if len(positions) < 2:
                continue
            pairs = [
                1 - difflib.SequenceMatcher(None, a, b).ratio()
                for i, a in enumerate(positions)
                for b in positions[i + 1 :]
            ]
            out[record.index] = round(sum(pairs) / len(pairs), 3)
        return out

    @property
    def final_divergence(self) -> float | None:
        """Divergence between participants in **the final round**; ``None`` = nothing measured in the
        last round.

        Two holes, opposite in direction and the same at root:

        1. It used to take ``by_round[max(by_round)]``. ``divergence_by_round`` records only rounds
           with "at least two non-empty positions", so what comes back is **the highest round with
           data**, not necessarily the final round — when someone degrades to an empty turn in the
           last round, this quietly falls back to an earlier one while still being called final.
        2. Changing it to "return 0.0 when the last round has no data" is equally wrong: **0.0
           means "a table of people paraphrasing each other"**, a strong conclusion. Using it as
           the "no data" return has missing measurement pass for a specific finding.
        """
        by_round = self.divergence_by_round
        last = len(self.rounds) - 1
        return by_round.get(last)

    @property
    def verdict_transitions(self) -> list[tuple[int, str, str, str, str]]:
        """Cells where **the judgement changed** between two adjacent rounds: (round, source, target,
        old, new).

        This is the measure of movement that does not depend on wording. ``agree/partial/disagree``
        are categorical, and "A's view of B went from disagree to agree" is a definite,
        semantically clear move that a rewording cannot manufacture — whereas surface-form
        similarity can.
        """
        out: list[tuple[int, str, str, str, str]] = []
        for prev, curr in zip(self.rounds, self.rounds[1:], strict=False):
            for source, row in curr.matrix.items():
                for target, verdict in row.items():
                    before = prev.matrix.get(source, {}).get(target)
                    if before and before != verdict:
                        out.append((curr.index, source, target, before, verdict))
        return out

    @property
    def real_transitions(self) -> list[tuple[int, str, str, str, str]]:
        """**Really changing their mind**: excluding first positions such as ``unknown → a position``.

        In round 0 nobody has read anybody and every cell is unknown; when the first positions are
        taken in round 1, every cell "changed" — that is speaking for the first time, not changing
        one's mind. Counting it inflates the conclusion "the debate changed positions" out of
        nowhere.
        """
        return [t for t in self.verdict_transitions if t[3] != "unknown"]

    @property
    def verdict_movement(self) -> float:
        """The share of cells where the judgement really changed. **This is the primary evidence for
        "what the debate changed".**

        Unlike surface-form drift it cannot be fooled by a rewording: if the category did not
        change, nothing changed.
        The denominator likewise counts only the opportunities **after both sides have taken a
        position**, excluding first positions.
        """
        opportunities = 0
        for prev, curr in zip(self.rounds, self.rounds[1:], strict=False):
            for source, row in curr.matrix.items():
                for target in row:
                    if prev.matrix.get(source, {}).get(target, "unknown") != "unknown":
                        opportunities += 1
        return len(self.real_transitions) / opportunities if opportunities else 0.0

    @property
    def toward_agreement(self) -> int:
        """The number of moves towards "more agreement" (disagree→partial/agree, partial→agree).

        **It uses the same ruler as** :attr:`real_transitions`: anything with ``unknown`` on either
        end does not count. ``unknown`` and ``disagree`` used to share rank 0, so
        "not measured → partial" counted as "moved towards agreement" — taking missing data for
        opposition and then taking its disappearance for progress. Measured: a run where every real
        move was away from agreement reported 2 moves towards it.
        """
        rank = {"disagree": 0, "partial": 1, "agree": 2}
        return sum(
            1
            for _, _, _, before, after in self.real_transitions
            if before in rank and after in rank and rank[after] > rank[before]
        )

    @property
    def residual_counts(self) -> dict[int, int]:
        """The residual count per round. **This is the one uncontested observable in the residual
        layer.**
        """
        return {r.index: sum(len(v) for v in r.residuals.values()) for r in self.rounds}

    @property
    def residual_flow(self) -> list[tuple[int, int, int, int]]:
        """The per-round item flow: (round, previous count, current count, current balance).

        .. warning::
           **Do not read it as "withdrawn/added".** Items are compared as exact strings, and an LLM
           almost never repeats itself verbatim — measured, **0** of 51 residuals were identical
           word for word. So "no longer listed" always equals the previous round's entire count and
           "added" always equals this round's entire count: these two numbers are **structurally
           inevitable constants, not measurements**.

           ``residual_turnover``, once computed from this, came out at 0.89–1.00 across six
           deliberations, because all it actually reflected was the change in count. That metric
           has been removed.

           For whether the content advanced, see :meth:`residual_similarity` (semantic), and it must
           be read together with :attr:`residual_granularity`.
        """
        out: list[tuple[int, int, int, int]] = []
        for prev, curr in zip(self.rounds, self.rounds[1:], strict=False):
            before = sum(len(v) for v in prev.residuals.values())
            after = sum(len(v) for v in curr.residuals.values())
            if not before:
                continue  # no residuals last round = a first registration, not a move
            out.append((curr.index, before, after, after))
        return out

    @property
    def residual_trend(self) -> int:
        """The difference between the final balance and the first comparable round's count. A negative
        value means the reservations are converging.
        """
        flow = self.residual_flow
        return flow[-1][2] - flow[0][1] if flow else 0

    @property
    def residual_granularity(self) -> tuple[float, float, float]:
        """The granularity of the residual items: (mean items per round, mean item length, coefficient
        of variation of length).

        **It must be read together with** :meth:`residual_similarity`. Measured (n=6):

        ============================== =========
        correlation with similarity     Pearson r
        ============================== =========
        items per round                   +0.830
        mean item length                  +0.858
        ============================== =========

        Both mechanisms point the same way: more candidates make it easier for ``max()`` to hit a
        high score; longer text shares more words and lifts the cosine. So **the similarity is very
        nearly determined by the model's writing style** — how many items it writes and how long
        they are — and not by the debate itself.

        The coefficient of variation is the round-to-round fluctuation of length **within** one run.
        The within-run median is about 0.10 and the across-run figure about 0.48, so comparison
        across configurations is essentially invalid, while comparison across rounds within one run
        still requires checking that this number is small enough.
        """
        import statistics

        counts, lengths = [], []
        for record in self.rounds:
            items = [x for v in record.residuals.values() for x in v]
            if not items:
                continue
            counts.append(len(items))
            lengths.append(statistics.mean(len(x) for x in items))
        if not counts:
            return (0.0, 0.0, 0.0)
        mean_len = statistics.mean(lengths)
        cv = statistics.stdev(lengths) / mean_len if len(lengths) > 1 and mean_len else 0.0
        return (
            round(statistics.mean(counts), 1),
            round(mean_len, 1),
            round(cv, 3),
        )

    def comparable_with(self, other: RunMetrics, tolerance: float = 0.25) -> bool:
        """Whether two deliberations' residual granularity is close enough for their similarities to be
        compared.

        When granularity differs too much, comparing similarity compares writing styles rather than
        debates — this project once drew a "the two groups are strikingly different" conclusion from
        exactly that and withdrew it.
        """
        a_count, a_len, _ = self.residual_granularity
        b_count, b_len, _ = other.residual_granularity
        if not (a_len and b_len and a_count and b_count):
            return False
        return (
            abs(a_len - b_len) / max(a_len, b_len) <= tolerance
            and abs(a_count - b_count) / max(a_count, b_count) <= tolerance
        )

    def _fresh_similarities(self, model: str | None = None) -> list[float]:
        """The **highest** similarity of each newly added residual to a residual of the same direction
        last round.

        "Newly added" is determined by exact string comparison — measured, 0 items were identical
        word for word, so nearly every item this round enters the comparison. That is not a defect:
        whether the content advanced is properly a question for the semantic layer, and the
        character layer only picks out candidates.
        """
        from .semantic import DEFAULT_MODEL, similarity_matrix

        model = DEFAULT_MODEL if model is None else model
        out: list[float] = []
        for prev, curr in zip(self.rounds, self.rounds[1:], strict=False):
            for pair, items in curr.residuals.items():
                before = prev.residuals.get(pair, [])
                if not before:
                    continue  # no residuals last round = a first registration, not a move
                fresh = [x for x in items if x not in set(before)]
                if not fresh:
                    continue
                out.extend(max(row) for row in similarity_matrix(fresh, before, model))
        return out

    def residual_similarity(self, model: str | None = None) -> float | None:
        """The **median** similarity of the newly added residuals to the previous round. ``None`` means
        there is nothing comparable.

        **This is the preferred metric because it needs no threshold.** Higher means the newly
        raised reservations resemble what was said last round (reworded); lower means they are new
        questions.

        .. warning::
           **It must be read together with** :attr:`residual_granularity`, and never compared on its
           own. Measured, this score correlates with "items per round" at r=+0.830 and with "mean
           item length" at r=+0.858 (n=6) — it is very nearly determined by the model's writing
           style rather than by the debate. Before comparing across configurations, confirm
           comparable granularity with :meth:`comparable_with`.

        Why not a binary classification: measured, real residuals' similarities lie on a continuum
        from 0.44 to 0.81 with a median of 0.634, and any threshold cuts right through the middle of
        the distribution — move it by 0.05 and the conclusion flips. The once-reported "one group
        1.00, the other 0.00" was exactly that threshold illusion (see DESIGN.md §14.6).
        """
        import statistics

        scores = self._fresh_similarities(model)
        return round(statistics.median(scores), 3) if scores else None

    def restatement_rate(self, threshold: float | None = None, model: str | None = None) -> float:
        """The share of newly added residuals judged "reworded".

        .. warning::
           **Extremely sensitive to the threshold**: over the same data, a threshold of 0.50 gives
           0.94 and 0.80 gives 0.02. Real similarities lie on a continuum with no natural cut point.
           To compare two configurations use :meth:`residual_similarity` (no threshold), or use
           :meth:`restatement_sensitivity` to confirm the conclusion holds across the interval.
        """
        from .semantic import DEFAULT_THRESHOLD

        threshold = DEFAULT_THRESHOLD if threshold is None else threshold
        scores = self._fresh_similarities(model)
        return sum(1 for s in scores if s >= threshold) / len(scores) if scores else 0.0

    def restatement_sensitivity(
        self, thresholds: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8), model: str | None = None
    ) -> dict[float, float]:
        """How the rewording rate varies with the threshold. **A conclusion counts only if it holds
        across the whole interval.**
        """
        scores = self._fresh_similarities(model)
        if not scores:
            return {}
        return {t: round(sum(1 for s in scores if s >= t) / len(scores), 3) for t in thresholds}

    @property
    def evidence_ceiling(self) -> bool:
        """Whether everyone's **self-tests** passed.

        **Self-tests only**: a cross-test failure is precisely a valuable signal (it means the
        parties' implementations or tests really do disagree), and counting it towards "too easy" is
        wrong.

        All-green self-tests mean the task is **too easy** for the current participants — they get
        it right working alone, and the debate has no room to improve anything. Measured: a parsing
        problem with 6 error boundaries had all four implementations, debate group and reflect group
        alike, scoring full marks on the held-out tests, while the debate spent 17% more words and
        15% more time.

        **Under a ceiling effect no method can measure a difference**, and extra rounds of debate
        are pure surcharge.
        """
        checks = [code for record in self.rounds for _, code in record.evidence]
        return bool(checks) and all(code == 0 for code in checks)

    @property
    def degraded_rate(self) -> float:
        """The share of stance cards that took the degraded path — the true reading of "format
        compliance".
        """
        total = sum(len(self.participants) for _ in self.rounds)
        return sum(len(r.degraded) for r in self.rounds) / total if total else 0.0

    @property
    def final_unresolved(self) -> int:
        return self.rounds[-1].unresolved if self.rounds else 0


def measure(run_dir: Path) -> RunMetrics:
    """Compute every metric from one deliberation's event stream."""
    events = read_events(Path(run_dir))
    start = next((e for e in events if e["t"] == "run.start"), {})

    metrics = RunMetrics(
        run_id=start.get("run_id", Path(run_dir).name),
        task=(start.get("task", "").splitlines() or [""])[0][:60],
        protocol=start.get("protocol", ""),
        participants=list(start.get("participants") or []),
    )

    rounds: dict[int, RoundMetrics] = {}
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    timestamps = [e["ts"] for e in events if "ts" in e]

    for event in events:
        kind = event["t"]
        if kind == "round.start":
            rounds.setdefault(event["round"], RoundMetrics(event["round"]))

        elif kind == "stance.emit":
            record = rounds.setdefault(event["round"], RoundMetrics(event["round"]))
            stance = event.get("stance") or {}
            pid = event["participant"]
            for verdict in (stance.get("stance_on") or {}).values():
                record.verdicts[verdict] = record.verdicts.get(verdict, 0) + 1
            if stance.get("changed"):
                record.changed.append(pid)
            if event.get("degraded"):
                record.degraded.append(pid)
            for target, items in (stance.get("residuals") or {}).items():
                record.residuals[f"{pid} → {target}"] = list(items)
            if position := (stance.get("position") or "").strip():
                first_seen.setdefault(pid, position)
                last_seen[pid] = position
                record.positions[pid] = position

        elif kind == "consensus.update":
            record = rounds.setdefault(event["round"], RoundMetrics(event["round"]))
            record.unresolved = event.get("unresolved", 0)
            record.min_confidence = event.get("min_confidence", 0.0)
            record.state = event.get("state", "")
            record.matrix = event.get("matrix") or {}

        elif kind == "turn.end":
            metrics.chars += event.get("chars", 0)
            usage = event.get("usage") or {}
            if usage.get("known"):
                metrics.input_tokens += usage.get("in") or 0
                metrics.output_tokens += usage.get("out") or 0
            else:
                metrics.unmeasured_usage_calls += 1

        elif kind == "run.aborted":
            metrics.aborted = event.get("reason", t("aborted from outside"))
        elif kind == "evidence":
            record = rounds.setdefault(event["round"], RoundMetrics(event["round"]))
            entry = (event["participant"], event.get("exit_code", 1))
            # A cross-test's cmd carries "×", and it means something different from a self-test: a
            # failure in the first is a signal, in the second a problem
            if "×" in event.get("cmd", ""):
                record.cross_evidence.append(entry)
            else:
                record.evidence.append(entry)
        elif kind == "false_consensus":
            metrics.false_consensus += 1
        elif kind == "writer.mismatch":
            metrics.writer_mismatch += 1
        elif kind == "verdict.final":
            metrics.outcome = event.get("outcome", "")
            metrics.completed = True

    metrics.rounds = [rounds[i] for i in sorted(rounds)]
    metrics.positions = {
        pid: (first_seen.get(pid, ""), last_seen.get(pid, "")) for pid in metrics.participants
    }
    if timestamps:
        metrics.wall_seconds = round(max(timestamps) - min(timestamps), 1)
    return metrics


# --------------------------------------------------------------------------- # Lifting a rival's
# code wholesale (the retrospective version: reconstructed from the persisted turns)
# --------------------------------------------------------------------------- #

#: The same threshold as the engine uses; do not keep two copies of it.
ADOPTION_THRESHOLD = adopt.THRESHOLD
Adoption = adopt.Adoption
AdoptionReport = adopt.Report


def _files_by_round(run_dir: Path) -> dict[tuple[int, str], dict[str, str]]:
    """Reconstruct the files each person handed in each round from the persisted turns.

    Valid only for participants whose files the engine writes for them: an agent CLI writes its
    own files and the code never enters the turn's prose.
    During a deliberation the engine takes a different route — snapshotting the working copy
    directly (``adoption.snapshot``), which covers both kinds of participant. This is for
    computing it after the fact over **old records that have already finished**.
    """
    out: dict[tuple[int, str], dict[str, str]] = {}
    for turn in sorted((run_dir / "turns").glob("r*_*.md")):
        head = turn.stem.split("_")
        if len(head) < 4 or not head[0].startswith("r"):
            continue
        try:
            index = int(head[0][1:])
        except ValueError:
            continue
        if files := extract_files(turn.read_text(encoding="utf-8")):
            out[(index, head[2])] = files
    return out


def code_adoption(run_dir: Path, *, threshold: float = adopt.THRESHOLD) -> adopt.Report:
    """Compute the copying events for a deliberation that has already finished.

    .. warning::
       It reports only that **copying happened**, never that "copying was harmful". The
       difference between groups (debate regressing 4/16 vs reflect 0/16) has Fisher p=0.101,
       **not significant**. Good or bad is answered by execution evidence — see the ``adoption``
       module and DESIGN.md 14.18.
    """
    by_round = _files_by_round(run_dir)
    if not by_round:
        return adopt.Report(
            False,
            t(
                "no code block with a path appears in any turn — this run had no "
                "participant whose files the engine writes for them, or the participants "
                "are agent CLIs that write their own files"
            ),
        )
    if len({pid for _, pid in by_round}) < 2:
        return adopt.Report(
            False, t("only one participant produced code; there is nothing to compare")
        )

    rounds = sorted({index for index, _ in by_round})
    found: list[adopt.Adoption] = []
    for previous_index, index in pairwise(rounds):
        previous = {pid: f for (r, pid), f in by_round.items() if r == previous_index}
        current = {pid: f for (r, pid), f in by_round.items() if r == index}
        found += adopt.detect(previous, current, round_index=index, threshold=threshold)
    return adopt.Report(True, events=found)


def collect(root: Path, *, only_usable: bool = True) -> list[RunMetrics]:
    """Scan the deliberation records under ``.sesa/runs/``.

    By default it returns only **finished** records. Pass ``only_usable=False`` for all of them,
    to report to the user how many were interrupted.
    """
    runs_dir = Path(root) / "runs"
    if not runs_dir.exists():
        return []
    out = []
    for entry in sorted(runs_dir.iterdir()):
        if not (entry / "events.jsonl").exists():
            continue
        try:
            metrics = measure(entry)
        except (ValueError, KeyError, OSError):
            continue  # one bad record should not destroy a whole report
        if not metrics.participants:
            # without a run.start there is nothing to attribute it to, and putting it in the
            # comparison table would only poison the statistics
            continue
        if only_usable and not metrics.usable:
            continue
        out.append(metrics)
    return out


def to_dict(metrics: RunMetrics) -> dict[str, Any]:
    """Structured output, for wiring into someone else's analysis pipeline."""
    return {
        "run_id": metrics.run_id,
        "completed": metrics.completed,
        "aborted": metrics.aborted,
        "task": metrics.task,
        "protocol": metrics.protocol,
        "participants": metrics.participants,
        "outcome": metrics.outcome,
        "rounds_used": metrics.rounds_used,
        "stance_changes": metrics.stance_changes,
        "stance_change_rate": round(metrics.stance_change_rate, 3),
        "position_drift": metrics.position_drift,
        "mean_drift": metrics.mean_drift,
        "divergence_by_round": metrics.divergence_by_round,
        "final_divergence": metrics.final_divergence,
        "verdict_movement": round(metrics.verdict_movement, 3),
        "verdict_transitions": len(metrics.real_transitions),
        "toward_agreement": metrics.toward_agreement,
        "residual_flow": metrics.residual_flow,
        "residual_trend": metrics.residual_trend,
        "residual_granularity": metrics.residual_granularity,
        "degraded_rate": round(metrics.degraded_rate, 3),
        "evidence_ceiling": metrics.evidence_ceiling,
        "cross_test_failures": sum(
            1 for r in metrics.rounds for _, code in r.cross_evidence if code != 0
        ),
        "final_unresolved": metrics.final_unresolved,
        "false_consensus": metrics.false_consensus,
        "writer_mismatch": metrics.writer_mismatch,
        "wall_seconds": metrics.wall_seconds,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "unmeasured_usage_calls": metrics.unmeasured_usage_calls,
        "chars": metrics.chars,
    }
