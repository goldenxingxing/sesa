"""Stance-card extraction.

LLMs — agent CLIs especially — often break the output format. The extraction strategy
descends by reliability:

1. the last ```json code block
2. the last **bracket-balanced** JSON object (not a regex; a regex is always wrong on
   nesting)
3. ask that participant to **send the stance card alone again** (initiated by the Engine)
4. still failing, record ``unknown`` and list it explicitly in the report — **no
   guessing, no writing on their behalf**
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..types import Stance, StanceOn, StanceVerdict, Verification

#: Each fence must be on a line of its own, or inline backticks in markdown prose throw the pairing
#: off
_FENCE = re.compile(r"^```(?:json|JSON)?[ \t]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
_VALID: tuple[StanceVerdict, ...] = ("agree", "partial", "disagree")


def _balanced_objects(text: str) -> list[tuple[str, int, int]]:
    """Scan out every bracket-balanced top-level ``{...}`` fragment and its position.

    Positions are returned so that :func:`strip_stance_block` can cut the stance card out of
    the prose precisely — it is not necessarily inside a tidy code fence.
    """
    out: list[tuple[str, int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append((text[start : i + 1], start, i + 1))
    return out


def find_json_blocks(text: str) -> list[dict[str, Any]]:
    """Return every parseable JSON object in the text, **in the order they appear**.

    **Both paths have to run.** An early version returned as soon as any parseable fence was
    found, so once a participant quoted some other JSON (all but inevitable when discussing
    a JSON-based system), the real stance card could never be found again — measured, claude
    quoted our own ``consensus.update`` event and its stance card was ruled unparseable.
    That is a defect in the extractor, not a participant breaking the format.

    **The order must be textual order.** It used to detect in the order "all fences first,
    all bare objects after", while ``parse_stance`` locates the final card by "take the
    first thing that looks like a stance card, scanning backwards" — two contradictory
    assumptions. The consequence: when a turn quoted someone else's stance card (very common
    when discussing disagreements), the quoted one overrode the author's own, so "Zhang's
    position" was recorded as what Li said.
    """
    found: list[tuple[int, str]] = []
    for match in _FENCE.finditer(text):
        found.append((match.start(), match.group(1).strip()))
    for chunk, start, _ in _balanced_objects(text):
        found.append((start, chunk))

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, raw in sorted(found, key=lambda item: item[0]):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            candidates.append(obj)
    return candidates


#: Only these, appearing in the note, make it "agreement with reservations". Better to miss one and
#: call it a clean agree — the other default-deny gates catch that; whereas a false positive turns a
#: word of assent into an open disagreement, which is manufacturing disagreement out of nothing.
#: Markers that read like "agreement with reservations".
#: **This is not interface copy, it is the test for parsing model output** — so it must follow **the
#: language of the deliberation**, which the task decides. Both languages' markers are therefore on
#: the list at once, and it does not switch with the interface language.
#: The English side used to hold only three words, missing common concessives like though / albeit /
#: caveat. Missing one has real consequences: **agreement with reservations gets taken for agreement
#: without**, which is exactly what default-deny exists to prevent.
_RESERVATION_MARKS = (
    # Chinese
    "但",
    "不过",
    "除了",
    "保留",
    "仍不",
    "还没",
    "前提是",
    "只是",
    "唯一的疑虑",
    "有个前提",
    # English
    "however",
    "except",
    "but ",
    "though",
    "albeit",
    "caveat",
    "reservation",
    "provided that",
    "as long as",
    "my only concern",
    "one concern",
    "with the proviso",
)


#: Explicit denials of having reservations. **These must be checked before the markers** — "no
#: reservation" contains "reservation", so a clean agreement would be downgraded to "with
#: reservations", throwing away a valid consensus for nothing. This is the mirror-image error of
#: missing one, and it costs just as much.
_NO_RESERVATION_MARKS = (
    "没有保留",
    "无保留",
    "毫无保留",
    "不存在保留",
    "没有任何保留",
    "no reservation",
    "without reservation",
    "no caveat",
    "no concerns",
    "unreserved",
)


def _reads_like_reservation(note: str) -> bool:
    """Whether this sentence reads like "agreement with reservations".

    **This is not an interface decision, it is parsing model output** — both languages'
    markers coexist and do not switch with the interface language: which language the
    deliberation speaks is decided by the task.
    """
    lowered = note.lower()
    if any(mark in lowered for mark in _NO_RESERVATION_MARKS):
        return False
    return any(mark in lowered for mark in _RESERVATION_MARKS)


def _coerce_bool(value: Any) -> bool:
    """Parse a boolean the model wrote.

    ``bool("false")`` is ``True`` — models write booleans as strings all the time, and this
    field decides directly whether "anyone changed position", which feeds deadlock detection.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "是")
    return bool(value)


def _looks_like_stance(obj: dict[str, Any]) -> bool:
    return "position" in obj or "stance_on" in obj or "key_claims" in obj


def _coerce_confidence(value: Any) -> float | None:
    """Parse the confidence. ``None`` means **not reported**, kept strictly apart from a
    reported 0.0.
    """
    # bool is a subclass of int: ``float(True)`` is 1.0, sails through the 0–1 check, and
    # `"confidence": true` is read as complete certainty. A model writing true is expressing a vague
    # "I feel confident", not the measured value 1.0 — treat it as not reported. NaN likewise: it
    # compares false against every threshold and silently walks past the confidence bar.
    if isinstance(value, bool):
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf != conf:  # NaN
        return None
    # some models fill in 0-100
    if conf > 1.0:
        conf = conf / 100.0
    return max(0.0, min(1.0, conf))


def _coerce_verdict(value: Any) -> StanceVerdict | None:
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if token in _VALID:
        return token  # type: ignore[return-value]
    # Prefix matching is only for **a tail of pure punctuation** (`agree.`, `agree!`, `"agree"`).
    # This used to be an unconditional startswith, so `agree with major reservations` and `agree
    # except X` were swallowed as a clean `agree` — **the tolerance erased the reservations**, which
    # is exactly what default-deny exists to prevent: a cell counts as resolved if and only if there
    # is an **explicit** agreement without reservation. Conditional agreement either lands in
    # partial (with residuals) or is treated as no position taken.
    stripped = token.strip("\"'`。.!！,，;；:：")
    if stripped in _VALID:
        return stripped  # type: ignore[return-value]
    # Common free-form variants. **Match against stripped, not token** — the punctuation was just
    # removed above, and comparing the unstripped string here made every Chinese model writing "同意。"
    # fail to parse and be recorded, under default-deny, as no position taken. **A valid agreement
    # silently thrown away** — and a Chinese model ending a sentence with a full stop is the norm,
    # not an accident.
    if stripped in ("yes", "agreed", "concur", "同意", "赞成", "认同"):
        return "agree"
    if stripped in ("no", "disagreed", "object", "反对", "不同意", "不认同"):
        return "disagree"
    if stripped in ("partially", "partial agreement", "mostly", "部分同意", "部分", "有保留"):
        return "partial"
    return None


def _coerce_verifications(value: Any) -> list[Verification]:
    """Parse a verification record. A missing or malformed field means dropping that record —
    **no defaults are filled in for it**.

    A missing ``result`` must not become ``reproduced`` above all: that manufactures an "I
    checked it" out of nothing, and the whole foundation of an agreement rests on it.
    """
    if not isinstance(value, list):
        return []
    out: list[Verification] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        how = str(item.get("how") or "").strip().lower()
        result = str(item.get("result") or "").strip().lower()
        if how not in ("executed", "cited", "unable"):
            continue
        if result not in ("reproduced", "refuted", "unable"):
            continue
        # Saying "could not check" while reporting a result contradicts itself — treat it as could
        # not check.
        if how == "unable":
            result = "unable"
        of = str(item.get("of") or "").strip()
        if not of:
            continue  # Without knowing which claim was checked, the record means nothing
        out.append(
            Verification(
                of=of,
                how=how,  # type: ignore[arg-type]
                result=result,  # type: ignore[arg-type]
                detail=str(item.get("detail") or "").strip(),
            )
        )
    return out


#: An explicit claim of "I ran it and the result differs from what they said" — counter evidence,
#: not an absence of measurement. "I ran it and the result differs from what they said" — counter
#: evidence, not an absence of measurement.
#: The English side used to hold only four entries, missing the commonest phrasings like differs /
#: contradicts. Measured: ``ran it, output differs from his claim`` was judged **reproduced** — a
#: piece of counter evidence taken as supporting, exactly backwards.
_REFUTED_MARKS = (
    "不符",
    "不一致",
    "对不上",
    "与其所述不同",
    "结果不同",
    "没通过",
    "失败",
    "refuted",
    "mismatch",
    "does not match",
    "did not match",
    "differs",
    "different from",
    "contradicts",
    "contrary to",
    "does not hold",
    "did not reproduce",
    "could not reproduce",
    "disagrees with",
    "inconsistent with",
    "not what",
)

#: An explicit claim of "I could not check".
_UNABLE_MARKS = (
    "查不了",
    "无法核验",
    "无法复现",
    "无法验证",
    "没法验",
    "跑不",
    "验不了",
    "没能验",
    "不能验",
    "未能",
    "环境缺",
    "unable",
    "cannot",
    "could not",
)

#: Explicit claims of "I checked, and it matches". **Only these provide a foundation.**
_CONFIRMED_MARKS = (
    "跑了",
    "跑过",
    "运行",
    "执行了",
    "复现了",
    "已复现",
    "重现了",
    "一致",
    "相符",
    "吻合",
    "对得上",
    "核对无误",
    "确认无误",
    "验证通过",
    "查了",
    "核对了",
    "查证",
    "确认了",
    "reproduced",
    "verified",
    "confirmed",
    "ran ",
    "checked",
    "matches",
)


def _verification_from_note(target: str, note: str) -> list[Verification]:
    """Turn the free-text verification note from the line-by-line table into a ``Verification``.

    The line table is the **degraded path**, for models that have already proved they cannot
    handle JSON. So only a coarse test is possible here. What matters is **which way a coarse
    test errs**:

    The earlier version was "say 'could not check' and it is unable; write anything else and
    it is executed/reproduced". That errs towards danger —

    * ``"I cannot reproduce this locally"`` matches none of the hard-coded words, so **an
      honest admission** is recorded as "reproduced", a foundation appears out of nowhere,
      and the admission itself is erased
    * ``"I had a look"`` claims nothing at all and likewise becomes "reproduced"

    Both have one root: **inferring reproduced from arbitrary text is manufacturing a
    measurement**. This is the hole this project keeps falling into — except that this time
    it fell into it inside the "the looseness is deliberate" defence I wrote for it.
    (Round 13 self-review: kimi produced the two failing inputs above; deepseek quoted that
    docstring and ruled it "the design intent" — **a docstring saying it is deliberate does
    not make it right**.)

    So the default is inverted: **without an explicit claim of having checked and matched,
    no foundation is given**. The cost of being wrong becomes "withheld" rather than
    "conjured" — the first only makes them add a sentence, the second disables the bar
    entirely. The original text always goes into ``detail`` for the reader to judge.
    """
    if not note:
        return []
    lowered = note.lower()

    def _hit(marks) -> bool:
        return any(mark in lowered for mark in marks)

    # **Negation first.** Someone writing "the run failed, it does not match what they said" matches
    # "ran", and checking the affirmative words first would judge it "reproduced" — reading a piece
    # of counter evidence as supporting, precisely. Keyword matching on free text is unreliable in
    # both directions, so the order must make it err on the safe side.
    if _hit(_REFUTED_MARKS):
        # It came out differently from what they said: this is **counter evidence**, not an absence
        # of measurement.
        return [Verification(of=f"{target} 的主张", how="executed", result="refuted", detail=note)]
    if _hit(_UNABLE_MARKS):
        return [Verification(of=f"{target} 的主张", how="unable", result="unable", detail=note)]
    if _hit(_CONFIRMED_MARKS):
        return [
            Verification(of=f"{target} 的主张", how="executed", result="reproduced", detail=note)
        ]
    # Nothing was claimed. Record it as "the check did not happen", with the original text kept.
    # **Not as opposition** — it simply provides no foundation for an agreement.
    return [Verification(of=f"{target} 的主张", how="unable", result="unable", detail=note)]


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def parse_stance(text: str, participant: str, round: int, others: list[str]) -> Stance | None:
    """Extract the stance card from a turn's prose; returning ``None`` leaves the layer above to
    decide whether to retry.
    """
    candidates = find_json_blocks(text)
    # The stance card is required to come last, so scan backwards for the first thing that looks
    # like one
    for obj in reversed(candidates):
        if not _looks_like_stance(obj):
            continue

        stance_on: dict[str, StanceOn] = {}
        raw_on = obj.get("stance_on")
        if isinstance(raw_on, dict):
            for target, value in raw_on.items():
                if target not in others:
                    continue  # ignore hallucinated participants
                residuals: list[str] = []
                verifications: list[Verification] = []
                if isinstance(value, dict):
                    verdict = _coerce_verdict(value.get("verdict"))
                    reason = str(value.get("reason") or "").strip()
                    verifications = _coerce_verifications(value.get("verified"))
                    residuals = _coerce_list(value.get("residuals"))
                elif isinstance(value, str):
                    verdict, reason = _coerce_verdict(value), ""
                else:
                    continue
                if not verdict:
                    continue
                if verdict == "partial" and not residuals:
                    # A "partial agreement" with an empty payload cannot be checked: it says neither
                    # what is agreed nor what is held back. Treat it as unknown, counted as
                    # unresolved by default-deny — do not assume agreement on their behalf.
                    verdict = "unknown"
                elif verdict == "agree" and (residuals or _reads_like_reservation(reason)):
                    # **Agreement with reservations is not agreement without.** default-deny
                    # requires "resolved" if and only if there is an explicit agree **without
                    # reservation**; taking an agree carrying residuals for a clean agree lets
                    # someone write `"verdict": "agree"` plus a sentence of reservation and buy a
                    # **better** outcome than filling in partial honestly. That inversion was
                    # measured: with identical reservations, filling in partial gave
                    # consensus_with_reservations while filling in agree gave consensus.
                    verdict = "partial"
                    if not residuals:
                        # If the reason states a reservation, register it as one — otherwise that
                        # sentence enters neither residuals nor the reservation count, and
                        # "agreement with reservations" passes for agreement without.
                        residuals = [reason]
                stance_on[target] = StanceOn(
                    verdict=verdict, reason=reason, residuals=residuals, verified=verifications
                )

        return Stance(
            participant=participant,
            round=round,
            position=str(obj.get("position") or "").strip(),
            confidence=_coerce_confidence(obj.get("confidence")),
            premises=_coerce_list(obj.get("premises")),
            key_claims=_coerce_list(obj.get("key_claims")),
            stance_on=stance_on,
            open_questions=_coerce_list(obj.get("open_questions")),
            changed_from_last_round=_coerce_bool(obj.get("changed_from_last_round")),
            raw=json.dumps(obj, ensure_ascii=False),
        )
    return None


def strip_stance_block(text: str) -> str:
    """Remove the stance card from the prose — it is for the machine and should not end up in
    the turn people read.

    What is cut must be **the very card** :func:`parse_stance` adopted. When the two look for
    the card in different orders, you get "A was recorded, B was cut from the prose" — the
    recorded card stays in the prose and enters everyone else's context, while the card the
    author quoted from someone else is the one erased.
    So, like parse_stance, this takes the last one by **position in the text**.
    """
    spans: list[tuple[int, int, str]] = []
    for match in _FENCE.finditer(text):
        spans.append((match.start(), match.end(), match.group(1).strip()))
    for chunk, start, end in _balanced_objects(text):
        spans.append((start, end, chunk))

    for start, end, raw in sorted(spans, key=lambda item: item[0], reverse=True):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and _looks_like_stance(obj):
            return _tidy(text[:start] + text[end:])
    return text.strip()


def _tidy(text: str) -> str:
    """Tidy up after the cut: drop the leftover empty fence and the surplus blank lines."""
    text = re.sub(r"```(?:json|JSON)?[ \t]*\n\s*```", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip().rstrip("`").strip()


_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*[:：]\s*(.+?)\s*$")


def parse_verdict_lines(
    text: str, participant: str, round: int, others: list[str]
) -> Stance | None:
    """Parse the degraded format: one ``name: verdict | note`` per line.

    This is tier T2 of the extraction ladder. Against asking for the JSON again, its
    advantages are a **tiny output space** and that **a bad line costs one cell** — whereas
    one syntax error voids a whole JSON card.
    """
    valid = set(others)
    stance_on: dict[str, StanceOn] = {}
    confidence: float | None = None

    for raw in text.splitlines():
        match = _LINE.match(raw.strip().lstrip("-*").strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2)

        if key.lower() == "confidence":
            confidence = _coerce_confidence(value.split("|")[0].strip())
            continue
        if key not in valid:
            continue

        head, _, rest = value.partition("|")
        # The third segment is the verification note. The line table **must be able to express
        # verification too** — otherwise a participant whose first parse failed is moved to a format
        # with no room for verified, and their agree is doomed to be downgraded. That would be "a
        # rule imposed with no way to comply", by another route.
        note, _, verify_note = rest.partition("|")
        verdict = _coerce_verdict(head.strip().strip("`"))
        if verdict is None:
            continue
        note = note.strip()
        if verdict == "agree" and _reads_like_reservation(note):
            # `agree | but I still do not accept his cost estimate` is not agreement without
            # reservation.
            #
            # But **a note must not trigger a downgrade on sight**: on the structured path
            # `reason` is
            # "the reason for agreeing", semantically different from residuals
            # (reservations), and
            # agreeing explicitly while giving a reason is entirely normal. The previous
            # version
            # conflated them, so "your QPS estimate matches mine, I fully agree" was
            # recorded as an
            # open reservation.
            verdict = "partial"
        if verdict == "partial" and not note:
            # The same rule as the JSON path: a "partial agreement" with an empty payload cannot be
            # checked
            verdict = "unknown"
        stance_on[key] = StanceOn(
            verdict=verdict,
            reason=note,
            residuals=[note] if verdict == "partial" else [],
            verified=_verification_from_note(key, verify_note.strip()),
        )

    if not stance_on:
        return None
    return Stance(
        participant=participant,
        round=round,
        confidence=confidence,
        stance_on=stance_on,
        raw=text[-2000:],
    )
