"""Budget and circuit breaker.

An honesty requirement: CLI adapters usually cannot report token counts, and we
**do not estimate** — an estimate passed off as real usage turns the cost figures
into a lie. So the wall clock is the fallback gate that is always available, and
the cost/token limits only take effect when real usage is actually available.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .i18n import t
from .types import Usage


@dataclass
class Budget:
    max_usd: float | None = None
    max_tokens: int | None = None
    max_wall_seconds: float | None = None

    started_at: float = field(default_factory=time.monotonic)
    spent_usd: float = 0.0
    spent_tokens: int = 0
    #: how many calls could not report real usage — used to explain to the user why the cost may be
    #: an undercount
    unknown_calls: int = 0
    #: how many calls reported token counts but no amount from the provider. The built-in adapters
    #: never invent pricing (``usd=None``), so in practice this equals the total number of calls —
    #: which is to say ``max_usd`` never fires at all.
    priceless_calls: int = 0

    def reset(self) -> None:
        """Reset the budget. **The spend is cleared too** — resetting only the timer would have
        the next deliberation start carrying the last one's bill, hitting the limit early and
        looking as though the models had suddenly got more expensive.
        """
        self.started_at = time.monotonic()
        self.spent_usd = 0.0
        self.spent_tokens = 0
        self.unknown_calls = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def add(self, usage: Usage) -> None:
        if not usage.known:
            self.unknown_calls += 1
            return
        if usage.usd is None:
            # Token counts arrived but no amount. **That is not the same as having spent nothing** —
            # adding it up as 0 turns max_usd into a setting that looks like it is in charge and is
            # not.
            self.priceless_calls += 1
        self.spent_usd += usage.usd or 0.0
        self.spent_tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0)

    # ------------------------------------------------------------------ #

    def exceeded(self) -> str | None:
        """Why the budget is spent; ``None`` means there is room left."""
        if self.max_wall_seconds is not None and self.elapsed >= self.max_wall_seconds:
            return t(
                "wall clock at {used:.0f}s, the limit of {cap:.0f}s is reached",
                used=self.elapsed,
                cap=self.max_wall_seconds,
            )
        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            return t(
                "${used:.2f} spent, the limit of ${cap:.2f} is reached",
                used=self.spent_usd,
                cap=self.max_usd,
            )
        if self.max_tokens is not None and self.spent_tokens >= self.max_tokens:
            return t(
                "{used} tokens used, the limit of {cap} is reached",
                used=self.spent_tokens,
                cap=self.max_tokens,
            )
        return None

    def near_limit(self, ratio: float = 0.8) -> str | None:
        """Warn as a limit approaches, so the user has a chance to wind things down early."""
        # ``is not None`` rather than truthiness: limit=0 is the legitimate setting "stop
        # immediately", and a truthiness test reads it as "no limit set" — exceeded() honours it,
        # near_limit() does not, and one setting behaves two opposite ways.
        if self.max_wall_seconds is not None and self.elapsed >= self.max_wall_seconds * ratio:
            return t(
                "wall clock {used:.0f}s / {cap:.0f}s",
                used=self.elapsed,
                cap=self.max_wall_seconds,
            )
        if self.max_usd is not None and self.spent_usd >= self.max_usd * ratio:
            return t("${used:.2f} / ${cap:.2f} spent", used=self.spent_usd, cap=self.max_usd)
        if self.max_tokens is not None and self.spent_tokens >= self.max_tokens * ratio:
            return t("{used} / {cap} tokens used", used=self.spent_tokens, cap=self.max_tokens)
        return None

    def unenforceable(self) -> str | None:
        """Which limits **will not take effect even though they are configured**.

        Measured: the built-in adapters never invent pricing (``usd=None``), so ``spent_usd``
        stays 0 and ``max_usd`` never fires — while ``sesa.example.yaml`` carries
        ``max_usd: 2.0``. A limit that looks like it is in charge and is not is worse than
        not offering the setting at all.
        """
        if self.max_usd is None:
            return None
        if self.spent_usd > 0:
            return None
        if not (self.unknown_calls or self.priceless_calls):
            return None
        return t(
            "max_usd=${cap:.2f} is configured, but not one call so far has returned an "
            "amount (the built-in adapters do not invent pricing). **This limit will not "
            "take effect**; use max_tokens or max_wall_seconds instead.",
            cap=self.max_usd,
        )

    def caveat(self) -> str | None:
        """How much the cost figures can be trusted."""
        if self.unknown_calls:
            return t(
                "{n} calls returned no real usage (CLI adapters usually do not report "
                "token counts), so the cost figure is an undercount — go by the wall clock.",
                n=self.unknown_calls,
            )
        return None
