"""Have a model read the transcript directly and answer "what did the debate change".

**Why this was avoided for so long**: the project's design principle is "no referee" —
and that refers to **a referee inside the deliberation**, a role that decides who is
right and thereby shapes the conclusion. A judge used for evaluation reads a transcript
of something **already over** and influences no deliberation at all. Applying the
principle for the former to the latter was a reasoning error: all six counting and
embedding proxy metrics built to avoid it failed (see DESIGN.md §14.5, §14.8).

**The judge's own failure modes**, each dealt with:

* **Hallucinated quotations** — every verdict must carry a **verbatim quotation**, and
  the quotation is **mechanically checked** against the transcript. A verdict that fails
  the check is void.
* **Self-preference** — the judge may not be a participant in that deliberation
  (:func:`assert_not_participant`).
* **Instability** — the same transcript can be judged several times, and
  :func:`agreement` reports the rate. **But repeating one judge measures certainty, not
  correctness**: the literature measures error correlation in a homogeneous jury at
  ρ≈0.94–0.97, so "jury size matters far less than error dependence" and adding more of
  the same judge buys almost nothing. Estimating reliability requires **cross-judging
  with a different model** (:func:`cross_agreement`).
* **Over-attribution** — the prompt offers "elaborated only" explicitly and says it is
  the default.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from .consensus.stance import find_json_blocks
from .i18n import scoped, t
from .prompts import Template, pick_language
from .record import Recorder, read_events

VERDICTS = ("实质改变", "仅扩充论证", "无变化")

#: Which language the judge answers in follows the transcript's language — while **the archived
#: verdicts must be of one vocabulary**. Half the verdicts in English and half in Chinese would turn
#: the agreement rate into a comparison of languages rather than of judgements. So the English
#: verdicts are normalised on read to the three canonical values above, the same way the bilingual
#: markers in :mod:`sesa.consensus.stance` are handled.
VERDICT_ALIASES = {
    "changed": "实质改变",
    "substantive change": "实质改变",
    "elaborated": "仅扩充论证",
    "elaboration only": "仅扩充论证",
    "unchanged": "无变化",
    "no change": "无变化",
}


def normalise_verdict(raw: str) -> str:
    """Normalise the judge's verdict to a canonical value; an unrecognised one comes back
    unchanged (and is then ruled untrustworthy).
    """
    text = (raw or "").strip()
    return VERDICT_ALIASES.get(text.lower(), text)


#: This tool **answers only "did it change", never "did it change for the better"**.
#: The literature (2026) measures: of the position flips caused by strict conformity, **57–77% went
#: from right to wrong**; and even empty-sounding reasoning induces 20–39% wrong adoptions in
#: "resistant agents" — sounding like an argument is itself persuasive. **Without ground truth, a
#: helpful influence and a harmful one cannot be told apart.**
#: So "positions changed a lot" may equally mean the debate is working or that the participants are
#: leading each other astray. Any reading that treats the rate of change as a quality signal has
#: nothing to stand on.
def change_is_not_quality() -> str:
    """This sentence goes into the eval report and follows the report's language, which is why
    it is a function and not a constant.
    """
    return t(
        'This verdict answers only "did it change", never "did it change '
        'for the better" — without ground truth, a helpful influence and a harmful '
        "one cannot be told apart (in measurements, 57\u201377% of strict-conformity "
        "flips went from right to wrong)."
    )


PROMPT = Template("""You are **evaluating a deliberation that has already ended**, not taking part in it. Do not judge who was right.

Answer one factual question only: **did each participant's position substantively change between the first round and the last?**

# The three verdicts

- `changed`: the conclusion, the option chosen, or a key claim **is different**. This includes conceding a point, withdrawing an earlier argument, and changing which option they favour.
- `elaborated`: the conclusion is unchanged; they added reasons, tightened conditions, or answered objections. **This is the most common case and the default** — do not call it a change just because a lot of the wording moved.
- `unchanged`: near-verbatim repetition.

# Transcript

{transcript}

# Output

Output one json code block only:

```json
{{
  "participants": {{
    "<participant id>": {{
      "verdict": "changed | elaborated | unchanged",
      "first_position": "a sentence **quoted verbatim** from their first round that represents their initial position",
      "final_position": "a sentence **quoted verbatim** from their last round that represents their final position",
      "reason": "why you judged it this way"
    }}
  }},
  "overall": "did the conclusion of the whole deliberation change substantively from round one? One sentence"
}}
```

**Quotations must appear verbatim in the transcript** — the program checks them, and an invented quotation voids that verdict.
If a participant's position cannot be determined from the transcript, put `unchanged` and give the reason in `reason`.
""")


def build_prompt(transcript: str) -> str:
    """The judge prompt. **The language follows the transcript** — a Chinese transcript needs a
    judge reading it in Chinese.

    The verdicts (changed / elaborated / unchanged) are normalised on read; see
    :data:`VERDICT_ALIASES`: the judge may answer in either language, and the archive holds
    one set of values.
    """
    with scoped(pick_language(transcript)):
        return PROMPT.format(transcript=transcript)


@dataclass
class ParticipantVerdict:
    participant: str
    verdict: str
    first_position: str
    final_position: str
    reason: str
    #: whether the quotation really appears in the transcript — checked mechanically, not taken on
    #: the judge's word
    first_verified: bool = False
    final_verified: bool = False

    @property
    def trustworthy(self) -> bool:
        return self.first_verified and self.final_verified and self.verdict in VERDICTS


@dataclass
class JudgeReport:
    run_id: str
    judge: str
    overall: str = ""
    verdicts: list[ParticipantVerdict] = field(default_factory=list)
    raw: str = ""

    @property
    def usable(self) -> list[ParticipantVerdict]:
        """Only a verdict whose quotations check out counts."""
        return [v for v in self.verdicts if v.trustworthy]

    @property
    def rejected(self) -> list[ParticipantVerdict]:
        return [v for v in self.verdicts if not v.trustworthy]

    @property
    def verification_rate(self) -> float:
        """The share of verdicts whose quotations check out — **this is the reading of the judge's
        own credibility**.

        Measured, it varies enormously: over the same batch of deliberations, one judge had 0
        verdicts voided and another had 4 out of 7. Without this check, the two outputs look
        equally credible.
        """
        return len(self.usable) / len(self.verdicts) if self.verdicts else 0.0

    def drop_unknown_participants(self, participants: list[str]) -> list[ParticipantVerdict]:
        """Drop participants the judge invented out of thin air.

        Measured, a judge took the **file names** in the transcript (``r00_p0_x_draft``) for
        participant ids.
        """
        known = set(participants)
        stray = [v for v in self.verdicts if v.participant not in known]
        self.verdicts = [v for v in self.verdicts if v.participant in known]
        return stray


def build_transcript(run_dir: Path, max_chars_per_turn: int = 4000) -> tuple[str, list[str]]:
    """Assemble a deliberation's turns into a transcript; returns (transcript, participant list).

    It uses the prose under ``turns/`` rather than the event stream — the prose is the version
    people read and the version the judge quotes from, and the two have to be the same source.
    """
    run_dir = Path(run_dir)
    events = read_events(run_dir)
    start = next((e for e in events if e["t"] == "run.start"), {})
    participants = list(start.get("participants") or [])

    task = str(start.get("task", ""))[:2000]
    with scoped(pick_language(task)):
        blocks = [t("# Topic") + f"\n\n{task}"]
    for path in sorted(run_dir.glob("turns/*.md")):
        stem = path.stem
        body = path.read_text(encoding="utf-8")
        # The "raw model output" fold in the archive is there for checking the parsing and does not
        # belong in the transcript Split on the **language-independent archive marker**, not on that
        # Chinese subheading. The subheading follows the deliberation language, so under an English
        # deliberation the judge would swallow the whole raw output and the reasoning along with it.
        body = body.split(Recorder.ARCHIVE_MARK)[0]
        blocks.append(f"\n## {stem}\n\n{body.strip()[:max_chars_per_turn]}")
    return "\n".join(blocks), participants


def normalise(text: str) -> str:
    """When comparing quotations, ignore whitespace and common punctuation differences;
    everything else is compared verbatim.
    """
    return re.sub(r"[\s，。、；：？！,.;:?!\"'「」『』（）()]+", "", text)


def verify_quote(
    quote: str,
    transcript: str,
    min_length: int = 8,
    *,
    speaker: str | None = None,
    round_index: int | None = None,
) -> bool:
    """Whether a quotation really appears in the transcript. A quotation too short is not
    accepted — it hits by chance too easily.

    Given ``speaker``, search only **that person's own turns**; given ``round_index`` as
    well, narrow further to that round.

    Narrowing by round is necessary: if ``first_position`` and ``final_position`` were both
    checked against every round, **round 0's wording could satisfy "final position"**, and
    the verdict "they went from A to B" would pass the check even when they changed not one
    word — while measuring change is the entire reason those two fields exist.

    .. warning::
       Without narrowing by speaker this check is worthless: the judge says "alice's position
       is X", and as long as X appears anywhere in the transcript — **even said by bob, even
       a sentence from the task statement** — it passes. And the entire reason this check
       exists is to stop the judge putting words in someone's mouth.
    """
    cleaned = normalise(quote)
    if len(cleaned) < min_length:
        return False
    scoped = _only_from(transcript, speaker, round_index)
    if scoped is None:
        # This transcript has no recognisable speaker blocks (an old record, or text assembled
        # externally). **When speakers cannot be told apart, everything must not be ruled false** —
        # that would kill every honest quotation too. Fall back to matching the whole text, but the
        # check can then only prove "someone said this", not "they said it".
        return cleaned in normalise(transcript)
    return cleaned in normalise(scoped)


def _only_from(transcript: str, speaker: str | None, round_index: int | None = None) -> str | None:
    """The part of the transcript belonging to one person.

    Returning ``None`` means **this transcript cannot separate speakers** (no block headings)
    — which has to be kept apart from "found, but empty": the first should fall back to the
    whole text, the second should be ruled false.

    The transcript's block heading has the form ``## r00_p0_alice_draft``, with the speaker
    inside it.
    """
    if not speaker:
        return transcript
    # **Only a real block heading counts.** A participant writing a subheading like `## Reasons`
    # inside their own turn is extremely common, and "a ## means the speaker changed" would cut off
    # the whole second half of their turn — so a genuine quotation would be ruled invented. This
    # check exists to stop the judge inventing quotations, and that change would have it kill honest
    # ones instead.
    # The block heading has a fixed shape: `## r{round}_p{phase}_{id}_{kind}`. An id may itself
    # contain underscores, so neither substring containment (`alice` would swallow `alice_bot`) nor
    # a naive split on `_` (splitting `alice_bot` yields `alice`) will do — drop the two fixed
    # leading segments and the trailing one, and what remains as a whole is the id.
    owners = [(line, _owner_of(line)) for line in transcript.splitlines()]
    if not any(owner for _, owner in owners):
        return None
    kept, taking = [], False
    for line, owner in owners:
        if owner is not None:
            who, index = owner
            taking = who == speaker and (round_index is None or index == round_index)
        elif taking:
            kept.append(line)
    return "\n".join(kept)


_BLOCK_HEADING = re.compile(r"^##\s+r(?P<round>\d+)_p\d+_(?P<who>.+)_[a-z]+\s*$")


def _owner_of(line: str) -> tuple[str, int] | None:
    """Whether this line is a block heading; if so return (speaker, round), otherwise ``None``."""
    match = _BLOCK_HEADING.match(line)
    return (match.group("who"), int(match.group("round"))) if match else None


def rounds_of(transcript: str, speaker: str) -> list[int]:
    """The rounds in which someone spoke in the transcript, ascending."""
    seen = {
        found[1]
        for line in transcript.splitlines()
        if (found := _owner_of(line)) and found[0] == speaker
    }
    return sorted(seen)


def _first_round(transcript: str, speaker: str) -> int | None:
    """The earliest round someone spoke in; ``None`` when rounds cannot be told apart (falling
    back to unrestricted).
    """
    found = rounds_of(transcript, speaker)
    return found[0] if found else None


def _last_round(transcript: str, speaker: str) -> int | None:
    found = rounds_of(transcript, speaker)
    return found[-1] if found else None


def parse(raw: str, transcript: str, run_id: str, judge: str) -> JudgeReport:
    report = JudgeReport(run_id=run_id, judge=judge, raw=raw)
    for obj in reversed(find_json_blocks(raw)):
        if "participants" not in obj:
            continue
        report.overall = str(obj.get("overall") or "").strip()
        for pid, item in (obj.get("participants") or {}).items():
            if not isinstance(item, dict):
                continue
            first = str(item.get("first_position") or "")
            final = str(item.get("final_position") or "")
            report.verdicts.append(
                ParticipantVerdict(
                    participant=str(pid),
                    verdict=normalise_verdict(str(item.get("verdict") or "")),
                    first_position=first,
                    final_position=final,
                    reason=str(item.get("reason") or "").strip(),
                    # The first and last positions must each be **locked to their own round**.
                    # Unrestricted, round 0's wording can satisfy "final position", so "they went
                    # from A to B" passes the check even when they changed not one word.
                    first_verified=verify_quote(
                        first, transcript, speaker=pid, round_index=_first_round(transcript, pid)
                    ),
                    final_verified=verify_quote(
                        final, transcript, speaker=pid, round_index=_last_round(transcript, pid)
                    ),
                )
            )
        break
    return report


def _fingerprint(spec) -> str:
    """A fingerprint of **which model is actually behind** a participant, independent of the id.

    A shared id is only the shallowest overlap: ``claude-conservative`` and ``claude`` have
    different ids and the same model behind them — comparing ids alone misses that
    self-preference.
    """
    options = getattr(spec, "options", {}) or {}
    command = options.get("command")
    if command:
        return "cli:" + " ".join(str(c) for c in command)
    return f"{spec.adapter}:{options.get('base_url', '')}:{spec.model or ''}"


def assert_not_participant(judge_id: str, participants: list[str], specs=None) -> None:
    """The judge may not be a participant in that deliberation — self-preference is the judge's
    commonest failure mode.

    Given ``specs``, it also compares the **underlying model fingerprint**: different ids with
    the same model (``claude-conservative`` and ``claude``, say) are stopped as well.
    """
    if judge_id in participants:
        raise ValueError(
            t(
                "{judge} took part in this deliberation and cannot also judge it. "
                "Self-preference makes it systematically overrate its own change of position.",
                judge=judge_id,
            )
        )
    if not specs:
        return
    table = {spec.id: spec for spec in specs}
    judge_spec = table.get(judge_id)
    if judge_spec is None:
        return
    mark = _fingerprint(judge_spec)
    clashes = [pid for pid in participants if pid in table and _fingerprint(table[pid]) == mark]
    if clashes:
        raise ValueError(
            t(
                "{judge} is the same underlying model as participant(s) {ids} and cannot "
                "judge. A different id does not make a different model — self-preference "
                "follows the model, not the name.",
                judge=judge_id,
                ids=", ".join(clashes),
            )
        )


def cross_agreement(reports: list[JudgeReport]) -> dict[str, float]:
    """The agreement rate **between different judges** — this is what counts as evidence of
    reliability.

    Repeating one judge (:func:`agreement`) only shows its output is stable; the errors of a
    homogeneous jury are highly correlated (measured at ρ≈0.94–0.97), and being wrong
    together also means "agreeing" together. Only judging the same way after changing model
    rules out one model's systematic bias.
    """
    by_judge: dict[str, list[JudgeReport]] = {}
    for report in reports:
        by_judge.setdefault(report.judge, []).append(report)
    if len(by_judge) < 2:
        return {}
    # **One report per judge, but say which one was taken.** `setdefault` used to keep the first
    # silently and drop the rest — so with one judge run three times, the user believed
    # cross-agreement counted all three when only the first was used. This takes the last (the most
    # recent) and leaves a trace when there are several.
    picked = []
    for judge, group in sorted(by_judge.items()):
        if len(group) > 1:
            warnings.warn(
                t(
                    "judge {judge} has {n} reports; cross-agreement uses only the last "
                    "one. For one judge's repeat consistency, use agreement().",
                    judge=judge,
                    n=len(group),
                ),
                RuntimeWarning,
                stacklevel=2,
            )
        picked.append(group[-1])
    return agreement(picked)


def agreement_gaps(reports: list[JudgeReport]) -> dict[str, str]:
    """Which participants have no agreement rate, and **why**.

    :func:`agreement` can only return those it has numbers for, so "never judged" and "judged,
    but the verdicts were rejected by the quotation check" look identical in the output — and
    the latter is a signal that **the judge itself is unreliable**, while the former is merely
    missing data.
    """
    usable: dict[str, int] = {}
    rejected: dict[str, int] = {}
    for report in reports:
        for verdict in report.usable:
            usable[verdict.participant] = usable.get(verdict.participant, 0) + 1
        for verdict in report.rejected:
            rejected[verdict.participant] = rejected.get(verdict.participant, 0) + 1

    gaps: dict[str, str] = {}
    for pid in sorted(set(usable) | set(rejected)):
        if usable.get(pid, 0) >= 2:
            continue
        if dropped := rejected.get(pid, 0):
            gaps[pid] = t(
                "only {n} verdicts passed quotation checking, {dropped} more were "
                "rejected — the agreement rate is missing not because nobody judged, "
                "but because the judge's quotations do not match the transcript",
                n=usable.get(pid, 0),
                dropped=dropped,
            )
        else:
            gaps[pid] = t("only {n} verdicts — too few to speak of agreement", n=usable.get(pid, 0))
    return gaps


def agreement(reports: list[JudgeReport]) -> dict[str, float]:
    """The agreement rate across several judgements.

    .. warning::
       With one judge repeated, this number **measures certainty, not correctness**.
       For evidence of reliability use :func:`cross_agreement` (cross-judging with a different
       model).
    """
    if len(reports) < 2:
        return {}
    per_participant: dict[str, list[str]] = {}
    for report in reports:
        for verdict in report.usable:
            per_participant.setdefault(verdict.participant, []).append(verdict.verdict)
    out = {}
    for pid, values in per_participant.items():
        if len(values) < 2:
            # **A rejected verdict is not "nothing happened".** `report.usable` has already filtered
            # out verdicts whose quotations failed to check, so "two judges, one verdict rejected"
            # would be skipped silently here — the reader sees "this person has no consistency data"
            # while the reality is "one of the two judges' verdicts did not pass the check at all".
            # The two mean entirely different things for the conclusion, and the latter is a signal
            # that **the judge itself is unreliable**.
            continue
        top = max(set(values), key=values.count)
        out[pid] = round(values.count(top) / len(values), 3)
    return out


def to_dict(report: JudgeReport) -> dict:
    return {
        "run_id": report.run_id,
        "judge": report.judge,
        "overall": report.overall,
        "verdicts": [
            {
                "participant": v.participant,
                "verdict": v.verdict,
                "reason": v.reason,
                "quotes_verified": v.trustworthy,
            }
            for v in report.verdicts
        ],
        "rejected": len(report.rejected),
        "verification_rate": round(report.verification_rate, 3),
    }


__all__ = [
    "PROMPT",
    "VERDICTS",
    "JudgeReport",
    "ParticipantVerdict",
    "agreement",
    "assert_not_participant",
    "build_transcript",
    "change_is_not_quality",
    "cross_agreement",
    "parse",
    "to_dict",
    "verify_quote",
]
