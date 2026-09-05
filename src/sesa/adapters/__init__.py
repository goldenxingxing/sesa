"""Adapter registry.

Three adapters ship with sesa, covering the two ways of calling a model:

- ``cli``            — spawn a subprocess (claude code / codex / dsh / gemini-cli / aider …)
- ``openai_compat``  — OpenAI Chat Completions format (DeepSeek / Kimi / OpenRouter / Ollama …)
- ``anthropic``      — Anthropic Messages format

Third parties can inject their own with :func:`register`, without changing this package.
"""

from __future__ import annotations

from ..i18n import t
from ..types import ParticipantSpec
from .anthropic import AnthropicAdapter
from .base import Adapter, AdapterError, CheckResult
from .cli import CliAdapter
from .openai_compat import OpenAICompatAdapter

_REGISTRY: dict[str, type[Adapter]] = {
    CliAdapter.name: CliAdapter,
    OpenAICompatAdapter.name: OpenAICompatAdapter,
    AnthropicAdapter.name: AnthropicAdapter,
}


def register(adapter_cls: type[Adapter]) -> type[Adapter]:
    """Register a custom adapter (usable as a decorator)."""
    if not adapter_cls.name:
        raise ValueError(t("an adapter must define a non-empty name"))
    _REGISTRY[adapter_cls.name] = adapter_cls
    return adapter_cls


def available() -> list[str]:
    return sorted(_REGISTRY)


def build(spec: ParticipantSpec) -> Adapter:
    """Instantiate the adapter named by spec.adapter."""
    cls = _REGISTRY.get(spec.adapter)
    if cls is None:
        raise ValueError(
            t(
                "Participant {pid}: unknown adapter {name}; available: {options}",
                pid=spec.id,
                name=repr(spec.adapter),
                options=", ".join(available()),
            )
        )
    return cls(spec)


__all__ = [
    "Adapter",
    "AdapterError",
    "AnthropicAdapter",
    "CheckResult",
    "CliAdapter",
    "OpenAICompatAdapter",
    "available",
    "build",
    "register",
]
