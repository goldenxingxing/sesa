"""The prompts use **the task's** language, the interface uses **the interface's**.

The user's choice: the deliberation follows the task. Asking in Chinese should get you a
Chinese deliberation even when the interface is in English — which is exactly their real
case: an English interface reviewing a Chinese product-requirements document.

Before this, an English task was still sent Chinese prompts, models would very likely answer
in Chinese, and the output was simply unusable.
"""

from __future__ import annotations

import asyncio

import pytest

import sesa.prompts as prompts
from sesa import i18n
from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from sesa.workspace import LocalWorkspace
from tests.test_engine import participant


def _prompts_for(task: str, tmp_path) -> list[str]:
    """Run one deliberation and collect every prompt sent out."""
    seen: list[str] = []
    original = prompts.Template.format

    def spy(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        seen.append(out)
        return out

    prompts.Template.format = spy
    try:

        async def go():
            engine = Engine(
                [participant("a"), participant("b")],
                build_protocol("debate"),
                max_rounds=1,
                workspace=LocalWorkspace(tmp_path),
            )
            async for _ in engine.run(task):
                pass

        asyncio.run(go())
    finally:
        prompts.Template.format = original
    return seen


def test_a_chinese_task_gets_chinese_prompts_under_an_english_interface(tmp_path, monkeypatch):
    monkeypatch.setenv("SESA_LANG", "en")
    i18n.use("en")
    sent = _prompts_for("评审这份产品需求文档", tmp_path)
    assert sent, "not one prompt was sent?"
    assert sent[0].startswith("# 任务"), (
        f"a Chinese task received an English prompt: {sent[0][:40]!r}"
    )
    assert i18n.active() == "en", "the interface language was changed after the scope exited"


def test_an_english_task_gets_english_prompts(tmp_path, monkeypatch):
    monkeypatch.setenv("SESA_LANG", "zh")
    i18n.use("zh")
    sent = _prompts_for("Review this PRD carefully", tmp_path)
    assert sent[0].startswith("# Task"), (
        f"an English task received a Chinese prompt: {sent[0][:40]!r}"
    )
    assert i18n.active() == "zh"


def test_the_first_round_is_covered_too(tmp_path, monkeypatch):
    """**Round 0's prompts are evaluated in place inside plan()**, not deferred.

    With the scope covering only _run_move they are already formed in the interface language —
    which is exactly how it slipped through: a Chinese task got English prompts while the later
    rounds were right.
    """
    import inspect

    monkeypatch.setenv("SESA_LANG", "en")
    i18n.use("en")
    source = inspect.getsource(Engine.run)
    assert "with scoped(prompts.pick_language(state.task))" in source

    sent = _prompts_for("评审这份产品需求文档", tmp_path)
    assert sent[0].startswith("# 任务")


@pytest.mark.parametrize("name", ["SYSTEM", "ROUND_ZERO", "DEBATE_ROUND"])
def test_every_big_template_has_a_chinese_translation(name):
    """A template left untranslated sends English down that path — while the task is Chinese."""
    from sesa.locales.zh import CATALOGUE

    assert getattr(prompts, name) in CATALOGUE, f"{name} has no Chinese translation"


def test_templates_translate_at_format_time_not_import_time():
    """The templates are module-level constants, and at import time the language is not resolved
    yet.

    They have six use sites scattered across the protocols, and wrapping t() at each of them
    means one missed site leaves that path permanently in English — and the missed one is most
    likely the least-travelled protocol, which is also the last to be noticed.
    """
    i18n.use("zh")
    assert prompts.ROUND_ZERO.format(task="T", injections="").startswith("# 任务")
    i18n.use("en")
    assert prompts.ROUND_ZERO.format(task="T", injections="").startswith("# Task")


def test_the_system_prompt_follows_the_task_not_the_caller():
    """Both call sites of the system prompt are outside the engine's ``scoped`` blocks.

    Once it depends on the caller wrapping it, the missed call site leaves a Chinese task's
    participants receiving an English system prompt — and the system prompt is the strongest
    signal there is for "which language to speak".
    """
    from sesa import i18n, prompts
    from sesa.state import DeliberationState
    from sesa.types import ParticipantSpec

    def system_for(task: str) -> str:
        state = DeliberationState(
            task=task,
            participants=[ParticipantSpec(id="alice", adapter="cli", options={})],
            max_rounds=2,
        )
        return prompts.system_prompt(state, "alice")

    i18n.use("en")  # the interface language is English, and **it is unaffected by the
    # call**
    assert "你正在参加" in system_for("这份需求文档有什么问题？")
    assert "You are taking part" in system_for("What is wrong with this PRD?")
    assert i18n.active() == "en"
