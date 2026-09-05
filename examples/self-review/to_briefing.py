#!/usr/bin/env python3
"""Turn an external code-review tool's JSON output into material for a Sesa deliberation.

Usage:
    ocr scan --path src/yourpkg --format json | python3 to_briefing.py > findings.md

Then hand it to **every** participant along with the topic:

    cat review-task.md findings.md > /tmp/task.md
    sesa run --file /tmp/task.md "..."

**Not through `briefing:`** (material private to one participant). This script used to
recommend that, on the grounds of "creating information asymmetry so the disagreement carries
information" — a scenario constructed for an experiment. In real use you have a scan report in
hand and there is no reason to show it to only one of them.

The wording of the header is deliberate too: **it asks the holder to verify each item rather
than relay it**. Measured, participants really do reject items the tool reported (2 in the
third round), and "the tool says there is a problem" is not an argument on its own — nobody can
examine what they cannot see.
"""

import json
import sys
from pathlib import Path

ORDER = {"high": 0, "medium": 1, "low": 2}

HEADER = """# Scan results from an external code-review tool

This was produced independently by an **external tool** and nobody has filtered it.

**It may contain false positives.** Your job is not to relay it but to verify it item by item:
raise what you can reproduce, and say plainly that an item does not hold when you cannot.
When you raise one, state the failure scenario **in your own words** — "the tool says there is
a problem here" is not an argument, and nobody can examine what they cannot see.
"""


def main() -> int:
    raw = sys.stdin.read() if len(sys.argv) < 2 else Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # This script consumes an external tool's output, whose format is not ours to control.
        # Crashing with a traceback would only make people think Sesa is at fault.
        print(f"cannot read the input JSON: {exc}", file=sys.stderr)
        return 1
    comments = parsed.get("comments") or [] if isinstance(parsed, dict) else []
    if not isinstance(comments, list):
        comments = []
    if not comments:
        print(HEADER + "\n(This scan found nothing. That is meaningful information; do not force it.)")
        return 0

    out = [HEADER]
    for i, c in enumerate(sorted(comments, key=lambda x: ORDER.get(x.get("severity"), 3)), 1):
        where = f"{c.get('path', '?')}:{c.get('start_line', '?')}"
        out.append(f"\n## [{i}] {c.get('severity', '?')} / {c.get('category', '?')} — {where}\n")
        out.append((c.get("content") or "").strip())
        if snippet := (c.get("existing_code") or "").strip():
            out.append(f"\nExisting code:\n```\n{snippet}\n```")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
