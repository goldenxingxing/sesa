"""Adapter for the OpenAI Chat Completions format.

One adapter covers a large field: DeepSeek / Kimi / OpenRouter / Ollama / vLLM / Groq /
Together / any self-hosted OpenAI-compatible endpoint. Only ``base_url`` and ``model``
change.

```yaml
- id: kimi
  adapter: openai_compat
  base_url: https://api.moonshot.cn/v1
  model: kimi-k2-0905-preview
  api_key: keyring            # keyring | plaintext | set api_key_env for an env var
```

**A note on cost: a thinking model's reasoning tokens count as output.** Measured, Kimi
spent **76%** of its output on reasoning (the prose was only 24%). And the deliberation
does not consume the reasoning — by default it never enters the other participants'
context — so it can simply be turned off when cost matters or for a controlled
experiment:

```yaml
  extra_body: { thinking: { type: disabled } }
```

``extra_body`` is merged into the request body verbatim, so any provider-specific
parameter can be passed through this way with no code change.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from ..i18n import t
from ..types import Chunk, Done, TextDelta, ThinkingDelta, Usage
from .base import Adapter, AdapterError

#: Default output cap. Enough for a full implementation plus a test suite, with room over.
DEFAULT_MAX_TOKENS = 16384

#: These fields belong to the adapter; extra_body must not override them.
_PROTOCOL_KEYS = frozenset({"stream", "stream_options", "messages", "model"})


class OpenAICompatAdapter(Adapter):
    name = "openai_compat"

    def __init__(self, spec) -> None:
        super().__init__(spec)
        opts = spec.options
        self.base_url: str = str(opts.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        if not spec.model:
            raise ValueError(
                t("Participant {pid}: adapter=openai_compat requires a model", pid=spec.id)
            )
        self.model: str = spec.model
        self.temperature = opts.get("temperature")
        #: Unset means the server default, and server defaults are often small (DeepSeek's is 4096).
        #: On a long task that default cuts the reply off mid-sentence, which looks like the model
        #: refusing to hand over the files.
        self.max_tokens = opts.get("max_tokens", DEFAULT_MAX_TOKENS)
        self.extra_body: dict = opts.get("extra_body") or {}
        self.headers_extra: dict = opts.get("headers") or {}
        self._api_key: str | None = None

    # ------------------------------------------------------------------ #

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
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            # Real usage is what makes honest budget accounting possible
            "stream_options": {"include_usage": True},
            # Protocol-critical fields must not be overridden: a user writing `stream: false` in
            # extra_body breaks the whole streaming contract, and the failure mode is "no chunk ever
            # arrives until the timeout".
            **{k: v for k, v in self.extra_body.items() if k not in _PROTOCOL_KEYS},
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens

        headers = {
            "Authorization": f"Bearer {self.resolve_key()}",
            "Content-Type": "application/json",
            **self.headers_extra,
        }

        usage = Usage.unknown()
        finish_reason: str | None = None
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
            ) as resp,
        ):
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise AdapterError(f"{self.id}: HTTP {resp.status_code} {detail}")

            malformed = 0
            produced = False
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    # A malformed frame must not be dropped in silence. When every frame is bad, the
                    # round produces an **empty reply that is recorded as a success** — the budget
                    # is charged, the conclusion is computed, and nothing is left at the scene of
                    # the failure.
                    malformed += 1
                    continue

                if err := obj.get("error"):
                    # An error block **mid-stream** (rate limit, upstream fault). Without raising,
                    # half a reply gets recorded as a successful turn — the same class of problem as
                    # being cut off by max_tokens, and the anthropic adapter has always raised: two
                    # adapters behaving differently about the same thing, so a user who switches
                    # provider meets entirely different failure semantics.
                    detail = err.get("message") if isinstance(err, dict) else str(err)
                    kind = (
                        err.get("type", "stream_error") if isinstance(err, dict) else "stream_error"
                    )
                    raise AdapterError(f"{self.id}: {kind}: {detail}")

                for choice in obj.get("choices") or []:
                    if reason := choice.get("finish_reason"):
                        finish_reason = reason
                    delta = choice.get("delta") or {}
                    # Kimi, DeepSeek-reasoner and others put the reasoning in reasoning_content.
                    # Keeping it as its own block is what makes "share the thinking or not" a real
                    # switch, instead of a convention in the prompt.
                    if reasoning := delta.get("reasoning_content"):
                        produced = True
                        yield ThinkingDelta(reasoning)
                    if piece := delta.get("content"):
                        produced = True
                        yield TextDelta(piece)

                if raw_usage := obj.get("usage"):
                    usage = Usage(
                        input_tokens=raw_usage.get("prompt_tokens"),
                        output_tokens=raw_usage.get("completion_tokens"),
                        usd=None,  # Pricing varies by provider; do not invent an amount
                        known=True,
                    )

        if malformed and not produced:
            # Not one word produced and every frame malformed — that is a failure, not "the model
            # had nothing to say". Returning an empty reply in silence has it recorded as a
            # successful empty turn: the budget is charged, the conclusion is computed, and nothing
            # is left at the scene of the failure.
            raise AdapterError(
                t(
                    "{pid}: the stream held {n} unparseable frames and produced no content",
                    pid=self.id,
                    n=malformed,
                )
            )

        # Truncation does not raise — the prose is often cut after the code block is complete, and
        # discarding the whole thing throws away usable work as well (4 turns were lost this way in
        # practice, one of which had already written 109 of 118 tests). But it cannot pass for a
        # complete turn either, so it is flagged.
        yield Done(usage, truncated=finish_reason == "length")
