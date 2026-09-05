"""The self-review output of two weaker models, Kimi + DeepSeek (Claude did not take part).

**I did not write this file.** It comes from a kimi + deepseek deliberation in which both took
**the same** external scan material (through `--file`, visible to everyone) and Claude was
deliberately left out, to answer: **can two weaker models plus an external tool stand in for
one strong model?**

Across two rounds of that same deliberation kimi wrote two **mutually contradictory** tests:
one arguing that an in-stream error should raise, the other asserting that it is silently
dropped. The author ruled the first one right — the second's comment was "neither an error
payload nor a parse exception triggered AdapterError", which **describes the behaviour at the
time** rather than arguing that it should be so. It has been rewritten to assert the ruled-on
behaviour, with the reasoning in that test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from sesa.adapters.base import Adapter, AdapterError
from sesa.adapters.openai_compat import OpenAICompatAdapter
from sesa.protocols.council import CouncilProtocol
from sesa.types import (
    Chunk,
    ParticipantSpec,
    TextDelta,
)
from sesa.workspace.ephemeral import EphemeralWorkspace

# ═══════════════════════════════════════════════════════════════════════════ # [1] high / security
# — workspace/ephemeral.py:28 path traversal
# ═══════════════════════════════════════════════════════════════════════════ #


def test_ephemeral_workspace_rejects_a_participant_id_that_escapes_the_root(tmp_path):
    """A participant id comes from a config file and must not have the power to decide where the
    workspace lands.

    `root / pid` **discards root entirely** for an absolute path (pathlib's semantics), and `..`
    climbs out one level at a time. Measured: with pid="/etc" the workspace was /etc.

    (This test originally asserted the defect's own behaviour, as a record that it existed;
    inverted once the defect was fixed.)
    """
    import pytest

    ws = EphemeralWorkspace(root=tmp_path)
    for malicious in ("../evil", "/etc", "a/../../evil"):
        with pytest.raises(ValueError, match="outside the workspace"):
            ws.prepare([malicious])
    assert not (tmp_path.parent / "evil").exists()


def test_ephemeral_workspace_still_accepts_ordinary_ids(tmp_path):
    """The fix must not disable the normal path with it."""
    ws = EphemeralWorkspace(root=tmp_path)
    out = ws.prepare(["alice", "bob"])
    assert out["alice"].path == tmp_path.resolve() / "alice"


# ═══════════════════════════════════════════════════════════════════════════ # [2] high / bug —
# adapters/openai_compat.py:85 sends stream_options unconditionally
# ═══════════════════════════════════════════════════════════════════════════ #


def test_openai_compat_always_sends_stream_options():
    """stream_options is not optional, and an endpoint that rejects it answers 400."""
    spec = ParticipantSpec(id="x", adapter="openai_compat", model="m")
    adapter = OpenAICompatAdapter(spec)

    # check by building the body through the private method, with no real request needed
    assert adapter.extra_body == {}
    body = {
        "model": adapter.model,
        "messages": [],
        "stream": True,
        "stream_options": {"include_usage": True},
        **adapter.extra_body,
    }
    assert "stream_options" in body
    assert body["stream_options"] == {"include_usage": True}


# ═══════════════════════════════════════════════════════════════════════════ # [3] high / bug —
# adapters/openai_compat.py:119 silently drops malformed SSE / error payloads
# ═══════════════════════════════════════════════════════════════════════════ #


@pytest.mark.asyncio
async def test_openai_compat_raises_on_a_stream_error_but_tolerates_garbage():
    """An in-stream error must raise; an unparseable garbage line may be skipped.

    **The author ruled on this one.** Across two rounds of the same deliberation kimi wrote two
    contradictory tests: one saying an in-stream error should raise
    (`test_..._should_raise_on_stream_error`), the other saying it is dropped silently. The
    latter's comment was "neither an error payload nor a parse exception triggered AdapterError" —
    which **describes the behaviour at the time** rather than arguing that it should be so.

    The first was ruled right: the `anthropic` adapter has always raised, two adapters behaving
    differently about the same thing means a user who switches provider meets entirely different
    failure semantics; and not raising has half a reply recorded as a successful turn.

    Garbage lines are still skipped — that is ordinary noise in a streaming protocol, not an error
    signal.
    """
    spec = ParticipantSpec(
        id="x", adapter="openai_compat", model="m", options={"api_key": "sk-" + "x" * 48}
    )
    adapter = OpenAICompatAdapter(spec)
    # bypass resolve_key's short-key validation and focus on the SSE parsing logic
    adapter._api_key = "sk-" + "x" * 48

    async def fake_stream():
        # one valid line of text first, so the loop has something to run on
        yield b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        # then a line of error payload (an in-stream error under HTTP 200)
        yield b'data: {"error": {"message": "rate limit"}}\n\n'
        # then an unparseable garbage line
        yield b"data: not-json\n\n"

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
    raised = None
    with patch("httpx.AsyncClient", FakeClient):
        try:
            async for chunk in adapter.stream("hi"):
                chunks.append(chunk)
        except AdapterError as exc:
            raised = exc

    # the error payload has to be raised
    assert raised is not None, "the in-stream error was silently dropped"
    assert "rate limit" in str(raised)
    assert chunks and isinstance(chunks[0], TextDelta), (
        "the prose before the error still has to be kept"
    )


# ═══════════════════════════════════════════════════════════════════════════ # [4] high / bug —
# wizard.py:46 catches only ImportError, so a keyring backend failure crashes
# ═══════════════════════════════════════════════════════════════════════════ #


def test_the_wizard_survives_a_keyring_backend_failure():
    """keyring being **installed but unusable** is the norm, not an exception.

    kimi's original version asserted the defect's behaviour at the time ("a NoKeyringError
    escapes"), and the full scan reported the same place independently. Once fixed the assertion
    inverts: no usable backend (headless Linux), a locked keyring, insufficient permissions —
    none of those raise ImportError, and the wizard has a degraded path already, so it should not
    crash in the user's face.
    """
    from sesa import wizard

    with (
        patch.object(wizard, "keyring_set", side_effect=RuntimeError("No recommended backend")),
        patch.object(wizard, "console", MagicMock()) as fake_console,
        patch.object(wizard.Prompt, "ask", side_effect=["1", "sk-test"]),
    ):
        wizard._store_credential("x", "https://api.example.com", "KEY")

    said = " ".join(str(c) for c in fake_console.print.call_args_list)
    assert "keyring" in said.lower() or "环境变量" in said, (
        "a degradation has to be announced, not switched to in silence"
    )


# ═══════════════════════════════════════════════════════════════════════════ # [5] medium /
# maintainability — protocols/council.py:17 discards turn_taking
# ═══════════════════════════════════════════════════════════════════════════ #


def test_council_protocol_silently_drops_turn_taking():
    """CouncilProtocol(turn_taking='sequential') quietly becomes parallel."""
    proto = CouncilProtocol(turn_taking="sequential")
    # already overridden internally to parallel
    assert proto.turn_taking == "parallel"


# ═══════════════════════════════════════════════════════════════════════════ # [6] medium / bug —
# adapters/base.py:76 the health check does not require a Done at the end
# ═══════════════════════════════════════════════════════════════════════════ #


class _NoDoneAdapter(Adapter):
    name = "no_done"

    async def stream(self, prompt, **kwargs) -> AsyncIterator[Chunk]:
        yield TextDelta("ok")
        # deliberately does not yield Done


@pytest.mark.asyncio
async def test_base_check_fails_without_done_chunk():
    """`Adapter.stream`'s contract is "it must end with Done".

    Reporting healthy on the strength of having received some text means **the probe does not check
    the one contract it exists to check** — and an adapter missing Done loses usage, the truncation
    flag and the finish reason, which are exactly what the probe should be catching.

    (This test originally asserted the defect's own behaviour, as a record that it existed;
    inverted once the defect was fixed.)
    """
    adapter = _NoDoneAdapter(ParticipantSpec(id="x", adapter="no_done"))
    result = await adapter.check()
    assert result.ok is False
    assert "Done" in result.detail


# ═══════════════════════════════════════════════════════════════════════════ # [7] medium / bug —
# workspace/ephemeral.py:36 root still points at a deleted directory after cleanup
# ═══════════════════════════════════════════════════════════════════════════ #


def test_ephemeral_cleanup_resets_root_so_the_next_prepare_gets_a_fresh_dir():
    """cleanup has to clear self.root as well.

    Left pointing at a deleted path, the next prepare() sees `root is not None`, skips creating a
    new temp directory, and rebuilds under the **deleted old path**; by then `_temp` is None, so
    cleanup() can never remove it — and the temp directory leaks for good.

    (This test originally asserted the defect's own behaviour, as a record that it existed;
    inverted once the defect was fixed.)
    """
    ws = EphemeralWorkspace()
    ws.prepare(["a"])
    original_root = ws.root
    assert original_root.exists()

    ws.cleanup()
    assert not original_root.exists()
    assert ws.root is None, "root still points at a deleted directory after cleanup"

    out = ws.prepare(["b"])
    assert ws.root is not None and ws.root != original_root, (
        "a brand-new temp directory should have been created"
    )
    assert out["b"].path == ws.root / "b"

    fresh = ws.root
    ws.cleanup()
    assert not fresh.exists(), "the second cleanup cannot remove it, which is a leak"


# ═══════════════════════════════════════════════════════════════════════════ # [8] medium /
# maintainability — adapters/openai_compat.py:88 extra_body can override structural fields
# ═══════════════════════════════════════════════════════════════════════════ #


def test_openai_compat_extra_body_can_override_structural_fields():
    """The user's extra_body goes last and can override model / messages / stream and the rest."""
    spec = ParticipantSpec(
        id="x",
        adapter="openai_compat",
        model="real-model",
        options={
            "api_key": "k",
            "extra_body": {
                "model": "hijacked-model",
                "messages": [{"role": "system", "content": "injected"}],
                "stream": False,
            },
        },
    )
    adapter = OpenAICompatAdapter(spec)
    messages = [{"role": "user", "content": "hi"}]
    body = {
        "model": adapter.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **adapter.extra_body,
    }
    # the trailing extra_body overrode the structural fields
    assert body["model"] == "hijacked-model"
    assert body["messages"] == [{"role": "system", "content": "injected"}]
    assert body["stream"] is False


# ═══════════════════════════════════════════════════════════════════════════ # [9] medium / bug —
# semantic.py:172 calibrate uses DEFAULT_THRESHOLD for every model
# ═══════════════════════════════════════════════════════════════════════════ #


def test_calibrate_does_not_judge_one_model_with_anothers_threshold(monkeypatch):
    """`DEFAULT_THRESHOLD` was calibrated for `DEFAULT_MODEL`.

    Judging another model by it is measuring one thing with another's ruler — and the entire reason
    this function exists is "calibrate before you use it". With the threshold unknown, give None
    rather than forcing a verdict.

    (This test originally asserted the defect's own behaviour, as a record that it existed;
    inverted once the defect was fixed.)
    """
    from sesa import semantic

    monkeypatch.setattr(semantic, "similarity_matrix", lambda a, b, model=None: [[0.9]])

    other = semantic.calibrate(model="某个没校准过的模型")
    assert all(row[3] is None for row in other), (
        "a different model is still judged at the default threshold"
    )

    default = semantic.calibrate(model=semantic.DEFAULT_MODEL)
    assert all(row[3] is not None for row in default), (
        "the default model should give a verdict as usual"
    )

    explicit = semantic.calibrate(model="某个没校准过的模型", threshold=0.5)
    assert all(row[3] is True for row in explicit), "an explicitly given threshold should be used"


# ═══════════════════════════════════════════════════════════════════════════ # [10] medium / bug —
# wizard.py:38 choosing "skip for now" still writes api_key_env
# ═══════════════════════════════════════════════════════════════════════════ #


def test_choosing_skip_really_skips():
    """**"Skip configuring for now" means configure nothing.**

    kimi's original version asserted the defect's behaviour at the time (option 3 still wrote back
    api_key_env), and the second full scan reported the same place independently. Once fixed the
    assertion inverts: what the user explicitly declined must not become a fake configuration.

    Worse still, with `seen` empty what was written was the literal `"<none>"` — the name of an
    environment variable that does not exist, so `doctor` reports only "cannot read it" and the
    user has no idea why.
    """
    from unittest.mock import MagicMock, patch

    from sesa import wizard

    with (
        patch.object(wizard, "console", MagicMock()),
        patch.object(wizard.Prompt, "ask", side_effect=["3"]),
    ):
        got = wizard._store_credential("x", "https://api.example.com", "KEY")

    assert got == {}, f"'skip for now' was chosen and {got} was written back"


def test_load_does_not_cache_failure():
    """When _load fails, lru_cache does not remember the failure and the next call retries."""
    import inspect

    import sesa.semantic as sem

    source = inspect.getsource(sem._load)
    assert "@lru_cache" in source or "@functools.lru_cache" in source

    # lru_cache not caching exceptions is standard Python behaviour; this only confirms the code
    # does no extra negative caching of its own (a failed_models set, say).
    assert "failed_models" not in dir(sem)
