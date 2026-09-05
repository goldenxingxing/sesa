"""Adapter for the Anthropic Messages API.

It cannot be folded into ``openai_compat``: system is a separate parameter rather than
a message, the response is content blocks, and the SSE event names are entirely
different.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from ..i18n import t
from ..types import Chunk, Done, TextDelta, ThinkingDelta, Usage
from .base import Adapter, AdapterError

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"


class AnthropicAdapter(Adapter):
    name = "anthropic"

    def __init__(self, spec) -> None:
        super().__init__(spec)
        opts = spec.options
        if not spec.model:
            raise ValueError(
                t("Participant {pid}: adapter=anthropic requires a model", pid=spec.id)
            )
        self.model: str = spec.model
        self.base_url: str = str(opts.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
        self.max_tokens: int = int(opts.get("max_tokens", 8192))
        self.temperature = opts.get("temperature")
        self.headers_extra: dict = opts.get("headers") or {}
        #: anything above 0 turns on extended thinking, which is what makes the reasoning stream out
        #: as ThinkingDelta
        self.thinking_budget: int = int(opts.get("thinking_budget", 0))
        self._api_key: str | None = None

    def resolve_key(self) -> str:
        if self._api_key is None:
            from ..credentials import resolve_api_key

            self._api_key = resolve_api_key(self.spec)
        return self._api_key

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        cwd: Path | None = None,
        timeout: float = 1800.0,
        context: dict[str, str] | None = None,
    ) -> AsyncIterator[Chunk]:
        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if system:
            body["system"] = system
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.thinking_budget and self.thinking_budget > 0:
            # Test it explicitly for positive. `if self.thinking_budget:` is also true for a
            # negative number, and a negative budget would be sent to the API verbatim — buying a
            # 400 "invalid parameter" whose message has nothing to do with the setting the user got
            # wrong. There is a thinking_delta to capture only once extended thinking is on
            body["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
            body.pop("temperature", None)  # mutually exclusive with thinking

        headers = {
            "x-api-key": self.resolve_key(),
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
            **self.headers_extra,
        }

        input_tokens = output_tokens = None
        stop_reason: str | None = None
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("POST", f"{self.base_url}/messages", json=body, headers=headers) as resp,
        ):
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise AdapterError(f"{self.id}: HTTP {resp.status_code} {detail}")

            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                kind = obj.get("type")
                if kind == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        yield ThinkingDelta(delta["thinking"])
                    elif delta.get("type") == "text_delta" and delta.get("text"):
                        yield TextDelta(delta["text"])
                elif kind == "message_start":
                    usage = (obj.get("message") or {}).get("usage") or {}
                    input_tokens = usage.get("input_tokens")
                elif kind == "message_delta":
                    usage = obj.get("usage") or {}
                    output_tokens = usage.get("output_tokens", output_tokens)
                    # A truncated reply looks exactly like a complete one. openai_compat had been
                    # reading finish_reason for ages while this one never read stop_reason — **two
                    # adapters behaving differently about the same thing**, so switching provider
                    # switches failure semantics.
                    stop_reason = (obj.get("delta") or {}).get("stop_reason", stop_reason)
                elif kind == "error":
                    err = obj.get("error") or {}
                    raise AdapterError(f"{self.id}: {err.get('type')}: {err.get('message')}")

        yield Done(
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=None,  # Pricing is not hard-coded, so it cannot go stale and mislead
                # the user
                known=input_tokens is not None or output_tokens is not None,
            ),
            truncated=stop_reason == "max_tokens",
        )
