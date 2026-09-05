"""Persistence and review.

Three layers of output (see DESIGN.md §7.4):

```
.sesa/runs/<run_id>/
├── RESULT.md      the main deliverable, ready to use
├── RESULT.json    the same thing structured, for MCP and third-party products
├── REPORT.md      the minutes of the deliberation
├── events.jsonl   the raw event stream, replayable and evaluable
└── turns/         everyone's raw text per round, for tracing back
```

``events.jsonl`` is the only source of truth: both ``RESULT`` and ``REPORT`` can be
rebuilt from it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
import warnings
from dataclasses import asdict
from pathlib import Path

from .events import Event
from .i18n import t
from .state import Turn
from .types import Result


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


class Recorder:
    """Write the event stream to JSONL, and save each round's raw text."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.dir = Path(root) / "runs" / run_id
        self.turns_dir = self.dir / "turns"
        self.turns_dir.mkdir(parents=True, exist_ok=True)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    # ------------------------------------------------------------------ #

    def emit(self, event: Event) -> None:
        # Note: signal reentrancy **cannot** be prevented here by "build the string first, then
        # write". Both forms in Python evaluate the arguments and then call write; they are
        # semantically identical — I once split it into two lines with a comment claiming it blocked
        # interleaved writes, and that comment was false and has been removed. The real reentrancy
        # protection is in `emit_abort` (for the signal path only, bypassing the buffer).
        self._events.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        self._events.flush()  # leave a reviewable scene even on a crash

    def emit_abort(self, event: Event) -> bool:
        """Persistence for signal handlers only: **bypass the buffer and write straight to the fd**.

        The abort handler runs on the main thread and can land in the middle of an ``emit``.
        Calling ``emit`` then re-enters the same ``BufferedWriter``, and CPython raises
        ``RuntimeError: reentrant call`` — which the handler's ``finally: raise SystemExit``
        swallows along with everything else. **The result is that the abort record is silently
        lost, and leaving that record is the only reason this handler exists.**

        ``os.write`` goes to the raw fd, never touching Python's buffer, so there is no
        reentrancy problem.
        Returns whether the write succeeded, so the caller can say so rather than swallow it.
        """
        try:
            payload = (json.dumps(event.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
            os.write(self._events.fileno(), payload)
            return True
        except Exception:
            # This is already the last line of defence; raising here would only bury the real reason
            # for the abort.
            return False

    #: The divider between the prose and the archived content. ``load_state`` splits on it — the
    #: prose enters other people's context, the archived sections (raw output, reasoning) **must
    #: not**.
    ARCHIVE_MARK = "\n\n<!-- sesa:archive-below -->\n"

    def save_turn(self, turn: Turn) -> Path:
        path = self.turns_dir / f"r{turn.round:02d}_p{turn.phase}_{turn.participant}_{turn.kind}.md"
        body = turn.text
        archived = False
        if turn.raw and turn.raw.strip() != turn.text.strip():
            body += self.ARCHIVE_MARK
            archived = True
            # The archive keeps the raw output. The stance card is stripped so that machine-readable
            # matter does not go into other people's context; stripping it from the archive too
            # would leave no way ever to check whether the parsing was right.
            body += (
                "\n\n---\n\n<details><summary>"
                + t("the model's raw output (stance card included; archive only)")
                + "</summary>\n\n````\n"
                + turn.raw.strip()
                + "\n````\n\n</details>\n"
            )
        if turn.thinking.strip():
            # The reasoning goes to disk only and never into other people's context (see DESIGN.md
            # §4.6)
            if not archived:
                body += self.ARCHIVE_MARK
            body += (
                "\n\n---\n\n<details><summary>"
                + t("its reasoning (not shared with the other participants)")
                + "</summary>\n\n"
            )
            body += turn.thinking + "\n\n</details>\n"
        path.write_text(body, encoding="utf-8")
        return path

    def save_snapshot(self, round_index: int, participant: str, files: dict[str, str]) -> Path:
        """Write someone's working copy to disk as it stood at the end of a round.

        Without this step, **the round-by-round work of an agent CLI that writes its own files can
        never be recovered** — its code does not appear in the turn's prose, and the branch keeps
        only the final state. Measured: a heterogeneous experiment could not answer "did claude
        regress part-way" for this reason, and could only look at the end state.
        """
        target = self.dir / "snapshots" / f"r{round_index:02d}" / participant
        target.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return target

    def save_briefing(self, participant: str, text: str) -> Path:
        """Write private material to disk. **Private ≠ leaves no trace** — it influenced this
        deliberation, so it must be reviewable.
        """
        target = self.dir / "briefings"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{participant}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def save_result(self, result: Result, markdown: str) -> Path:
        (self.dir / "RESULT.json").write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        path = self.dir / "RESULT.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def save_report(self, markdown: str) -> Path:
        path = self.dir / "REPORT.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def close(self) -> None:
        if not self._events.closed:
            self._events.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_events(run_dir: Path) -> list[dict]:
    """Read the event stream back, for replay, reporting and evaluation.

    **Skip broken lines; never let one line destroy a whole run.**

    The event stream is this product's only source of truth, and there is always a chance of
    its being written badly: the process ``SIGKILL``-ed mid-``write``, which no signal handler
    can stop; a full disk; a power cut. This used to call ``json.loads`` on a broken line and
    let it raise, so **one bad byte made a whole deliberation unreadable forever** — report
    could not read it, resume could not continue it, eval could not compute over it, while
    99.9% of the content was perfectly good.

    Returning the number of skipped lines to the caller would be the better design, but it
    would touch every call site; the compromise is a warning, so that "I skipped something"
    is at least not silent.
    """
    path = Path(run_dir) / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(t("no event stream found: {path}", path=path))
    out, broken = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            broken += 1
    if broken:
        warnings.warn(
            t(
                "{path} holds {broken} unparseable lines, which were skipped (this happens "
                "when the process is killed mid-write). The other {kept} events are still "
                "usable, but this run's record is **incomplete**.",
                path=path,
                broken=broken,
                kept=len(out),
            ),
            RuntimeWarning,
            stacklevel=2,
        )
    return out


def load_state(
    run_dir: Path, participants: list, *, max_rounds: int, share_thinking: str = "never"
):
    """Restore a deliberation's state from the persisted event stream and ``turns/``, for
    ``resume``.

    This is "the event stream is the only source of truth" delivered directly: no extra
    snapshot file is needed, ``events.jsonl`` plus ``turns/`` is enough to rebuild.

    The participant list must match the original run — with different people it is not a
    continuation of the same deliberation, and the positions in everyone's stance cards would
    point at people who are not there.
    """
    from .state import DeliberationState, RoundRecord, Turn
    from .types import Stance

    run_dir = Path(run_dir)
    events = read_events(run_dir)

    start = next((e for e in events if e["t"] == "run.start"), None)
    if start is None:
        raise ValueError(
            t(
                "the event stream in {dir} has no run.start, so it cannot be resumed",
                dir=run_dir,
            )
        )

    original = list(start.get("participants") or [])
    current = [p.id for p in participants]
    # Compare **sorted lists**, not sets. A set lets a duplicate through: the original [a, b] and
    # the current [a, a, b] are the same set, and continuing from there gives a two seats and a
    # position on itself, while the disagreement matrix is indexed by id and the second a overwrites
    # the first.
    if sorted(original) != sorted(current):
        raise ValueError(
            t(
                "The participant list differs from the original run, so it cannot be "
                "resumed.\n  originally: {before}\n  now: {after}\n"
                "With different people it is not a continuation of the same deliberation, "
                "and the positions in everyone's stance cards would point at people who "
                "are not there.",
                before=", ".join(original),
                after=", ".join(current),
            )
        )

    state = DeliberationState(
        task=start.get("task", ""),
        participants=participants,
        max_rounds=max_rounds,
        share_thinking=share_thinking,
    )

    # The truncation flag lives only in the event stream; the .md files under turns/ do not show it.
    # Without it, after a resume `statements()` no longer adds the warning and the engine no longer
    # refuses a half-finished stance card.
    truncated_turns: dict[tuple[int, int, str, str], bool] = {}
    truncated_fallback: dict[tuple[int, str], bool] = {}
    for e in events:
        if e["t"] != "turn.end" or not e.get("truncated"):
            continue
        key = (e.get("round", 0), e.get("participant", ""))
        truncated_fallback[key] = True
        truncated_turns[(key[0], e.get("phase", 0), key[1], e.get("kind", "draft"))] = True

    # The prose is under turns/, the stance is in the event stream — each restored from its own
    # place
    records: dict[int, RoundRecord] = {}
    for path in sorted(run_dir.glob("turns/r*_p*_*.md")):
        try:
            round_part, phase_part, rest = path.stem.split("_", 2)
            index, phase = int(round_part[1:]), int(phase_part[1:])
            participant, _, kind = rest.rpartition("_")
        except ValueError:
            continue  # Skip a file whose name does not follow the convention, rather than
            # let one bad file destroy a whole resume
        if participant not in current:
            continue
        records.setdefault(index, RoundRecord(index)).turns.append(
            Turn(
                participant=participant,
                round=index,
                phase=phase,
                kind=kind,
                # **Take only the prose before the divider.** Reading the whole file would bring the
                # archived reasoning along with it, and turn.text is what enters the other
                # participants' context — so share_thinking=never would be silently broken after a
                # resume.
                text=path.read_text(encoding="utf-8").split(Recorder.ARCHIVE_MARK)[0].rstrip(),
                # The truncation flag has to come back with it. Without it, after a resume
                # `statements()` no longer adds the warning and the engine no longer refuses a
                # half-finished stance card — **two defences silently down at once**, while the
                # event stream recorded the fact all along.
                truncated=truncated_turns.get(
                    (index, phase, participant, kind),
                    # Older event streams have no phase/kind, so fall back to a coarse match: better
                    # to mark another turn from the same round as truncated too than to lose the
                    # fact entirely.
                    truncated_fallback.get((index, participant), False),
                ),
            )
        )

    # The archived raw output holds the complete stance card. When the event payload is missing a
    # field (an older record, or a field added since), fill it in from there — otherwise
    # "``events.jsonl`` plus ``turns/`` is enough to rebuild" is an empty claim. Measured with
    # premises missing: the conclusion came back and the premises were lost, and the premises are
    # exactly what `resume --inject` exists to veto.
    archived: dict[tuple[int, str], str] = {}
    for path in sorted(run_dir.glob("turns/r*_p*_*.md")):
        try:
            round_part, _, rest = path.stem.split("_", 2)
            index = int(round_part[1:])
            participant, _, _ = rest.rpartition("_")
        except ValueError:
            continue
        text = path.read_text(encoding="utf-8")
        if Recorder.ARCHIVE_MARK in text and (blocks := text.split("````")) and len(blocks) > 1:
            # Filenames are walked in lexical order, so within a round draft sorts before revise and
            # the later write (revise) overwrites the earlier — which is exactly the "last turn" we
            # want. But the index has to carry that meaning, or it looks like an accidental
            # overwrite.
            archived[(index, participant)] = blocks[1]

    for event in events:
        if event["t"] != "stance.emit":
            continue
        index, participant = event["round"], event["participant"]
        raw = event.get("stance") or {}
        if participant not in current:
            continue
        records.setdefault(index, RoundRecord(index)).stances[participant] = Stance(
            participant=participant,
            round=index,
            position=raw.get("position", ""),
            # Missing is missing. Flattening it to 0.0 lets "not reported" pass for "I am very
            # unsure" — types.py has just separated the two, and this restore path must not join
            # them back up.
            confidence=_coerce_optional_float(raw.get("confidence")),
            premises=list(
                raw["premises"]
                if "premises" in raw
                else _recovered(archived, index, participant, "premises")
            ),
            key_claims=list(
                # `or` treats an **explicitly recorded empty list** as "the field is missing" and
                # goes digging in the archive. The event stream is the only truth: if it says "no
                # key claims", there are none — digging a few out of the archive grows the
                # deliverable things the participant never said.
                raw["key_claims"]
                if "key_claims" in raw
                else _recovered(archived, index, participant, "key_claims")
            ),
            stance_on={
                target: _restore_stance_on(
                    verdict,
                    # A resume that loses reason and residuals has everyone forgetting what they
                    # objected to — and the disagreements get reinvented from scratch
                    (raw.get("reasons") or {}).get(target, ""),
                    list((raw.get("residuals") or {}).get(target, [])),
                    list((raw.get("verified") or {}).get(target, [])),
                )
                for target, verdict in (raw.get("stance_on") or {}).items()
            },
            changed_from_last_round=bool(raw.get("changed")),
            unknown=bool(raw.get("unknown")),
        )

    ordered = [records[i] for i in sorted(records)]

    # **Rounds at the end that produced nothing are not carried into the resume.**
    # Measured: a deliberation ran out of wall-clock budget, and in round 3 all three ran for 309
    # seconds and produced 0 characters. Read back as-is, the parties would find "total silence last
    # round" in their context on resume — and that is an artefact of a budget failure, not anyone's
    # opinion. Asking them to explain a silence that never happened injects noise into the
    # deliberation.
    # Only trim from **the end**: a round wiped out in the middle really did happen (everyone timing
    # out, say), belongs to the deliberation's own history, and must not be erased.
    # The test has to look at **both turns and stance cards**. Looking only at turns would wrongly
    # cut old records and externally assembled event streams — those may hold only stance.emit with
    # no turns/*.md, and a stance card is solid content. (Two existing tests caught this on the
    # spot.)
    def _barren(record) -> bool:
        return not any(turn.ok for turn in record.turns) and not record.stances

    while ordered and _barren(ordered[-1]):
        ordered.pop()

    state.rounds = ordered
    return state


def _recovered(archived: dict, index: int, participant: str, field: str) -> list:
    """When the event payload is missing a field, go back to the archived raw stance card.

    It only fills gaps, never overwrites: the event stream remains the preferred source and
    this is its backstop.
    """
    if not (body := archived.get((index, participant))):
        return []
    from .consensus.stance import parse_stance

    stance = parse_stance(body, participant, index, [])
    return list(getattr(stance, field, None) or []) if stance else []


def _coerce_optional_float(value) -> float | None:
    """``None`` means not reported, kept strictly apart from a reported 0.0.

    It has to normalise 0–100 too: the parsing layer has always done so (models filling in
    0–100 is common) and this restore path did not. Once the 0–1 range check moved into
    ``__post_init__``, any old record holding 85 would make `resume` **crash outright** —
    **a construction path missed when adding a check, for the second time that day.**
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _restore_verifications(raw: list) -> list:
    """Restore verification records. The same rule as the parsing layer: **an incomplete record
    is dropped**.

    A missing ``result`` must not be filled in as ``reproduced`` above all — that
    manufactures an "I checked it" out of nothing, and the whole foundation of an agreement
    rests on it.
    """
    from .types import Verification

    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        how, result = str(item.get("how") or ""), str(item.get("result") or "")
        if how not in ("executed", "cited", "unable") or result not in (
            "reproduced",
            "refuted",
            "unable",
        ):
            continue
        if not (of := str(item.get("of") or "").strip()):
            continue
        out.append(
            Verification(
                of=of,
                how=how,
                result="unable" if how == "unable" else result,
                detail=str(item.get("detail") or ""),
            )
        )
    return out


def _restore_stance_on(
    verdict: str, reason: str, residuals: list[str], verified: list | None = None
):
    """Restore one cell of a position from an old record, following **the same rule as the
    parsing layer**.

    A partial with an empty payload is treated as unknown — the parsing layer has always had
    this rule, and the restore layer used to construct it directly. Once the invariant moved
    into ``__post_init__``, any record holding such historical data would make ``resume``
    crash outright: **a correct check added, without checking every path into that type.**
    """
    from .types import StanceOn

    if verdict == "partial" and not residuals:
        return StanceOn(verdict="unknown", reason=reason)
    return StanceOn(
        verdict=verdict,
        reason=reason,
        residuals=residuals,
        # The verification records have to come back with it. Without them, after a resume every
        # agree has no foundation and is downgraded to not measured — **a resumed deliberation could
        # never reach consensus.**
        verified=_restore_verifications(verified or []),
    )
