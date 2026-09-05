import pytest

from sesa.adapters import available, build, register
from sesa.adapters.base import Adapter
from sesa.types import ParticipantSpec


def test_three_builtin_adapters():
    assert available() == ["anthropic", "cli", "openai_compat"]


def test_unknown_adapter_lists_alternatives():
    with pytest.raises(ValueError, match="openai_compat"):
        build(ParticipantSpec(id="x", adapter="nope"))


def test_third_party_adapter_can_be_registered():
    """A third party injects a custom adapter without changing this package."""

    class Dummy(Adapter):
        name = "dummy"

        def stream(self, prompt, **kw):  # pragma: no cover - it need not actually run
            raise NotImplementedError

    register(Dummy)
    assert isinstance(build(ParticipantSpec(id="d", adapter="dummy")), Dummy)


def test_cli_adapter_requires_command():
    with pytest.raises(ValueError, match="requires a command"):
        build(ParticipantSpec(id="x", adapter="cli"))


def test_api_adapters_require_model():
    with pytest.raises(ValueError, match="requires a model"):
        build(ParticipantSpec(id="x", adapter="openai_compat"))
    with pytest.raises(ValueError, match="requires a model"):
        build(ParticipantSpec(id="x", adapter="anthropic"))
