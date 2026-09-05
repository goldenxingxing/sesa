"""A premise is something attackable, not a sentence buried in the prose.

To examine whether a chain holds, you have to be able to point at **one specific step** in
it. "I think that is wrong" cannot be checked; "your second premise does not hold in my
setting" can. Only as a separate structured field can a premise be overturned one by one,
and only then can the "veto a premise" intervention connect to anything.
"""

from __future__ import annotations

from sesa import prompts
from sesa.consensus.stance import parse_stance
from sesa.report import render_result
from sesa.state import RoundRecord, Turn
from sesa.types import Outcome, Result, Stance

CARD = """我主张用 SQLite。

```json
{
  "position": "用 SQLite",
  "confidence": 0.7,
  "premises": ["峰值 QPS 不超过 200", "团队里没有 DBA"],
  "key_claims": ["运维成本更低"],
  "stance_on": {},
  "open_questions": []
}
```
"""


def test_premises_are_parsed_as_a_field_not_left_in_prose():
    stance = parse_stance(CARD, "alice", 0, [])

    assert stance.premises == ["峰值 QPS 不超过 200", "团队里没有 DBA"]


def test_a_card_without_premises_still_parses():
    """An older format must not void the whole card just because a field was added."""
    stance = parse_stance('```json\n{"position": "用 SQLite", "stance_on": {}}\n```', "a", 0, [])

    assert stance is not None
    assert stance.premises == []


def test_others_see_the_premises_pulled_out_of_the_prose():
    """Buried in the prose they are easy to skip past — pulled out on their own, the others can go
    at them one by one.
    """
    record = RoundRecord(0)
    record.turns = [Turn("alice", 0, 0, "draft", "我主张用 SQLite。")]
    record.stances = {
        "alice": Stance(
            participant="alice", round=0, position="用 SQLite", premises=["峰值 QPS 不超过 200"]
        )
    }

    rendered = prompts.render_others(record, exclude="bob")

    assert "Premises alice declared" in rendered
    assert "峰值 QPS 不超过 200" in rendered


def test_unknown_stances_contribute_no_premises():
    """No writing on their behalf when extraction fails — what is not there cannot be conjured up."""
    record = RoundRecord(0)
    record.turns = [Turn("alice", 0, 0, "draft", "我主张用 SQLite。")]
    record.stances = {
        "alice": Stance(participant="alice", round=0, unknown=True, premises=["假的"])
    }

    assert "前提假设" not in prompts.render_others(record, exclude="bob")


def test_the_deliverable_states_what_the_conclusion_rests_on():
    result = Result(
        run_id="20260830-1",
        task="该用 Postgres 还是 SQLite？",
        outcome=Outcome.CONSENSUS,
        conclusion="用 SQLite。",
        premises={"alice": ["峰值 QPS 不超过 200"], "bob": ["数据量三年内不超过 10GB"]},
    )

    text = render_result(result)

    assert "本结论依赖的前提" in text
    assert "峰值 QPS 不超过 200" in text
    assert "数据量三年内不超过 10GB" in text
    # Premises are not decoration: the reader has to be told what to do when one does not hold
    assert "sesa resume 20260830-1 --inject" in text


def test_the_debate_prompt_asks_for_premises_before_conclusions():
    """Anyone can attack a conclusion; attacking the premises is what this deliberation can
    produce.
    """
    # **It has to hold in both languages.** The prompts follow the task, and checking only one
    # leaves open that the "ask for premises first" requirement has already been lost in the other
    # language's deliberations.
    from sesa import i18n

    for lang, premise, overturn in (
        ("en", "Premises", "Overturning one premise"),
        ("zh", "前提", "推翻一条前提"),
    ):
        i18n.use(lang)
        assert premise in prompts.ROUND_ZERO.format(task="T", injections="")
        assert overturn in prompts.DEBATE_ROUND.format(
            task="T",
            others_block="",
            consensus_block="",
            evidence_block="",
            thinking_block="",
            injections="",
        )
    i18n.use("en")
    assert "premises" in prompts.stance_instruction(["bob"])
