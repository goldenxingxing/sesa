"""Diagnostic information must not be dropped along the way.

What triggered it: the user's `dsh` would not start, and the wizard showed one line,
"AdapterError: dsh: exit code 1". While dsh's own error said it plainly —
`The requested module 'node:zlib' does not provide an export named
'createZstdDecompress'` — meaning the Node version was too old (v22.14 < 22.15).

**The answer was in hand all along, and two rendering steps each dropped half of it.**
"""

from __future__ import annotations

from sesa.adapters.cli import _both_ends

# ── taking only the tail loses the cause at the head ────────────────────────────── #


def test_a_long_output_keeps_both_ends():
    """Which end holds the answer cannot be known in advance:

    * claude's `You've hit your session limit` is at **the tail**
    * the cause of a Node crash is at **the head**, with the tail all stack frames

    Since it cannot be known, give both.
    """
    head = "Error: 真正的病因在这里"
    tail = "at async ModuleJob.run"
    text = head + ("\n    at 无关的堆栈帧" * 200) + "\n" + tail

    got = _both_ends(text)
    assert head in got, "the cause at the head was cut off"
    assert tail in got, "the tail has to be kept too"
    assert "omitted in the middle" in got, "it has to say how much was omitted"
    assert len(got) < len(text)


def test_a_short_output_is_returned_whole():
    assert _both_ends("退出码 1，就这么多") == "退出码 1，就这么多"


# ── printing only the first line loses the whole reason ─────────────────────────── #


def test_the_wizard_prints_every_line_of_a_failure(capsys):
    """The CLI adapter's message has the form `dsh: exit code 1\n<the real reason>`.

    Printing only the first line leaves the user with "exit code 1" — while in hand is a
    message that explains the cause exactly. This is the same thing as the adapter's "reporting
    only the exit code throws away the answer we are holding", committed one layer up, in the
    rendering.
    """
    import sesa.adapters
    from sesa import wizard
    from sesa.types import ParticipantSpec

    class _Boom:
        async def check(self):
            raise RuntimeError("dsh: 退出码 1\n真正的原因在第二行\n第三行也有用")

    original = sesa.adapters.build
    sesa.adapters.build = lambda spec: _Boom()
    try:
        broken = wizard._verify([ParticipantSpec(id="dsh", adapter="cli")])
    finally:
        sesa.adapters.build = original

    assert broken == ["dsh"]
    printed = capsys.readouterr().out
    assert "真正的原因在第二行" in printed, "the second line was dropped"
    assert "第三行也有用" in printed


def test_a_failure_with_no_detail_says_so_rather_than_printing_nothing(capsys):
    """With an empty detail, say plainly "no reason was left behind" — a bare ✗ makes people think
    the tool failed to print it, rather than that the other side really said nothing.
    """
    import sesa.adapters
    from sesa import wizard
    from sesa.adapters.base import CheckResult
    from sesa.types import ParticipantSpec

    class _Silent:
        async def check(self):
            return CheckResult(False, "")

    original = sesa.adapters.build
    sesa.adapters.build = lambda spec: _Silent()
    try:
        wizard._verify([ParticipantSpec(id="quiet", adapter="cli")])
    finally:
        sesa.adapters.build = original

    # The interface defaults to English; the Chinese path is covered by test_i18n's catalogue
    # completeness test.
    assert "no reason was given" in capsys.readouterr().out
