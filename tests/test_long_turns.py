"""A turn that is still producing output should not be killed for "taking long".

The user's question: "each round has a time limit now — what about a complex task where one
round takes a very long time?"

They were right. `limit = min(idle_timeout, remaining total)` used to make the total a hard
cap, so a process **visibly producing steady output** was cut off at 30 minutes — when
being visibly at work is the direct evidence that it has not hung.

The division of labour after the fix:

* **Before it speaks** — the total time only. Slow and dead really are observationally
  identical here (a fully-buffering CLI can stay silent for 1100s), and only this can catch
  it.
* **After it speaks** — idle_timeout only. "Went quiet in mid-sentence" is what hung looks
  like; as long as it keeps talking, let it talk.
"""

from __future__ import annotations

import sys
import time

import pytest

from sesa.adapters.cli import CliAdapter
from sesa.types import ParticipantSpec, TextDelta


def _adapter(script: str, *, timeout: float, idle: float) -> CliAdapter:
    return CliAdapter(
        ParticipantSpec(
            id="x",
            adapter="cli",
            options={
                "command": [sys.executable, script],
                "prompt": "stdin",
                "timeout": timeout,
                "idle_timeout": idle,
            },
        )
    )


@pytest.mark.asyncio
async def test_a_steadily_talking_turn_outlives_the_total_timeout(tmp_path):
    """A total of 1s, actually running 3s, producing output throughout — **it must not be killed**."""
    script = tmp_path / "chatty.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "for i in range(6):\n"
        "    print(f'第 {i} 段', flush=True)\n"
        "    time.sleep(0.5)\n",
        encoding="utf-8",
    )
    text = []
    async for chunk in _adapter(str(script), timeout=1, idle=5).stream("hi"):
        if isinstance(chunk, TextDelta):
            text.append(chunk.text)
    assert "第 5 段" in "".join(text), (
        "the output was cut off — the total time is still acting as a hard cap"
    )


@pytest.mark.asyncio
async def test_going_silent_mid_sentence_is_still_caught(tmp_path):
    """Going quiet in mid-sentence is what hung looks like — that gate has to remain."""
    script = tmp_path / "stalls.py"
    script.write_text(
        "import sys, time\nsys.stdin.read()\nprint('开了个头', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    started = time.time()
    with pytest.raises(Exception) as caught:
        async for _ in _adapter(str(script), timeout=60, idle=2).stream("hi"):
            pass
    assert "no new output" in str(caught.value), (
        f"the error states the wrong reason: {caught.value}"
    )
    assert time.time() - started < 20, "idle_timeout should have reaped it in time"


@pytest.mark.asyncio
async def test_never_speaking_is_still_bounded_by_the_total_timeout(tmp_path):
    """Having never spoken, slow and dead cannot be told apart — only the total time can catch it."""
    script = tmp_path / "mute.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n", encoding="utf-8")
    started = time.time()
    with pytest.raises(Exception) as caught:
        async for _ in _adapter(str(script), timeout=2, idle=60).stream("hi"):
            pass
    message = str(caught.value)
    assert "total time exceeded" in message
    assert "may simply be slow rather than stuck" in message, (
        "'slow' must not be asserted as 'stuck'"
    )
    assert time.time() - started < 20


@pytest.mark.asyncio
async def test_the_two_failures_do_not_get_confused(tmp_path):
    """The two timeouts must give different errors — getting it wrong sends the user to adjust a
    parameter that does nothing.
    """
    chatty = tmp_path / "stalls.py"
    chatty.write_text(
        "import sys, time\nsys.stdin.read()\nprint('x', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    mute = tmp_path / "mute.py"
    mute.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")

    async def message_of(script: str, timeout: float, idle: float) -> str:
        try:
            async for _ in _adapter(script, timeout=timeout, idle=idle).stream("hi"):
                pass
        except Exception as exc:
            return str(exc)
        return ""

    stalled = await message_of(str(chatty), timeout=30, idle=1)
    silent = await message_of(str(mute), timeout=1, idle=30)
    assert "idle_timeout" in stalled
    assert "total time exceeded" in silent
    assert stalled != silent
