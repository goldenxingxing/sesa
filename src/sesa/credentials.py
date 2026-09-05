"""Credential resolution.

**By default a key is never written into a config file in plaintext.** Three sources,
in priority order:

1. ``api_key_env: KIMI_API_KEY``  — an environment variable, suited to CI
2. ``api_key: keyring``           — the system keyring (macOS Keychain / Windows
   Credential Manager / Secret Service), the default in ``sesa init``
3. ``api_key: sk-...``            — plaintext, allowed only after the wizard warns
"""

from __future__ import annotations

import os
import re

from ._install import install_hint
from .i18n import t
from .types import ParticipantSpec

SERVICE = "sesa"

#: a value shaped like `xxx:` is almost certainly a mistyped directive rather than a key
_DIRECTIVE_LIKE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*:")


def _redact(value: str) -> str:
    """A form for error messages: recognisable, but leaking nothing.

    The class docstring promises "the key itself is never echoed", and echoing here rests on
    "I judge that this is not a key" — **and the judgement can be wrong**: ``_DIRECTIVE_LIKE``
    matches ``word:``, and some providers' keys are exactly of the form ``user:token``, which
    would be printed into the error in full. An absolute promise cannot be kept by a
    judgement that can be wrong.
    """
    # **A short value is not echoed in full either.** It used to repr anything ≤6 characters
    # verbatim, on the grounds that "nothing that short can be a key" — but this function is called
    # redact, and its promise is "nothing leaks", not "nothing leaks when I think it is safe". The
    # judgement can be wrong (a truncated key, a short passphrase, a provider's short token), and
    # being wrong means writing a credential into a log. Report the length only.
    if len(value) > 6:
        return t("{head}…({n} characters in total)", head=repr(value[:6]), n=len(value))
    return t("({n} characters, redacted)", n=len(value))


#: a string shorter than this cannot be a real key
_MIN_KEY_LENGTH = 16

#: the usual environment variable names for common providers; the wizard and the resolver both try
#: them
COMMON_ENV_HINTS = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "moonshot": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    "kimi": ["KIMI_API_KEY", "MOONSHOT_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
}


class CredentialError(RuntimeError):
    """A credential is missing or unreadable. The message never echoes the key itself."""


def _keyring_get(participant_id: str) -> str | None:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - depends on which extra is installed
        raise CredentialError(
            t(
                "The configuration asks for the system keyring, but keyring is not "
                "installed. Run `{hint}`, or switch to api_key_env.",
                hint=install_hint("keyring"),
            )
        ) from exc
    return keyring.get_password(SERVICE, participant_id)


def keyring_set(participant_id: str, api_key: str) -> None:
    """For the wizard: store a key in the system keyring."""
    import keyring

    keyring.set_password(SERVICE, participant_id, api_key)


def keyring_has(participant_id: str) -> bool:
    """Whether the keyring holds a credential for this participant.

    **Any exception counts as "no".** This decides only whether to ask the user about
    reusing it — being wrong costs one extra question, whereas raising would abort the whole
    wizard.
    """
    try:
        import keyring
    except ImportError:
        return False
    try:
        return bool(keyring.get_password(SERVICE, participant_id))
    except Exception:
        return False


def keyring_delete(participant_id: str) -> bool:
    """Delete a credential from the system keyring. Returns whether one was really deleted.

    **It must not be reported as deleted regardless.** This used to swallow every exception
    (keyring not installed at all, the backend refusing access), leaving the caller with no
    signal: the user believed the credential was cleared while it was still in the keyring.
    With credentials, "I thought it was deleted" is more dangerous than "I know it was not".
    """
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.delete_password(SERVICE, participant_id)
    except Exception:
        # A missing password also raises (PasswordDeleteError) — and there "deletion failed" and
        # "there was nothing there" have the same outcome, so both return False and the caller goes
        # and checks.
        return False
    return True


def env_hints_for(base_url: str | None) -> list[str]:
    """Guess the likely environment variable names from base_url, for the wizard to suggest."""
    if not base_url:
        return []
    for needle, names in COMMON_ENV_HINTS.items():
        if needle in base_url:
            return names
    return []


def resolve_api_key(spec: ParticipantSpec) -> str:
    """Fetch the API key in priority order; raise an error **containing none of the key** when
    there is none.
    """
    opts = spec.options

    if env_name := opts.get("api_key_env"):
        value = os.environ.get(str(env_name))
        if value:
            return value
        raise CredentialError(
            t(
                "Participant {pid}: environment variable {env} is not set. Run "
                "`export {env}=...`, or use `sesa participants add` to put it in the keyring.",
                pid=spec.id,
                env=env_name,
            )
        )

    raw = opts.get("api_key")
    if isinstance(raw, str) and (raw == "keyring" or raw.startswith("keyring:")):
        # `keyring` looks the entry up by participant id; `keyring:<name>` takes a shared entry — so
        # one key serving several participants (the same model in different stances, say) need not
        # be stored twice.
        account = raw.split(":", 1)[1].strip() if ":" in raw else spec.id
        if not account:
            raise CredentialError(
                t(
                    "Participant {pid}: `keyring:` must be followed by a keyring entry name",
                    pid=spec.id,
                )
            )
        value = _keyring_get(account)
        if value:
            return value
        raise CredentialError(
            t(
                "Participant {pid}: the system keyring has no entry {account}. "
                "Run `sesa participants add` to store one, or switch to api_key_env.",
                pid=spec.id,
                account=repr(account),
            )
        )

    if isinstance(raw, str) and raw:
        # Sending a mistyped directive off as a plaintext key is a nasty way to fail: a typo like
        # `keryring` would go to the provider verbatim. Better to raise here than to silently treat
        # a word as a key.
        looks_like_directive = _DIRECTIVE_LIKE.match(raw) or raw.lower().startswith("keyring")
        if looks_like_directive:
            raise CredentialError(
                t(
                    "Participant {pid}: the api_key value {shown} looks like a mistyped "
                    "directive rather than an actual key. Valid forms: `keyring`, "
                    "`keyring:<entry name>`, or switch to `api_key_env: <variable name>`.",
                    pid=spec.id,
                    shown=_redact(raw),
                )
            )
        if len(raw) < _MIN_KEY_LENGTH:
            raise CredentialError(
                t(
                    "Participant {pid}: api_key is too short ({n} characters) to be a real "
                    "key. If you really mean to write it in plaintext, write the whole key; "
                    "`keyring` or `api_key_env` is better.",
                    pid=spec.id,
                    n=len(raw),
                )
            )
        return raw

    # Last resort: guess the environment variable from base_url, so that "nothing configured" still
    # has a chance of just running
    for name in env_hints_for(opts.get("base_url")):
        if value := os.environ.get(name):
            return value

    raise CredentialError(
        t(
            "Participant {pid}: no credential configured. Set api_key_env, or run "
            "`sesa participants add` to store one in the system keyring.",
            pid=spec.id,
        )
    )
