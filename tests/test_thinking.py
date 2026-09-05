"""Capturing and isolating the reasoning draft."""

from __future__ import annotations

import json

import httpx
import pytest

from sesa.adapters.anthropic import AnthropicAdapter
from sesa.adapters.openai_compat import OpenAICompatAdapter
from sesa.types import Done, ParticipantSpec, TextDelta, ThinkingDelta


def sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


async def drain(adapter, prompt="hi"):
    think, text = [], []
    async for chunk in adapter.stream(prompt, timeout=5):
        if isinstance(chunk, ThinkingDelta):
            think.append(chunk.text)
        elif isinstance(chunk, TextDelta):
            text.append(chunk.text)
        elif isinstance(chunk, Done):
            pass
    return "".join(think), "".join(text)


@pytest.fixture
def stub(monkeypatch):
    def install(payload: bytes):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload)

        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    return install


async def test_openai_compat_captures_reasoning_content(stub):
    """Kimi, DeepSeek-reasoner and others put the reasoning in reasoning_content."""
    stub(
        sse(
            [
                {"choices": [{"delta": {"reasoning_content": "让我先比较小数位…"}}]},
                {"choices": [{"delta": {"content": "9.8 更大"}}]},
            ]
        )
    )
    adapter = OpenAICompatAdapter(
        ParticipantSpec(
            id="kimi",
            adapter="openai_compat",
            model="m",
            options={"base_url": "https://x/v1", "api_key": "k" * 20},
        )
    )
    think, text = await drain(adapter)
    assert think == "让我先比较小数位…"
    assert text == "9.8 更大"  # the reasoning must never get mixed into the prose


async def test_anthropic_captures_thinking_delta(stub):
    stub(
        sse(
            [
                {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "推理"},
                },
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "结论"}},
            ]
        )
    )
    adapter = AnthropicAdapter(
        ParticipantSpec(id="claude", adapter="anthropic", model="m", options={"api_key": "k" * 20})
    )
    think, text = await drain(adapter)
    assert think == "推理"
    assert text == "结论"


def test_extended_thinking_is_off_unless_a_budget_is_given():
    """The thinking budget has to be given explicitly — we do not turn on a switch that changes
    billing on the user's behalf.
    """
    plain = AnthropicAdapter(
        ParticipantSpec(id="c", adapter="anthropic", model="m", options={"api_key": "k" * 20})
    )
    assert plain.thinking_budget == 0
    enabled = AnthropicAdapter(
        ParticipantSpec(
            id="c",
            adapter="anthropic",
            model="m",
            options={"api_key": "k" * 20, "thinking_budget": 4096},
        )
    )
    assert enabled.thinking_budget == 4096


# --------------------------------------------------------------------------- # Truncation
# --------------------------------------------------------------------------- #


def _deepseek() -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        ParticipantSpec(
            id="deepseek",
            adapter="openai_compat",
            model="deepseek-chat",
            options={"base_url": "https://x/v1", "api_key": "k" * 20},
        )
    )


async def test_a_truncated_reply_keeps_its_text_but_is_marked(stub):
    """Truncation should neither discard the whole thing nor pass for a complete turn.

    I once changed "accept truncation silently" into "raise on truncation", and in one
    experiment 4 turns were discarded whole, one of which had already written 109 of 118 tests —
    truncation usually happens after the code block is complete. The right thing is to keep the
    prose and flag it.
    """
    stub(
        sse(
            [
                {
                    "choices": [
                        {"delta": {"content": "```python name=semver.py\nOK\n```\n然后我还想说"}}
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
            ]
        )
    )

    chunks = [c async for c in _deepseek().stream("写个实现")]

    assert any(isinstance(c, TextDelta) for c in chunks), "the prose has to be kept"
    done = chunks[-1]
    assert isinstance(done, Done)
    assert done.truncated, "it has to be flagged, or it passes for a complete turn"


async def test_a_normal_stop_is_not_reported_as_truncation(stub):
    stub(
        sse(
            [
                {"choices": [{"delta": {"content": "写完了"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        )
    )

    chunks = [c async for c in _deepseek().stream("写完了吗")]

    assert not chunks[-1].truncated


def test_output_budget_defaults_to_something_that_fits_a_whole_file():
    """The server default is often only 4096, which cuts a long task's reply off mid-sentence."""
    assert _deepseek().max_tokens >= 16384
