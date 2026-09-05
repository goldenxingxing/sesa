"""The self-review output of two weaker models, Kimi + DeepSeek (Claude did not take part).

**I did not write this file.** It comes from a kimi + deepseek deliberation in which both
took **the same** external scan material (through `--file`, visible to everyone) and Claude
was deliberately left out, to answer: **can two weaker models plus an external tool stand in
for one strong model?**

Across two rounds of that same deliberation kimi wrote two **mutually contradictory** tests:
one arguing that an in-stream error should raise, the other asserting that it is silently
dropped. The author ruled the first one right — the second's comment was "neither an error
payload nor a parse exception triggered AdapterError", which **describes the behaviour at
the time** rather than arguing that it should be so. It has been rewritten to assert the
ruled-on behaviour, with the reasoning in that test.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sesa.adapters.anthropic import AnthropicAdapter
from sesa.adapters.base import AdapterError
from sesa.adapters.openai_compat import OpenAICompatAdapter
from sesa.types import Chunk, ParticipantSpec


def _long_key() -> str:
    return "sk-" + "x" * 48


# ═══════════════════════════════════════════════════════════════════════════ # The Anthropic
# adapter: already handles in-stream errors correctly
# ═══════════════════════════════════════════════════════════════════════════ #


@pytest.mark.asyncio
async def test_anthropic_adapter_raises_on_stream_error():
    """An Anthropic in-stream error event is recognised and raises AdapterError."""
    spec = ParticipantSpec(id="x", adapter="anthropic", model="m", options={"api_key": _long_key()})
    adapter = AnthropicAdapter(spec)
    adapter._api_key = _long_key()

    async def fake_stream():
        yield b'data: {"type": "error", "error": {"type": "rate_limit_error", "message": "too fast"}}\n\n'

    class FakeResp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            async for line in fake_stream():
                yield line.decode("utf-8")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            class Ctx:
                async def __aenter__(self):
                    return FakeResp()

                async def __aexit__(self, *args):
                    return False

            return Ctx()

    with patch("httpx.AsyncClient", FakeClient), pytest.raises(AdapterError, match="too fast"):
        async for _ in adapter.stream("hi"):
            pass


# ═══════════════════════════════════════════════════════════════════════════ # The
# OpenAI-compatible adapter: the same error goes unhandled
# ═══════════════════════════════════════════════════════════════════════════ #


@pytest.mark.asyncio
async def test_openai_compat_adapter_should_raise_on_stream_error():
    """An OpenAI-compatible in-stream error payload is currently dropped silently, and **this test
    expects it to raise**.

    The current implementation parses only choices/usage, hits the error field and continues, and
    at the end of the loop yields Done(usage=unknown, truncated=False).
    So the test fails — and the failure is itself the finding.
    """
    spec = ParticipantSpec(
        id="x", adapter="openai_compat", model="m", options={"api_key": _long_key()}
    )
    adapter = OpenAICompatAdapter(spec)
    adapter._api_key = _long_key()

    async def fake_stream():
        # give one line of valid text first, so the caller believes there is normal output
        yield b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        # then the in-stream error — OpenAI answers HTTP 200 with data: error in some cases
        yield b'data: {"error": {"message": "rate limit", "type": "rate_limit"}}\n\n'

    class FakeResp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            async for line in fake_stream():
                yield line.decode("utf-8")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            class Ctx:
                async def __aenter__(self):
                    return FakeResp()

                async def __aexit__(self, *args):
                    return False

            return Ctx()

    chunks: list[Chunk] = []
    with (
        patch("httpx.AsyncClient", FakeClient),
        pytest.raises(AdapterError, match="rate limit"),
    ):
        async for chunk in adapter.stream("hi"):
            chunks.append(chunk)

    # Reaching here means the current implementation has been fixed; otherwise the pytest.raises
    # above fails.
