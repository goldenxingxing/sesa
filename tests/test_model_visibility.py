"""Which model a participant is actually running has to be visible.

How it started: in the user's participant table, the "model" column was `—` for all three
CLIs. And one of them, `dsh` (DeepSeek Harness), was in fact running **Kimi K3** — their dsh
patch said `provider: kimi-code`. So two of the three participants were the same brain,
while this product's entire information content rests on "the brains differ".

**That dash hid the single most important thing, and hid it for a long time.**
"""

from __future__ import annotations

import pytest

from sesa.adapters.base import _reported_model
from sesa.config import declared_model
from sesa.types import ParticipantSpec

# ── say plainly whatever the configuration can determine ────────────────────────── #


def test_an_api_participants_model_comes_from_the_config():
    spec = ParticipantSpec(id="a", adapter="openai_compat", model="deepseek-chat")
    assert declared_model(spec) == "deepseek-chat"


def test_an_explicit_model_flag_on_a_cli_is_authoritative():
    """An explicit --model on the command line is what we passed in, and is authoritative."""
    for flag in ("--model", "-m"):
        spec = ParticipantSpec(
            id="k", adapter="cli", options={"command": ["kimi", flag, "kimi-k2.6"]}
        )
        assert declared_model(spec) == "kimi-k2.6"


def test_a_cli_without_a_model_flag_returns_none_not_a_dash():
    """**Do not return "—".**

    That dash could mean "not configured" or "configured but unknown to us", and the two mean
    entirely different things to the user. Return None and let the caller decide how to say it.
    """
    spec = ParticipantSpec(id="d", adapter="cli", options={"command": ["dsh", "--profile", "x"]})
    assert declared_model(spec) is None


def test_a_dangling_model_flag_does_not_crash():
    spec = ParticipantSpec(id="k", adapter="cli", options={"command": ["kimi", "--model"]})
    assert declared_model(spec) is None


# ── the self-reported model: useful, but not fact ───────────────────────────────── #


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("MODEL=deepseek-v4-flash-vision-exp", "deepseek-v4-flash-vision-exp"),
        ('好的。\nMODEL="kimi-k2.6".', "kimi-k2.6"),
        ("MODEL = gpt-x", "gpt-x"),
    ],
)
def test_the_self_reported_model_is_extracted(reply, expected):
    assert _reported_model(reply) == expected


@pytest.mark.parametrize("reply", ["我是一个AI助手，很高兴为你服务", "", "ok", "model unknown"])
def test_an_answer_that_does_not_name_a_model_yields_none(reply):
    """**Never pass the whole reply off as a model name.**

    Plenty of CLIs answer with a paragraph of pleasantries, and putting that in the "model"
    column has the reader take it for fact.
    """
    assert _reported_model(reply) is None


def test_doctor_shows_the_two_sources_separately():
    """What the config says and what it says about itself are two things, in two columns.

    A mismatch has to be flagged on the spot — that is the signal for "you think you are using A
    and B is running", and it is exactly the hole this user fell into.
    """
    import inspect

    from sesa import cli

    source = inspect.getsource(cli.doctor)
    assert "model (configured)" in source and "model (self-reported)" in source
    assert "mismatch" in source


def test_the_self_report_is_labelled_as_self_reported():
    """Models often get their own identity wrong — especially when wrapped or routed to another
    backend. Useful, but it has to be labelled as self-reported.
    """
    from sesa.adapters.base import CheckResult

    assert CheckResult.__doc__
    field_doc = inspect_field_doc()
    assert "self-reported" in field_doc and "not fact" in field_doc


def inspect_field_doc() -> str:
    import inspect

    from sesa.adapters import base

    return inspect.getsource(base)
