"""Deterministic rendering of RESULT.md / REPORT.md.

**A fixed skeleton** is a hard guarantee: agreement and disagreement change how
much each section contains, never the shape of the document. The reader's way
in stays the same, rather than the format changing depending on whether the
participants happened to agree.

The rapporteur supplies content only; the skeleton is rendered here — so
"fixed" does not depend on a model behaving itself every time.

**The deliverable is rendered in the task's language, not the interface's.**
It is the deliberation written down, and an English "## Conclusion" over
Chinese prose is a document nobody asked for. See :class:`sesa.i18n.scoped`.
"""

from __future__ import annotations

import re

from .consensus.matrix import render_matrix
from .i18n import scoped, t
from .prompts import pick_language
from .state import DeliberationState
from .types import ConsensusReport, Outcome, Result

#: Outcome → the banner at the top of RESULT.md. Values are English source strings;
#: :func:`sesa.i18n.t` translates them at render time.
OUTCOME_BANNER = {
    Outcome.CONSENSUS: "✅ **Consensus reached**",
    Outcome.CONSENSUS_WITH_RESERVATIONS: (
        "🟡 **Consensus with reservations** — nobody objected outright, but the "
        "reservations below are unresolved"
    ),
    Outcome.DEADLOCK: (
        "⚠️ **No consensus — the deliberation deadlocked** (several rounds running "
        "with nobody moving and the disagreement not shrinking)"
    ),
    Outcome.EXHAUSTED: (
        "⏳ **Unfinished** — rounds or budget ran out; below is where it stood when it stopped"
    ),
    Outcome.FALSE_CONSENSUS: (
        "🔁 **False consensus detected** — the stance cards claim agreement while "
        "the statements conflict on substance"
    ),
    Outcome.PARTIAL_COVERAGE_CONSENSUS: (
        "🟠 **Consensus over partial coverage** — nobody objected in what was "
        "measured, but some cells were never measured at all, and **not measured "
        "is not agreement**"
    ),
    Outcome.NOT_MEASURED: (
        "⚪ **This protocol does not measure consensus** — everyone answers "
        "independently and never sees the others, so there is nothing to assess"
    ),
}
# A missing banner entry degrades the top of RESULT.md into a bare enum value
# (`partial_coverage_consensus`), handing the reader an internal identifier instead of an
# explanation. The test below pins that down. Use raise rather than assert: `python -O` strips
# assert lines entirely, so this integrity guard would silently vanish in production — while what it
# guards against is exactly the kind of thing ("the outcome degrading to a bare enum value") that
# only shows up on a real run.
if set(OUTCOME_BANNER) != set(Outcome):
    raise RuntimeError(
        f"No banner configured for these outcomes: {sorted(set(Outcome) - set(OUTCOME_BANNER))}"
    )


def short_title(task: str, limit: int = 60) -> str:
    """Take one heading-like line out of the topic.

    A topic is often an entire document (``--file rfc.md``), and pushing that into an H1 turns
    the whole document into a heading. Take the first line with content in it, strip the
    markdown markers, and truncate if over-long.
    """
    for raw in task.splitlines():
        # `lstrip("#")` strips **every** leading #, including a `### Important` inside a
        # Python or
        # shell comment, and even the first character of a whole `#!/usr/bin/env python`
        # line.
        # Markdown headings go to six levels at most, so strip by heading syntax: a # must
        # be
        # followed by whitespace.
        line = re.sub(r"^#{1,6}\s+", "", raw.strip()).strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return t("(untitled)")


def _cell(text: str) -> str:
    """Fit the model's prose into a markdown table cell without bursting it.

    Pipes have to be escaped and newlines flattened — miss either and the table's skeleton
    falls apart.
    """
    flattened = " ".join(str(text).split())
    return flattened.replace("|", "\\|")


def render_result(result: Result) -> str:
    """Render RESULT.md. Four sections always exist; empty ones say so."""
    # **The deliverable speaks the task's language, not the interface's.**
    with scoped(pick_language(result.task)):
        return _render_result(result)


def _render_result(result: Result) -> str:
    lines = [f"# {short_title(result.task)}", ""]
    lines.append(t(OUTCOME_BANNER.get(result.outcome, str(result.outcome))))
    meta = [t("round {n}", n=result.rounds_used)]
    if result.drafted_by:
        meta.append(t("drafted by {who}", who=result.drafted_by))
    if result.usage.known and result.usage.usd:
        meta.append(t("cost ${amount}", amount=f"{result.usage.usd:.2f}"))
    lines += [f"<sub>{' · '.join(meta)} · run `{result.run_id}`</sub>", ""]
    if len(result.task.strip()) > 200:
        lines += [
            "<details><summary>" + t("full task") + "</summary>",
            "",
            result.task.strip(),
            "",
            "</details>",
            "",
        ]
    lines += ["---", ""]

    # ── someone never managed a complete turn (the reader has to know this first) ──────
    if result.truncated_turns:
        listed = "、".join(
            t("{id} ({n} rounds)", id=pid, n=n) for pid, n in sorted(result.truncated_turns.items())
        )
        lines += [
            "> [!WARNING]",
            "> " + t("**Statements by {who} were cut off by the output budget.**", who=listed),
            ">",
            "> "
            + t(
                "A truncated statement's stance card is not accepted (half a sentence "
                "is not a position), and its code is usually unfinished too. **If "
                "someone was truncated in every round they did not really take part** "
                "— the conclusion below was formed without them. Raise that "
                "participant's `max_tokens`, or ask them to say less."
            ),
            "",
        ]

    # ── asymmetric material (the reader has to know this first) ───────────────────────
    if result.briefings:
        listed = "、".join(
            t("{id} ({n} chars)", id=pid, n=n) for pid, n in sorted(result.briefings.items())
        )
        lines += [
            "> [!NOTE]",
            "> "
            + t(
                "**The participants did not see the same material**: {who} received "
                "private briefings the others could not see.",
                who=listed,
            ),
            ">",
            "> "
            + t(
                "Disagreement here **may be an information gap rather than a "
                "judgement gap** — one side knowing something the other does not "
                "makes their disagreement something other than a clash of views. "
                "The originals are in `briefings/`."
            ),
            "",
        ]

    # ── who it converged on (placed before the conclusion) ────────────────────────────
    # The risk in a debate is not failing to converge, it is converging on the wrong side.
    # The
    # reader should know whether this conclusion was bought with a regression before they
    # read
    # the conclusion.
    if regressive := result.regressive_adoptions:
        lines += [
            "> [!WARNING]",
            "> " + t("**This conclusion rests on a step backwards.**"),
            ">",
        ]
        for rd, who, foe, path, peer, own, _ in regressive:
            lines.append(
                "> "
                + t(
                    "Round {round}: **{who} abandoned their own `{path}` for {foe}'s** "
                    "({peer} similar to theirs, only {own} left of their own previous "
                    "version), and their self-test **went from passing to failing as a "
                    "result**.",
                    round=rd,
                    who=who,
                    path=path,
                    foe=foe,
                    peer=f"{peer:.0%}",
                    own=f"{own:.0%}",
                )
            )
        kept = "`、`".join(result.branches.values())
        lines += [
            ">",
            "> "
            + t(
                "They did agree — but they agreed on the wrong thing. The "
                "implementation that lost out is still on its branch: `{branches}`.",
                branches=kept or t("(not kept)"),
            ),
            "",
        ]
    elif result.adoptions:
        lines += [
            "> [!NOTE]",
            "> " + t("Someone **copied a peer's work wholesale** in this run:"),
        ]
        for rd, who, foe, path, peer, own, _ in result.adoptions:
            lines.append(
                "> "
                + t(
                    "Round {round}: {who}'s `{path}` is {peer} similar to {foe}'s "
                    "previous round, with only {own} left of their own.",
                    round=rd,
                    who=who,
                    path=path,
                    foe=foe,
                    peer=f"{peer:.0%}",
                    own=f"{own:.0%}",
                )
            )
        lines += [
            ">",
            "> "
            + t(
                "This is a statement of fact — the execution evidence does not show "
                "it made things worse, and they may simply have been persuaded. "
                "**Converging on a peer and converging on the right answer are two "
                "different things.**"
            ),
            "",
        ]

    # ── the conclusion ────────────────────────────────────────────────────────────────
    lines += ["## " + t("Conclusion"), ""]
    if result.conclusion.strip():
        lines += [result.conclusion.strip(), ""]
    else:
        lines += [
            t(
                "The participants agreed on nothing of substance, so this section is "
                "empty. Go straight to «Open disagreements»."
            ),
            "",
        ]

    # ── the grounds for consensus ─────────────────────────────────────────────────────
    lines += ["## " + t("Grounds for the consensus"), ""]
    if result.grounds:
        lines += [f"- {g}" for g in result.grounds] + [""]
    else:
        lines += [t("(none)"), ""]

    # ── open disagreements ────────────────────────────────────────────────────────────
    lines += ["## " + t("Open disagreements"), ""]
    if not result.disagreements:
        if result.outcome is Outcome.NOT_MEASURED:
            # "Not measured" is not "not settled". Nobody sees anybody throughout this protocol, so
            # there is no peer assessment to speak of — carrying over the "no consensus was reached"
            # line below would be labelling missing data as disagreement.
            lines += [
                t(
                    "This protocol **does not measure consensus**: everyone answers "
                    "independently and never sees the others, so there is nothing to "
                    "assess — neither agreement nor disagreement. Each participant's "
                    "conclusion is listed below and in `turns/`."
                ),
                "",
            ]
        elif result.outcome is Outcome.PARTIAL_COVERAGE_CONSENSUS:
            # The banner says "consensus with partial coverage"; if the prose still said "no
            # consensus was reached", one document would contradict itself. What was measured really
            # did meet no objection, and what was not measured is stated honestly.
            lines += [
                t(
                    "Nobody objected **in what was measured**. But some cells were "
                    "never measured at all — see «What was not measured» below. "
                    "**Not measured is not agreement.**"
                ),
                "",
            ]
        elif result.outcome is Outcome.CONSENSUS_WITH_RESERVATIONS:
            # The banner says "reservations unresolved" and the residuals are listed below one by
            # one; if the prose still said "agreement on every substantive question", the document
            # would be fighting itself.
            lines += [
                t(
                    "Nobody objected outright. But **the reservations are "
                    "unresolved** — they are listed under «Reservations» below, and "
                    "on those points the participants do not agree."
                ),
                "",
            ]
        elif result.outcome is Outcome.CONSENSUS:
            lines += [t("None. The participants agree on every point of substance."), ""]
        else:
            # No consensus and yet no disagreement can be listed means the record itself has a
            # problem — say so, do not pretend to agreement
            lines += [
                t(
                    "No specific disagreements could be listed, but **this "
                    "deliberation did not reach consensus** (outcome: `{outcome}`). "
                    "See the matrix in `REPORT.md` and the originals in `turns/`.",
                    outcome=result.outcome.value,
                ),
                "",
            ]
    else:
        for i, d in enumerate(result.disagreements, 1):
            lines += ["### " + t("Disagreement {n}: {topic}", n=i, topic=d.topic), ""]
            if d.positions:
                lines += [
                    "| " + " | ".join((t("participant"), t("position"), t("reasoning"))) + " |",
                    "|---|---|---|",
                ]
                for pid, position in d.positions.items():
                    # Escaping pipes is not enough: positions and reasons are prose written by a
                    # model and **often contain newlines**, and one newline splits that table row
                    # into two and takes the whole table's structure with it — while "the skeleton
                    # is constant" is exactly what this deliverable promises.
                    lines.append(f"| {pid} | {_cell(position)} | {_cell(d.reasons.get(pid, ''))} |")
                lines.append("")
            if d.root_cause:
                lines += [t("**Root cause**: {cause}", cause=d.root_cause), ""]
            if d.decisive_question:
                lines += [
                    t("**What would settle it**: {question}", question=d.decisive_question),
                    "",
                    t(
                        '**Next**: `sesa resume {run} --inject "<your answer>"`',
                        run=result.run_id,
                    ),
                    "",
                ]
            else:
                # Bottom line 4: **an open disagreement must come with a way out**. When the
                # rapporteur wrote no decisive question (or the disagreement was filled in
                # mechanically by the engine from the stance cards in the first place), there still
                # has to be a road forward, or the reader gets two contradictory essays and a
                # silence.
                lines += [
                    t(
                        "**What would settle it**: the rapporteur did not say. If you "
                        "hold something that would settle it, feed it in:"
                    ),
                    t('`sesa resume {run} --inject "<your information>"`', run=result.run_id),
                    "",
                ]

    # ── next steps (any unsettled outcome must come with a way out) ───────────────────
    if result.outcome in (Outcome.DEADLOCK, Outcome.EXHAUSTED, Outcome.FALSE_CONSENSUS):
        lines += ["## " + t("Next"), ""]
        lines += [
            t(
                "This run did not reach consensus. The fourth bottom line requires "
                "that **an open disagreement always comes with a way forward** — even "
                "when not a single specific disagreement could be listed (everyone "
                "failed, say, or coverage was zero), the reader should get more than "
                "«they did not agree»."
            ),
            "",
            t(
                "- Add something that would settle it and resume: "
                '`sesa resume {run} --inject "<information>"`',
                run=result.run_id,
            ),
            t(
                "- Read the statements and the matrix: `sesa report {run}`",
                run=result.run_id,
            ),
            "",
        ]

    # ── objective evidence (code tasks) ───────────────────────────────────────────────
    if result.cross_test:
        lines += ["## " + t("Cross-testing"), ""]
        lines += [
            t(
                "Everyone tends to be green on their own tests — when the person who "
                "wrote the implementation also wrote the tests, green says almost "
                "nothing."
            ),
            t("The table below is **A's tests run against B's implementation**:"),
            "",
            "```",
            result.cross_test,
            "```",
            "",
        ]
        if result.universally_passing:
            lines += [
                t(
                    "**Implementations that pass everyone's tests**: {who} — the "
                    "hardest evidence this run produced.",
                    who="、".join(result.universally_passing),
                ),
                "",
            ]
        else:
            lines += [
                t(
                    "**No implementation passes everyone's tests.** That can mean "
                    "every implementation has gaps, or that some tests encode their "
                    "author's private assumptions — reading the table cell by cell "
                    "tells you which."
                ),
                "",
            ]

    if result.branches:
        lines += [
            "## " + t("Branches"),
            "",
            t(
                "**All of them are kept. An implementation that lost out is where a "
                "minority view lives.**"
            ),
            "",
        ]
        for pid, branch in sorted(result.branches.items()):
            mark = (
                " ✅ " + t("passes everyone's tests") if pid in result.universally_passing else ""
            )
            lines.append(f"- `{branch}`（{pid}）{mark}")
        lines += [
            "",
            "```bash",
            f"git diff {' '.join(sorted(result.branches.values()))}",
            "```",
            "",
        ]

    # ── premises (the conclusion holds only under these) ──────────────────────────────
    if result.premises:
        lines += ["## " + t("Premises this conclusion rests on"), ""]
        lines += [
            t(
                "Change any one of them and the conclusion has to be re-judged. If one "
                "does not hold in your situation, resume from the break point with "
                '`sesa resume {run} --inject "<what overturns it>"`.',
                run=result.run_id,
            ),
            "",
        ]
        for pid, items in sorted(result.premises.items()):
            if not items:
                continue
            lines.append(f"**{pid}**")
            lines += [f"- {x}" for x in items]
            lines.append("")

    # ── what was not measured (quantified, not just described) ────────────────────────
    # When a protocol structurally produces no peer assessment, those cells **do not exist**
    # and
    # "not measured" does not apply — listing something that does not exist as missing data
    # is
    # the same "labelling the unmeasured" error committed in the other direction.
    if result.outcome is not Outcome.NOT_MEASURED and (
        result.coverage < 1.0 or result.unmeasured_cells
    ):
        lines += ["## " + t("What was not measured"), ""]
        lines += [
            t(
                "**{pct} of the cells were measured.** What was not measured is not "
                "disagreement — it is missing data, and counts as neither agreement "
                "nor objection.",
                pct=f"{result.coverage:.0%}",
            ),
            "",
        ]
        unverified = set(result.unverified_agreements)
        unverifiable = set(result.unverifiable_agreements)

        def _why(cell: str) -> str:
            if cell in unverified:
                return "　←　**" + t("said agree, submitted no verification") + "**"
            if cell in unverifiable:
                # They did submit one; it said they could not check. Writing it as "did not submit"
                # wrongs them — and this project promised the participants that "could not check is
                # a respectable answer".
                return (
                    "　←　**"
                    + t("said agree, and explained they could not verify")
                    + "**"
                    + t(" (reason under Grounds below)")
                )
            return ""

        if result.unmeasured_cells:
            lines += [f"- {cell}{_why(cell)}" for cell in result.unmeasured_cells] + [""]
        else:
            lines += [t("(the engine did not record which cells)"), ""]
        if unverifiable:
            lines += [
                "> [!NOTE]",
                "> "
                + t(
                    "{n} of them **said honestly that they could not verify**, with a reason.",
                    n=len(unverifiable),
                ),
                "> "
                + t(
                    "The cells still count as unmeasured — a failed verification "
                    "cannot ground an agreement, and that does not bend."
                ),
                "> "
                + t(
                    "But this is not the same as saying nothing: **what needs solving "
                    "is the obstacle they named** (a missing dependency, no citation "
                    "given…), not chasing them for a verification."
                ),
                "",
            ]
        if unverified:
            # Listing a bare unknown has the reader assume the other party took no position. In fact
            # they did, and it merely has no foundation — and the two call for completely different
            # remedies.
            lines += [
                "> [!IMPORTANT]",
                "> "
                + t(
                    "{n} of the above are **not silence — they took a position with "
                    "nothing under it**:",
                    n=len(unverified),
                ),
                "> "
                + t(
                    "these participants wrote `agree` about a peer without submitting "
                    "a single verification they reproduced themselves, while that peer "
                    "did put forward evidence the engine had executed."
                ),
                ">",
                "> "
                + t(
                    "This engine does not treat «I did not check» as a weaker kind of "
                    "agreement, so those cells count as unmeasured. To turn them into "
                    "real agreement, the participant has to actually run the other "
                    "side's work — and if they cannot, `how: unable` with a reason is "
                    "just as honest an answer."
                ),
                "",
            ]

    if result.verification_grounds:
        lines += ["## " + t("What these agreements stand on"), ""]
        lines += [
            t(
                "The engine decides an agreement «has grounds» on the strength of the "
                "records below. **They are all self-reported** — a participant says "
                "they ran it, they checked it, and nothing can prove that on their "
                "behalf. How good this consensus is comes down to whether these hold "
                "up when you read them yourself."
            ),
            "",
        ]
        for cell, items in sorted(result.verification_grounds.items()):
            lines.append(f"**{cell}**")
            lines += [f"- {_cell(item)}" for item in items]
            lines.append("")

    if result.suspicious_testers:
        lines += [
            "> [!NOTE]",
            "> "
            + t(
                "**Only {who}'s own implementation passes {who}'s tests.**",
                who="、".join(result.suspicious_testers),
            ),
            ">",
            "> "
            + t(
                "That usually means the tests encode assumptions that hold only for "
                "their author — green does not say the implementation is right, only "
                "that the tests and the implementation share a misunderstanding."
            ),
            "",
        ]

    # ── reservations (residuals enter the deliverable verbatim) ───────────────────────
    if result.residuals:
        lines += ["## " + t("Reservations"), ""]
        for pair, items in sorted(result.residuals.items()):
            lines.append(f"**{pair}**")
            lines += [f"- {item}" for item in items]
            lines.append("")

    # ── minority opinions ─────────────────────────────────────────────────────────────
    lines += ["## " + t("Minority views"), ""]
    if result.minority:
        for pid, text in result.minority.items():
            lines += [f"**{pid}**: {text.strip()}", ""]
    else:
        lines += [t("(none)"), ""]

    return "\n".join(lines).rstrip() + "\n"


def render_report(
    state: DeliberationState, result: Result, budget_caveat: str | None = None
) -> str:
    """Render REPORT.md — the minutes: how the argument went, how the matrix
    moved, what it cost."""
    with scoped(pick_language(result.task)):
        return _render_report(state, result, budget_caveat)


def _render_report(
    state: DeliberationState, result: Result, budget_caveat: str | None = None
) -> str:
    lines = [
        "# " + t("Minutes: {title}", title=short_title(result.task)),
        "",
        f"run `{result.run_id}`",
        "",
    ]
    lines += [
        "| " + t("item") + " | " + t("value") + " |",
        "|---|---|",
        f"| {t('outcome')} | `{result.outcome.value}` |",
        f"| {t('rounds')} | {result.rounds_used} |",
        f"| {t('participants')} | {', '.join(state.ids)} |",
        f"| {t('rapporteur')} | {result.drafted_by or t('(degraded: mechanical summary)')} |",
    ]
    usage = result.usage
    if usage.known:
        lines.append(
            f"| {t('usage')} | in {usage.input_tokens or 0} / "
            f"out {usage.output_tokens or 0} tokens |"
        )
    lines.append("")
    if budget_caveat:
        lines += [f"> {budget_caveat}", ""]

    if not getattr(state, "stances_requested", True):
        # This protocol requests no stance card (reflect, where nobody sees anybody). The matrix is
        # necessarily all "unknown", and accounting for it as "N open disagreements" and listing the
        # participants as "stance could not be parsed" is labelling missing data as disagreement —
        # the same error already plugged today at three separate outlets: RESULT.md, the terminal
        # progress output, and the consensus blockers.
        lines += [
            "## " + t("Disagreement matrix"),
            "",
            t(
                "In this protocol the participants never see each other and produce no "
                "peer assessments, so there is no matrix to speak of. Their "
                "independent conclusions are below and in `turns/`."
            ),
            "",
        ]
        return _finish_report(lines, state)

    lines += ["## " + t("How the matrix moved"), ""]
    for record in state.rounds:
        report: ConsensusReport | None = record.consensus
        lines += ["### " + t("Round {n}", n=record.index), ""]
        if report is None:
            lines += [t("(no consensus snapshot for this round)"), ""]
            continue
        lines += ["```", render_matrix(report), "```", ""]
        summary = report.describe_unresolved()
        if report.partials:
            summary += " · " + t("{n} partial", n=report.partials)
        summary += " · " + t("lowest confidence {v}", v=f"{report.min_confidence:.2f}")
        if report.stalled_rounds:
            summary += " · " + t("stalled for {n} rounds", n=report.stalled_rounds)
        lines += [summary, ""]
        if report.blockers:
            lines += [t("Why it did not converge:")] + [f"- {b}" for b in report.blockers] + [""]

    return _finish_report(lines, state)


def run_anomalies(state) -> list[str]:
    """**What went wrong** in this deliberation, itemised.

    .. warning::
       **An empty list does not mean the deliberation was fine.** This list is a closed set
       distilled from holes already fallen into, and it cannot recognise a new shape of
       problem — while the two defects actually caught today (``fences_seen`` lying, someone
       truncated in both rounds) were both of a kind nobody had thought of in advance.

       Reading "no anomaly reported" as "everything is fine" is the very error this project
       keeps guarding against: **an empty value masquerading as data**. The caller must present
       it as "no anomaly of a known kind was found".

    The event stream holds everything; the problem is that nobody reads it — one deliberation
    has tens of thousands of ``turn.delta`` events and an anomaly drowns among them. Measured:
    one participant was truncated in both rounds and landed zero files, and the only mention of
    it came from **another participant** in passing — the engine knew, and did not say.

    So this does not print more logs (which would only drown it further); it picks the
    anomalies out.
    """
    notes: list[str] = []
    for record in state.rounds:
        for turn in record.turns:
            who = t("round {n} {who}", n=record.index, who=turn.participant)
            if turn.error:
                notes.append(
                    t(
                        "{who}: the turn failed — {reason}",
                        who=who,
                        reason=turn.error.splitlines()[0][:90],
                    )
                )
            elif turn.truncated:
                notes.append(
                    t(
                        "{who}: cut off by the output budget ({n} chars); stance card not accepted",
                        who=who,
                        n=len(turn.text),
                    )
                )
        for item in record.evidence:
            if item.exit_code != 0 and item.is_self_test:
                notes.append(
                    t(
                        "round {n} {who}: self-test failed (exit code {code})",
                        n=record.index,
                        who=item.participant,
                        code=item.exit_code,
                    )
                )
        for found in record.adoptions:
            notes.append(
                t(
                    "round {n} {who}: copied {foe}'s {path} wholesale",
                    n=found.round,
                    who=found.participant,
                    foe=found.adopted_from,
                    path=found.path,
                )
            )

    silent = [
        pid
        for pid in state.ids
        if state.rounds
        and not any(t.participant == pid and t.ok for r in state.rounds for t in r.turns)
    ]
    for pid in silent:
        notes.append(
            t(
                "**{who} never spoke successfully at all** — this deliberation "
                "effectively happened without them",
                who=pid,
            )
        )
    return notes


def _finish_report(lines: list[str], state) -> str:
    if notes := run_anomalies(state):
        lines += [
            "## " + t("What went wrong in this run"),
            "",
            t(
                "The event stream holds everything, but a run produces tens of "
                "thousands of events and anomalies drown in them. Pulled out here:"
            ),
            "",
        ]
        lines += [f"- {note}" for note in notes] + [""]
    else:
        lines += [
            "## " + t("What went wrong in this run"),
            "",
            t(
                "**No anomaly of a known kind was found** — what was checked: failed "
                "turns, truncation by the output budget, code written but never "
                "landed, wholesale copying of a peer, and never speaking successfully."
            ),
            "",
            "> "
            + t(
                "This is not the same as «everything is fine». The list was distilled "
                "from **potholes already hit**; it cannot recognise a single new kind "
                "of problem. To be sure this run was sound you still have to read "
                "`turns/` and `events.jsonl`."
            ),
            "",
        ]

    lines += ["## " + t("Statements by round"), "", t("Originals are in `turns/`."), ""]
    for record in state.rounds:
        for turn in record.turns:
            # `turn.error` may be None (an empty reply, or truncated with no prose), and
            # interpolating it directly renders a baffling "❌ None". A multi-line error has to be
            # flattened too, or it bursts the minutes' structure.
            if turn.ok:
                status = ""
            elif turn.error:
                status = f" ❌ {' '.join(str(turn.error).split())}"
            else:
                status = " ❌ " + t("produced nothing and left no reason")
            lines.append(
                "- "
                + t(
                    "round {round} / phase {phase} / {who} ({kind}, {chars} chars, {secs}s)",
                    round=turn.round,
                    phase=turn.phase,
                    who=turn.participant,
                    kind=turn.kind,
                    chars=len(turn.text),
                    secs=f"{turn.duration_s:.1f}",
                )
                + status
            )
    lines.append("")
    return "\n".join(lines)
