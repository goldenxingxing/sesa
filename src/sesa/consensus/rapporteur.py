"""The rapporteur.

**They write whether or not it converged** — even after a blazing row, whatever was
settled must be written up as a conclusion, leaving only what genuinely was not settled
in "open disagreements". Handing two contradictory essays back to a person unchanged is
handing the work of making sense of it back to them.

The rapporteur is **not a judge**: the role rotates among the participants, and its job
is to integrate the wording and attribute the disagreements, not to rule on who is right.

The key division of labour: **the rapporteur supplies only content, and the skeleton of
`RESULT.md` is rendered deterministically by** :mod:`sesa.report`. That makes "the
skeleton is constant" a hard guarantee rather than a hope that the model behaves.
"""

from __future__ import annotations

import json

from ..consensus.stance import find_json_blocks
from ..i18n import scoped, t
from ..prompts import Template, pick_language
from ..state import DeliberationState
from ..types import ConsensusReport, Disagreement, Outcome


def _schema() -> dict:
    """The JSON skeleton the rapporteur produces. **Built when called** — the descriptions have
    to follow the deliberation language, and as a module constant they would be frozen into
    one language at import time. The field names are the parsing contract and are not
    translated.
    """
    return {
        "conclusion": t(
            "the conclusion the parties actually agree on, markdown, ready to use. "
            "If there is no consensus at all, write: no consensus"
        ),
        "grounds": [
            t(
                "the key arguments holding the conclusion up, one per item, naming who raised or accepted each"
            )
        ],
        "disagreements": [
            {
                "topic": t("what this disagreement is about"),
                "positions": {
                    t("<participant id>"): t("their position on this point, one sentence")
                },
                "reasons": {t("<participant id>"): t("the reason they gave")},
                "root_cause": t(
                    "the root of the disagreement — most technical arguments come from "
                    "differing premises rather than one side being wrong. Dig the premises out"
                ),
                "decisive_question": t(
                    "the single question whose answer dissolves this disagreement. "
                    "Specific and answerable"
                ),
            }
        ],
        "minority": {
            t("<participant id>"): t("what they held to and was not adopted, kept as written")
        },
        "conflicts_found": [
            t(
                "if every stance card says agree but the prose actually conflicts, list the "
                "conflicting points here; otherwise leave an empty array"
            )
        ],
    }


PROMPT = Template("""You are the **rapporteur** of this deliberation.

You are not a judge — your job is to **integrate the wording and attribute the disagreements**, not to rule on who is right. Do not introduce any view that did not appear in the discussion.

# The original task

{task}

# Everyone's final turn

{statements}

# Where the disagreements stand (computed by the program from the stance cards)

{consensus}

# What to do

Produce a structured result of the deliberation. **Output one json code block only**, with no other text.

```json
{schema}
```

Requirements:

1. **`conclusion` holds only what the parties genuinely agree on.** Contested material must not be written into the conclusion as though everyone agreed.
2. **Every entry in `disagreements` must fill in `root_cause` and `decisive_question`.**
   Simply listing each side's position is not enough — that hands the work of making sense of it back to the reader.
   The large majority of disagreements come from **differing premises** (scale, budget, timeline, team level). Dig the assumptions out and the reader can usually decide at a glance.
   `decisive_question` must be specific enough that answering it dissolves the disagreement. Do not write "more information is needed".
3. **`minority` keeps minority views as written** — do not soften or drop one because it is in the minority.
4. **`conflicts_found`:** if you find that every stance card says agree but the prose actually conflicts, list the conflicting points here — this sends the deliberation back for another round. Empty array if there are none.
{outcome_note}""")


def _outcome_note(outcome: Outcome) -> str:
    notes = {
        Outcome.CONSENSUS: t(
            "\nThis deliberation **reached consensus** (no explicit opposition in the "
            "disagreement matrix). Integrate it as it stands, but still check whether the "
            "prose holds a substantive conflict the stance cards did not reflect."
        ),
        Outcome.DEADLOCK: t(
            "\nThis deliberation is **deadlocked**: nobody changed position for several "
            "rounds and the disagreements did not shrink. **Do not manufacture a consensus "
            "to make the document look better.** What was settled goes into `conclusion` as "
            "usual; what was not goes into `disagreements` in full."
        ),
        Outcome.EXHAUSTED: t(
            "\nThis deliberation was stopped because **the rounds or the budget ran out**; "
            "the discussion did not finish. Reflect the state honestly: what was settled "
            "goes into `conclusion`, what is still open goes into `disagreements`."
        ),
    }
    return notes.get(outcome, "")


def build_prompt(state: DeliberationState, report: ConsensusReport, outcome: Outcome) -> str:
    # The language is decided here: the rapporteur prompt is sent by `_call_plain` and does not pass
    # through the engine's two `scoped` blocks. A Chinese deliberation receiving an English
    # rapporteur prompt produces a RESULT.md with mixed languages.
    with scoped(pick_language(state.task)):
        return _build_prompt(state, report, outcome)


def _build_prompt(state: DeliberationState, report: ConsensusReport, outcome: Outcome) -> str:
    record = state.current
    statements = "\n\n".join(
        f"## {pid}\n\n{text.strip()}"
        for pid, text in (record.statements() if record else {}).items()
    ) or t("(none)")

    lines = []
    for source, target in report.disagreeing_pairs():
        stance = record.stances.get(source) if record else None
        reason = stance.stance_on[target].reason if stance and target in stance.stance_on else ""
        lines.append(
            t(
                "- {source} opposes {target}: {reason}", source=source, target=target, reason=reason
            ).rstrip(": ：")
        )
    # Blame only those who **were asked**. Protocols like reflect never request a stance card at
    # all, and listing their participants as "stance could not be parsed" accuses them of not
    # answering a question nobody put to them.
    if report.unknown_participants and getattr(state, "stances_requested", True):
        lines.append(
            t(
                "- Stance could not be parsed: {ids} (do not take a position on their behalf)",
                ids=", ".join(report.unknown_participants),
            )
        )
    if lines:
        consensus_block = "\n".join(lines)
    elif getattr(state, "stances_requested", True):
        consensus_block = t("(the stance cards show no explicit opposition)")
    else:
        # This protocol never requested a stance card, so "the stance cards show no explicit
        # opposition" is testimony about something never measured.
        consensus_block = t(
            "(in this protocol the parties cannot see each other and no stance cards are "
            "requested, so there is no peer-assessment data)"
        )

    return PROMPT.format(
        task=state.task,
        statements=statements,
        consensus=consensus_block,
        schema=json.dumps(_schema(), ensure_ascii=False, indent=2),
        outcome_note=_outcome_note(outcome),
    )


def _as_mapping(value) -> dict:
    """Treat as a mapping only what really is one; anything else counts as "not given".

    Models routinely write a field that should be an object as an array or a string, and
    ``or {}`` only catches None and empty values — a non-empty list travels all the way to
    ``.items()`` before it blows up.
    """
    return value if isinstance(value, dict) else {}


def parse_draft(text: str, ids: list[str]) -> dict | None:
    """Parse the rapporteur's output; return ``None`` on failure, and the Engine takes the
    degraded path.
    """
    for obj in reversed(find_json_blocks(text)):
        if "conclusion" not in obj and "disagreements" not in obj:
            continue

        disagreements = []
        for raw in obj.get("disagreements") or []:
            if not isinstance(raw, dict):
                continue
            # `or {}` only catches None/empty. When a model writes positions as a list or a string
            # (non-empty ⇒ no fallback to {}), `.items()` raises AttributeError straight out — while
            # this function's contract is "return None if it cannot be parsed, and let the engine
            # take the degraded path". One malformed draft would then kill the whole deliberation
            # instead of degrading it.
            positions = {
                k: str(v) for k, v in _as_mapping(raw.get("positions")).items() if k in ids
            }
            # **Keep it even with no positions.** I once dropped such entries under "a partial with
            # an empty payload is treated as unknown", and that broke false-consensus detection: a
            # rapporteur who reads the whole thing and says "there is a substantive conflict here"
            # is giving a real signal that the structured stance cards **failed to reflect**, even
            # without filling in a position per person — and that is the entire reason the
            # false-consensus path exists.
            # The hazard on the reconcile side (an empty shell counting as "the rapporteur wrote a
            # disagreement") is not solved by discarding: that side now checks coverage pair by
            # pair, an entry with no positions covers no pair, and the fallback fires as usual.
            disagreements.append(
                Disagreement(
                    topic=str(raw.get("topic") or t("unnamed disagreement")),
                    positions=positions,
                    reasons={
                        k: str(v) for k, v in _as_mapping(raw.get("reasons")).items() if k in ids
                    },
                    root_cause=str(raw.get("root_cause") or ""),
                    decisive_question=str(raw.get("decisive_question") or ""),
                )
            )

        return {
            "conclusion": str(obj.get("conclusion") or "").strip(),
            "grounds": [str(g).strip() for g in (obj.get("grounds") or []) if str(g).strip()],
            "disagreements": disagreements,
            "minority": {
                k: str(v) for k, v in _as_mapping(obj.get("minority")).items() if k in ids
            },
            "conflicts_found": [
                str(c).strip() for c in (obj.get("conflicts_found") or []) if str(c).strip()
            ],
        }
    return None


def reconcile(draft: dict, state: DeliberationState, report: ConsensusReport) -> dict:
    with scoped(pick_language(state.task)):
        return _reconcile(draft, state, report)


def _reconcile(draft: dict, state: DeliberationState, report: ConsensusReport) -> dict:
    """Check the rapporteur's output against the disagreement matrix.

    **The rapporteur's self-reported list of disagreements cannot be taken on trust.** They
    may miss one, cut a corner, or skip a disagreement to make the document look better —
    which is exactly "dressing a deadlock up as consensus". The engine holds a computable
    ground truth, so it must use it as a backstop: when the matrix holds explicit opposition
    and the rapporteur wrote no disagreement at all, the engine fills them in mechanically
    from the stance cards and marks it ``reconciled``.
    """
    # The trigger is `opposed`, **not** `unresolved`. The latter is opposition + not measured, and
    # using it as the backstop accuses the rapporteur of missing disagreements when there is zero
    # opposition and merely a few unmeasured cells — while they missed nothing, there was nothing to
    # write. Calling unmeasured cells disagreement is precisely what this project's second bottom
    # line exists to prevent: labelling missing data as disagreement. Unmeasured cells have their
    # own outlet: coverage and unmeasured_cells are delivered with the outcome.
    if not report.opposed:
        return draft
    # **"They listed one" is not "they missed none".** The earlier condition was `if not
    # report.opposed or draft.get("disagreements")` — one disagreement written and the backstop was
    # skipped entirely, with the other explicit oppositions **vanishing silently from the
    # deliverable**. And "minority opinions must reach the deliverable" is one of this project's
    # bottom lines: missing four fifths is no different in kind from missing all of it.
    listed = draft.get("disagreements") or []

    # **The test is "which pairs got written down", not "how many were written".**
    # It first looked at non-emptiness (one entry and the whole thing was skipped); I changed it to
    # compare counts, which was still not enough: a rapporteur can list one **irrelevant**
    # disagreement to make up the number, the counts match, and the pair that really is opposed in
    # the matrix vanishes from the deliverable all the same. Only a pair-by-pair check stops it.
    # Pairs in the matrix are directed (a opposes b); coverage is judged undirected — a single
    # disagreement that discusses both a and b has recorded that opposition.
    wanted_pairs = {tuple(sorted(pair)) for pair in report.disagreeing_pairs()}
    missing = sorted(pair for pair in wanted_pairs if not _pair_is_covered(pair, listed))
    if not missing:
        return draft

    if not (found := _disagreements_from_matrix(state, report)):
        # Saying "filled in mechanically" and then filling in nothing is worse than not filling in
        # at all — it claims to have done something out of thin air.
        return draft

    draft = dict(draft)
    # **Add, do not overwrite.** What the rapporteur wrote carries reasons and root causes; the
    # mechanical transcription carries only positions, and replacing outright throws the human part
    # away with it. Append only the pairs not yet covered.
    wanted = set(missing)
    extra = [d for d in found if _pairs_of(d) & wanted]
    draft["disagreements"] = list(listed) + (extra or found)
    pairs = t("\u3001").join(f"{a}\u2194{b}" for a, b in missing)
    draft["reconciled"] = (
        t(
            "The rapporteur listed {n} disagreements, but these oppositions in the matrix "
            "were not written down: {pairs}. The engine filled in the following "
            "mechanically from the stance cards.",
            n=len(listed),
            pairs=pairs,
        )
        if listed
        else t(
            "The rapporteur listed no disagreements, but the matrix holds {n} explicit "
            "oppositions; the engine filled in the following mechanically from the "
            "stance cards.",
            n=report.opposed,
        )
    )
    return draft


def _positions_of(disagreement) -> dict:
    """Take one disagreement's positions.

    ``reconcile`` may receive a ``Disagreement`` object (from ``parse_draft``) or a raw dict
    (an external call, an old record). **Neither may be allowed to crash it** — this
    function is the last check on the deliverable, and its crashing means the whole
    deliberation was for nothing.
    """
    if isinstance(disagreement, dict):
        return disagreement.get("positions") or {}
    return getattr(disagreement, "positions", None) or {}


def _pairs_of(disagreement) -> set[tuple[str, str]]:
    """Every pair of participants a disagreement involves. Direction does not matter, so
    normalise by sort order.
    """
    who = sorted(_positions_of(disagreement))
    return {(a, b) for i, a in enumerate(who) for b in who[i + 1 :]}


def _pair_is_covered(pair: tuple[str, str], listed: list) -> bool:
    """For this pair in the matrix, does any disagreement the rapporteur wrote discuss both
    sides?
    """
    source, target = pair
    return any(source in _positions_of(d) and target in _positions_of(d) for d in listed)


def _disagreements_from_matrix(
    state: DeliberationState, report: ConsensusReport
) -> list[Disagreement]:
    """Carry over only what the stance cards already say; never voice a new opinion on anyone's
    behalf.
    """
    record = state.current
    out: list[Disagreement] = []
    for source, target in report.disagreeing_pairs():
        stance = record.stances.get(source) if record else None
        reason = stance.stance_on[target].reason if stance and target in stance.stance_on else ""
        positions = {}
        for pid in (source, target):
            other = record.stances.get(pid) if record else None
            positions[pid] = other.position if other else ""
        out.append(
            Disagreement(
                topic=t("{source} opposes {target}'s claim", source=source, target=target),
                positions=positions,
                reasons={source: reason} if reason else {},
                root_cause=t(
                    "(the rapporteur gave no attribution; the engine filled this in from "
                    "the stance cards — the original wording is under turns/)"
                ),
                decisive_question="",
            )
        )
    return out


def fallback_draft(state: DeliberationState, report: ConsensusReport) -> dict:
    with scoped(pick_language(state.task)):
        return _fallback_draft(state, report)


def _fallback_draft(state: DeliberationState, report: ConsensusReport) -> dict:
    """The degraded output when drafting fails: a mechanical summary only, **voicing no
    opinion on anyone's behalf**.
    """
    record = state.current
    statements = record.statements() if record else {}
    return {
        "conclusion": "",
        "grounds": [],
        "disagreements": _disagreements_from_matrix(state, report),
        # Truncation has to leave a trace. Cutting a thousand characters without a marker leaves the
        # reader with no idea there was more — and a minority opinion is the last thing that should
        # be quietly shortened.
        "minority": {pid: _clip(text.strip()) for pid, text in statements.items()},
        "conflicts_found": [],
        "degraded": True,
    }


#: Character cap for one minority opinion on the degraded path.
MINORITY_LIMIT = 2000


def _clip(text: str, limit: int = MINORITY_LIMIT) -> str:
    """Truncate when too long, **and say how much was cut and where the full text is**."""
    if len(text) <= limit:
        return text
    return text[:limit] + t(
        "\u2026\u2026(truncated here, {n} characters cut; the full text is under turns/)",
        n=len(text) - limit,
    )
