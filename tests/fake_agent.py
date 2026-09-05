#!/usr/bin/env python3
"""A fake participant for the tests: a real CLI agent that reads a prompt from stdin and
writes its reply to stdout.

A real subprocess rather than a mock, because the contract between Engine and CliAdapter
(streaming, exit codes, feeding stdin, stance-card extraction) holds only over a real
process.

Its behaviour is controlled by environment variables:

* ``FAKE_ID``       the participant id
* ``FAKE_VERDICT``  its stance on the others: agree / disagree
* ``FAKE_CONF``     the confidence
* ``FAKE_MODE``     normal | no_stance (emit no stance card, to test the retry and unknown)
                    | crash (non-zero exit, to test that one failure does not sink the run)
* ``FAKE_CONFLICT`` report a conflict in conflicts_found while drafting (to test false
                    consensus)
* ``FAKE_DRAFT_DISAGREE`` list an open disagreement while drafting (to test false-consensus
                    detection in the other direction)
* ``FAKE_DUMP``     append the prompt received to this file (to assert on how prompts are
                    assembled)
* ``FAKE_WRITE``    ``<relative path>=<contents>``, written into the working directory each
                    round (to test copy detection). Rounds are separated by ``|``, round N
                    takes segment N; if there are too few segments the last one is reused
"""

import json
import os
import pathlib
import re
import sys

ID = os.environ.get("FAKE_ID", "fake")
VERDICT = os.environ.get("FAKE_VERDICT", "agree")
CONF = float(os.environ.get("FAKE_CONF", "0.9"))
MODE = os.environ.get("FAKE_MODE", "normal")
CONFLICT = os.environ.get("FAKE_CONFLICT", "")
#: Set true to simulate a participant that says agree without having checked the other's evidence.
#: False by default — a normal participant **follows the schema**, and so should the fixture.
SKIP_VERIFY = os.environ.get("FAKE_SKIP_VERIFY", "") == "1"


def _verified(other: str) -> list:
    """Simulate having verified the other's evidence. Without it an agree is treated as no
    position taken.
    """
    if SKIP_VERIFY or VERDICT != "agree":
        return []
    return [
        {
            "of": f"{other} 的关键主张",
            "how": "executed",
            "result": "reproduced",
            "detail": f"跑了 {other} 给的命令，结果与其所述一致",
        }
    ]


DRAFT_DISAGREE = os.environ.get("FAKE_DRAFT_DISAGREE", "")
WRITE = os.environ.get("FAKE_WRITE", "")


def others_from(prompt: str) -> list[str]:
    match = re.search(r"其他参与者：(.+)", prompt)
    if not match:
        return []
    return [x.strip() for x in match.group(1).split("、") if x.strip() and x.strip() != "（无）"]


def main() -> int:
    prompt = sys.stdin.read()

    if WRITE:
        # The round is recognised from the prompt: round 0 is the independent draft, everything
        # after carries the previous round
        stage = 0 if "其他参与者的当前观点" not in prompt else 1
        chunks = WRITE.split("|")
        path, _, body = chunks[min(stage, len(chunks) - 1)].partition("=")
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.replace("\\n", "\n"), encoding="utf-8")

    if dump := os.environ.get("FAKE_DUMP"):
        with open(dump, "a", encoding="utf-8") as fh:
            fh.write(prompt + "\n\x00\n")

    if MODE == "crash":
        sys.stderr.write("fake agent 故意失败\n")
        return 1

    # a rapporteur request
    if "你是本场议事的**执笔人**" in prompt:
        draft = {
            "conclusion": f"由 {ID} 整合的结论。",
            "grounds": ["各方认同的关键论点"],
            "disagreements": (
                [
                    {
                        "topic": DRAFT_DISAGREE,
                        "positions": {},
                        "root_cause": "前提假设不同",
                        "decisive_question": "实际量级是多少？",
                    }
                ]
                if DRAFT_DISAGREE
                else []
            ),
            "minority": {},
            "conflicts_found": [CONFLICT] if CONFLICT else [],
        }
        print("```json")
        print(json.dumps(draft, ensure_ascii=False))
        print("```")
        return 0

    others = others_from(prompt)

    # a retry request asking for the stance card alone
    if "只输出一个 json 代码块" in prompt and "你刚才的发言是" in prompt:
        if MODE == "no_stance":
            print("我还是不想给结构化数据。")
            return 0
        print("```json")
        print(
            json.dumps(
                {
                    "position": f"{ID} 的立场",
                    "confidence": CONF,
                    "stance_on": {
                        o: {
                            "verdict": VERDICT,
                            "reason": "理由",
                            "residuals": ["尚未接受的具体点"] if VERDICT == "partial" else [],
                            "verified": _verified(o),
                        }
                        for o in others
                    },
                },
                ensure_ascii=False,
            )
        )
        print("```")
        return 0

    print(f"我是 {ID}，这是我这一轮的正式发言。")
    if MODE == "no_stance":
        return 0
    print()
    print("```json")
    print(
        json.dumps(
            {
                "position": f"{ID} 的立场",
                "confidence": CONF,
                "key_claims": ["主张一"],
                "stance_on": {
                    o: {
                        "verdict": VERDICT,
                        "reason": "理由" if VERDICT != "agree" else "",
                        # partial requires non-empty residuals, or it is treated as no position
                        # taken
                        "residuals": ["尚未接受的具体点"] if VERDICT == "partial" else [],
                        "verified": _verified(o),
                    }
                    for o in others
                },
                "open_questions": [],
                "changed_from_last_round": False,
            },
            ensure_ascii=False,
        )
    )
    print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
