"""Credential resolution.

Two bottom lines:
* the key itself **never appears** in an error message
* a mistyped directive **is never sent off as a plaintext key**
"""

from __future__ import annotations

import pytest

from sesa.credentials import CredentialError, resolve_api_key
from sesa.types import ParticipantSpec

REAL_KEY = "sk-test-" + "x" * 40


def spec(pid: str = "p", **options) -> ParticipantSpec:
    return ParticipantSpec(id=pid, adapter="openai_compat", model="m", options=options)


def test_env_var_takes_priority(monkeypatch):
    monkeypatch.setenv("MY_KEY", REAL_KEY)
    assert resolve_api_key(spec(api_key_env="MY_KEY", api_key="ignored" * 5)) == REAL_KEY


def test_missing_env_var_names_it_without_leaking_anything(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    with pytest.raises(CredentialError, match="MY_KEY is not set"):
        resolve_api_key(spec(api_key_env="MY_KEY"))


def test_keyring_by_participant_id(monkeypatch):
    monkeypatch.setattr(
        "sesa.credentials._keyring_get", lambda a: REAL_KEY if a == "claude" else None
    )
    assert resolve_api_key(spec("claude", api_key="keyring")) == REAL_KEY


def test_shared_keyring_entry_serves_many_participants(monkeypatch):
    """One key serving several participants (the same model in different stances, say) need not be
    stored twice.
    """
    monkeypatch.setattr(
        "sesa.credentials._keyring_get", lambda a: REAL_KEY if a == "anthropic" else None
    )
    for pid in ("claude-conservative", "claude-radical"):
        assert resolve_api_key(spec(pid, api_key="keyring:anthropic")) == REAL_KEY


def test_missing_keyring_entry_names_the_account(monkeypatch):
    monkeypatch.setattr("sesa.credentials._keyring_get", lambda a: None)
    with pytest.raises(CredentialError, match="'anthropic'"):
        resolve_api_key(spec(api_key="keyring:anthropic"))


def test_empty_keyring_account_is_rejected():
    with pytest.raises(CredentialError, match="must be followed by a keyring entry name"):
        resolve_api_key(spec(api_key="keyring:"))


@pytest.mark.parametrize("bad", ["keryring", "keyrng", "env:MY_KEY", "vault:secret"])
def test_typoed_directive_is_never_sent_as_a_key(bad):
    """Sending a mistyped directive to a provider as a plaintext key is the worst way to fail."""
    with pytest.raises(CredentialError):
        resolve_api_key(spec(api_key=bad))


def test_suspiciously_short_plaintext_is_rejected():
    with pytest.raises(CredentialError, match="too short"):
        resolve_api_key(spec(api_key="short"))


def test_full_length_plaintext_still_works():
    assert resolve_api_key(spec(api_key=REAL_KEY)) == REAL_KEY


def test_falls_back_to_provider_env_hint(monkeypatch):
    """With nothing configured, guess the environment variable from base_url, so that working out
    of the box is possible.
    """
    monkeypatch.setenv("MOONSHOT_API_KEY", REAL_KEY)
    assert resolve_api_key(spec(base_url="https://api.moonshot.cn/v1")) == REAL_KEY


def test_no_credentials_at_all_tells_the_user_what_to_do(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(CredentialError, match="participants add"):
        resolve_api_key(spec(base_url="https://example.com/v1"))


def test_error_messages_never_contain_the_key(monkeypatch):
    monkeypatch.setattr("sesa.credentials._keyring_get", lambda a: None)
    for options in ({"api_key": "keyring"}, {"api_key_env": "NOPE_KEY"}):
        try:
            resolve_api_key(spec(**options))
        except CredentialError as exc:
            assert REAL_KEY not in str(exc)
