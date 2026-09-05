"""The CLI adapter's behavioural contract.

Every case here uses real subprocesses and no mocks — because this adapter's whole value lies
in "can it reliably drive an external CLI we do not control".
"""

from __future__ import annotations

import inspect
import sys
import time

import pytest

from sesa.adapters import build
from sesa.adapters.base import AdapterError
from sesa.types import Done, ParticipantSpec, TextDelta


async def collect(spec: ParticipantSpec, prompt: str = "hello world") -> tuple[str, object]:
    text, usage = [], None
    async for chunk in build(spec).stream(prompt):
        if isinstance(chunk, TextDelta):
            text.append(chunk.text)
        elif isinstance(chunk, Done):
            usage = chunk.usage
    return "".join(text), usage


def spec(**options) -> ParticipantSpec:
    return ParticipantSpec(id=options.pop("id", "p"), adapter="cli", options=options)


async def test_stdin_raw_roundtrip():
    text, usage = await collect(spec(command=["cat"], prompt="stdin"))
    assert text == "hello world"
    # a CLI cannot report token counts — record honestly as unknown, never invent
    assert usage.known is False


async def test_argv_mode():
    text, _ = await collect(spec(command=["echo"], prompt="argv"))
    assert text.strip() == "hello world"


async def test_argv_template_mode():
    text, _ = await collect(
        spec(
            command=["python3", "-c", "print({prompt!r})".replace("{prompt!r}", "'{prompt}'")],
            prompt="argv_template",
        ),
    )
    assert "hello world" in text


async def test_jsonl_parse_with_extract_path():
    text, _ = await collect(
        spec(
            command=[
                "python3",
                "-c",
                "import json,sys\n"
                "for w in sys.stdin.read().split():\n"
                "    print(json.dumps({'message': {'text': w}}))",
            ],
            prompt="stdin",
            parse="jsonl",
            extract="message.text",
        )
    )
    assert text == "helloworld"


async def test_non_json_line_passes_through():
    """A line that is not JSON is emitted verbatim, which beats dropping it in silence."""
    text, _ = await collect(
        spec(
            command=["python3", "-c", "print('plain noise')"],
            prompt="stdin",
            parse="jsonl",
            extract="message.text",
        )
    )
    assert "plain noise" in text


async def test_nonzero_exit_raises_with_stderr_tail():
    with pytest.raises(AdapterError) as err:
        await collect(
            spec(
                command=[
                    "python3",
                    "-c",
                    "import sys; sys.stderr.write('Not logged in\\n'); sys.exit(1)",
                ],
                prompt="stdin",
            )
        )
    assert "exit code 1" in str(err.value)
    assert "Not logged in" in str(err.value)  # diagnostic information has to come through


async def test_timeout_kills_process():
    with pytest.raises(AdapterError, match="raise"):
        await collect(
            spec(
                command=["python3", "-c", "import time; time.sleep(10)"], prompt="stdin", timeout=1
            )
        )


async def test_missing_command_is_reported_clearly():
    with pytest.raises(AdapterError, match="not on PATH"):
        await collect(spec(command=["definitely-not-a-real-cmd-xyz"]))


async def test_process_ignoring_stdin_does_not_leak_broken_pipe():
    """Some CLIs never read stdin at all, or exit immediately on error — a BrokenPipeError must not
    run loose.
    """
    text, _ = await collect(
        spec(command=["python3", "-c", "print('bye')"], prompt="stdin"), "x" * 200_000
    )
    assert text.strip() == "bye"


async def test_process_ignoring_stdin_still_reports_real_failure():
    """A failed write to stdin must not drown out the process's real cause of failure."""
    with pytest.raises(AdapterError) as err:
        await collect(
            spec(
                command=["python3", "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"],
                prompt="stdin",
            ),
            "x" * 200_000,
        )
    assert "exit code 2" in str(err.value) and "boom" in str(err.value)


# --------------------------------------------------------------------------- # The artefact-file
# channel
# Rather than hoping an agent CLI obeys an stdout format strictly, agree on an artefact path and
# read only the file. What this removes is not only parse failures but instruction drift: commentary
# smuggled in, the artefact written as a natural-language patch, tool logs mixed in. (The design
# came from a minority opinion kimi raised in a real deliberation.)
# --------------------------------------------------------------------------- #


async def test_artifact_file_is_the_source_of_truth_not_stdout(tmp_path):
    writer = (
        "import sys, pathlib\n"
        "sys.stdin.read()\n"
        "print('这些是日志噪音，不该被当成发言')\n"
        f"pathlib.Path({str(tmp_path / 'out.md')!r}).write_text('这才是正式发言', encoding='utf-8')\n"
    )
    text, _ = await collect(
        spec(command=["python3", "-c", writer], prompt="stdin", artifact=str(tmp_path / "out.md"))
    )
    assert text == "这才是正式发言"
    assert "日志噪音" not in text


async def test_artifact_path_templates_are_rendered(tmp_path):
    adapter = build(
        spec(id="claude", command=["true"], artifact="{workspace}/turns/{participant}-r{round}.md")
    )
    path = adapter._artifact_path(tmp_path, {"round": "3"})
    assert path == tmp_path / "turns" / "claude-r3.md"


async def test_stale_artifact_from_a_previous_round_is_removed(tmp_path):
    """A file of the same name left over from the previous round, if read, becomes a turn attributed
    to the wrong round.
    """
    stale = tmp_path / "out.md"
    stale.write_text("上一轮的旧发言", encoding="utf-8")
    with pytest.raises(AdapterError, match="the artefact file"):
        await collect(
            spec(
                command=["python3", "-c", "import sys; sys.stdin.read()"],
                prompt="stdin",
                artifact=str(stale),
            )
        )
    assert not stale.exists()


async def test_crashing_process_reports_the_exit_code_not_the_missing_artifact(tmp_path):
    """If the process really died, say the process died — that is the more precise root cause."""
    crash = "import sys; sys.stdin.read(); sys.stderr.write('Not logged in\\n'); sys.exit(1)"
    with pytest.raises(AdapterError) as err:
        await collect(
            spec(
                command=["python3", "-c", crash], prompt="stdin", artifact=str(tmp_path / "out.md")
            )
        )
    assert "exit code 1" in str(err.value)
    assert "Not logged in" in str(err.value)  # diagnostic information has to come through


async def test_silent_success_without_artifact_fails_loudly(tmp_path):
    """A process that "succeeds" and writes nothing is the most insidious case — silent unreliability
    is exactly what this feature exists to eliminate.
    """
    quiet = (
        "import sys; sys.stdin.read(); "
        "sys.stderr.write('warning: 我把内容打到 stdout 了\\n'); print('漂移的产物')"
    )
    with pytest.raises(AdapterError) as err:
        await collect(
            spec(
                command=["python3", "-c", quiet], prompt="stdin", artifact=str(tmp_path / "out.md")
            )
        )
    assert "the artefact file" in str(err.value)
    assert "我把内容打到 stdout" in str(err.value)  # makes instruction drift easier to diagnose


async def test_artifact_instruction_is_appended_to_the_prompt(tmp_path):
    """The participant has to know where to write — the instruction is appended by the adapter
    itself, with no work for the engine.
    """
    echo = (
        "import sys, pathlib\n"
        "p = sys.stdin.read()\n"
        f"pathlib.Path({str(tmp_path / 'out.md')!r}).write_text(p, encoding='utf-8')\n"
    )
    text, _ = await collect(
        spec(command=["python3", "-c", echo], prompt="stdin", artifact=str(tmp_path / "out.md"))
    )
    assert "write your full turn to the file" in text
    assert "stdout is treated as a log" in text


async def test_artifact_can_be_optional(tmp_path):
    text, _ = await collect(
        spec(
            command=["python3", "-c", "import sys; sys.stdin.read(); print('回退到 stdout')"],
            prompt="stdin",
            artifact=str(tmp_path / "out.md"),
            artifact_required=False,
        )
    )
    assert text == ""  # an empty artefact is empty and stdout is still only a log — but it
    # is no longer a hard failure


# --------------------------------------------------------------------------- # Capturing the
# reasoning
# Keeping the reasoning and the prose as two kinds of chunk is what makes "share the thinking or
# not" a real switch, instead of a convention in the prompt. By default it does not enter other
# people's context (DESIGN.md §4.6).
# --------------------------------------------------------------------------- #


async def test_thinking_is_a_separate_chunk_kind():
    from sesa.types import ThinkingDelta

    assert ThinkingDelta("推理草稿").text == "推理草稿"


async def test_failure_reports_stdout_when_stderr_is_empty(tmp_path):
    """Plenty of agent CLIs write the reason for failure to stdout.

    Measured: claude's "You've hit your session limit · resets 4:20am" appears only in stdout,
    while the engine reported "exit code 1 (stderr empty)" — 8 deliberations were wasted for this
    reason, and the event stream gave no sign that it was a quota problem. If the answer is in
    hand, hand it over.
    """
    script = tmp_path / "quota.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        'print("You\'ve hit your session limit · resets 4:20am")\n'
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    spec = ParticipantSpec(
        id="claude",
        adapter="cli",
        options={"command": [sys.executable, str(script)], "prompt": "stdin"},
    )

    with pytest.raises(AdapterError, match="session limit"):
        await collect(spec)


async def test_stdout_tail_does_not_leak_across_rounds(tmp_path):
    """The adapter is reused across rounds, and the previous round's output must not get into this
    round's error message.
    """
    marker = tmp_path / "ran-once"
    script = tmp_path / "flaky.py"
    script.write_text(
        "import pathlib, sys\n"
        "sys.stdin.read()\n"
        f"m = pathlib.Path({str(marker)!r})\n"
        "if m.exists():\n"
        "    sys.exit(1)\n"
        "m.touch()\n"
        "print('第一轮说了很多话')\n",
        encoding="utf-8",
    )
    adapter = build(
        ParticipantSpec(
            id="claude",
            adapter="cli",
            options={"command": [sys.executable, str(script)], "prompt": "stdin"},
        )
    )

    async for _ in adapter.stream("第一轮"):
        pass

    with pytest.raises(AdapterError) as caught:
        async for _ in adapter.stream("第二轮"):
            pass

    assert "第一轮说了很多话" not in str(caught.value)
    assert "both stdout and stderr were empty" in str(caught.value)


async def test_a_slow_but_talking_participant_is_not_killed(tmp_path):
    """**"Slow but working" and "stuck" are two different things.**

    A total-time timeout cannot tell them apart: one participant worked for 900 seconds, was
    killed, and was recorded as contributing 0 characters — while its working copy did in fact
    hold output. An agent with tools taking ten-odd minutes on one round is entirely normal, as
    long as it keeps producing.
    """
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "for i in range(6):\n"
        "    print(f'第 {i} 步', flush=True)\n"
        "    time.sleep(0.4)\n",
        encoding="utf-8",
    )
    spec = ParticipantSpec(
        id="slow",
        adapter="cli",
        options={
            "command": [sys.executable, str(script)],
            "prompt": "stdin",
            "timeout": 30,
            "idle_timeout": 2,  # 0.4s per step, far faster than the idle cap
        },
    )

    text, _ = await collect(spec)

    assert "第 5 步" in text, "a process that keeps producing output must not be killed"


async def test_a_participant_that_stops_talking_is_killed_early(tmp_path):
    """The counterexample: it says a few things and stops, and the idle timeout has to reap it before
    the total time does.
    """
    script = tmp_path / "stall.py"
    script.write_text(
        "import sys, time\nsys.stdin.read()\nprint('开了个头', flush=True)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    spec = ParticipantSpec(
        id="stall",
        adapter="cli",
        options={
            "command": [sys.executable, str(script)],
            "prompt": "stdin",
            "timeout": 60,
            "idle_timeout": 1,
        },
    )

    started = time.monotonic()
    with pytest.raises(AdapterError, match="no new output for"):
        await collect(spec)

    assert time.monotonic() - started < 20, (
        "it should be reaped after 1s idle, not after the full 60s"
    )


async def test_a_process_that_never_speaks_gets_the_full_total_timeout(tmp_path):
    """**The test is not "how long since it spoke" but "has it spoken at all".**

    Some CLIs buffer throughout and emit everything at the end (`kimi --quiet` does exactly that).
    For them "idle" and "working" are observationally identical — applying an idle timeout kills
    them on **total** elapsed time. Measured: kimi takes 660 seconds to finish a round normally,
    and with a 300s idle default it would be killed by mistake at 300s.

    So before the first chunk arrives, only the total time applies. When slow and dead cannot be
    told apart, **it is better to wait out the total time than to kill a participant that is still
    working.**
    """
    script = tmp_path / "buffered.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(1.5)\n"  # silent throughout, far beyond idle_timeout
        "print('憋到最后才说话', flush=True)\n",
        encoding="utf-8",
    )
    spec = ParticipantSpec(
        id="buffered",
        adapter="cli",
        options={
            "command": [sys.executable, str(script)],
            "prompt": "stdin",
            "timeout": 30,
            "idle_timeout": 0.3,  # far shorter than its silence
        },
    )

    text, _ = await collect(spec)

    assert "憋到最后才说话" in text, (
        "a process that never spoke must not be killed by the idle timeout"
    )


#: the per-round duration measured today, as the anchor for the default. `kimi --quiet` buffers
#: throughout and emits at the end, taking 660–900 seconds to finish a round normally.
SLOWEST_REAL_TURN_SECONDS = 900


def test_the_defaults_do_not_kill_a_participant_we_have_actually_used():
    """The default has to cover the slowest participant **actually in use**.

    The old default was timeout=600, while `kimi --quiet` takes 660–900 seconds to finish a round
    normally — an agent CLI I use myself would be killed under the default configuration, and the
    error the user gets is "judged hung". Someone wiring up a slower model only fares worse.

    Setting it high is safe: what really constrains cost is the run-wide wall-clock budget (the
    engine takes the smaller of the two), and this only asks "has this process hung". Whereas
    killing by mistake voids a whole turn.
    """
    from sesa.adapters.cli import CliAdapter

    adapter = CliAdapter(ParticipantSpec(id="x", adapter="cli", options={"command": ["true"]}))

    assert adapter.default_timeout > SLOWEST_REAL_TURN_SECONDS, (
        f"the total defaults to {adapter.default_timeout:.0f}s, which does not cover the slowest measured "
        f"{SLOWEST_REAL_TURN_SECONDS}s — it would kill a participant that is working normally"
    )
    assert adapter.idle_timeout >= 600, (
        "the idle cap needs room too: an agent goes quiet for a long time while running a slow tool"
    )
    assert adapter.idle_timeout < adapter.default_timeout, (
        "the idle cap should be smaller than the total, or it is worthless"
    )


def test_every_adapter_agrees_on_the_backstop():
    """The three adapters' fallback values have to agree, or switching provider switches behaviour."""
    from sesa.adapters.anthropic import AnthropicAdapter
    from sesa.adapters.openai_compat import OpenAICompatAdapter
    from sesa.config import Config

    assert Config().turn_timeout > SLOWEST_REAL_TURN_SECONDS
    for adapter_cls in (OpenAICompatAdapter, AnthropicAdapter):
        default = inspect.signature(adapter_cls.stream).parameters["timeout"].default
        assert default > SLOWEST_REAL_TURN_SECONDS, (
            f"{adapter_cls.__name__}'s fallback is too small"
        )


async def test_a_total_timeout_does_not_claim_the_process_was_stuck(tmp_path):
    """ "Judged hung" is very likely a lie.

    Measured: kimi takes 660–900 seconds to finish a round normally while the default timeout was
    600 — a participant still working gets killed and the user is told "hung", so they go looking
    for why it hung, when the truth is **the cap was set too small in the first place**.
    """
    script = tmp_path / "slowpoke.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    spec = ParticipantSpec(
        id="slowpoke",
        adapter="cli",
        options={"command": [sys.executable, str(script)], "prompt": "stdin", "timeout": 1},
    )

    with pytest.raises(AdapterError) as caught:
        await collect(spec)

    message = str(caught.value)
    assert "may simply be slow" in message
    assert "cannot be told apart" in message, (
        "with no output at all it has to say slow and dead cannot be told apart"
    )
    assert "timeout" in message, "it has to say which parameter to adjust"
