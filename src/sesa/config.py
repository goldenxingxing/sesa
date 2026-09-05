"""Loading and merging configuration.

Two levels, **filled in once and reused indefinitely**:

```
~/.config/sesa/config.yaml   the global participant library, shared across projects
                             (written by sesa init)
./sesa.yaml                  per project: which of them, which protocol, what budget
```

The project level overrides the global one; participants are merged by ``id`` (the
project level wins for a shared id).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .i18n import t
from .types import ParticipantSpec

GLOBAL_DIR = Path(os.environ.get("SESA_CONFIG_DIR") or (Path.home() / ".config" / "sesa"))
GLOBAL_CONFIG = GLOBAL_DIR / "config.yaml"
PROJECT_CONFIG_NAMES = ("sesa.yaml", "sesa.yml", ".sesa.yaml")

#: First-class fields of ParticipantSpec; every other key goes into options for the adapter to
#: interpret, which is what makes "adding a new agent = writing a few lines of YAML" true.
_SPEC_FIELDS = {"id", "adapter", "model", "role"}


class ConfigError(RuntimeError):
    """Something is wrong with the configuration itself — the message must tell the user
    exactly what to change.
    """


# --------------------------------------------------------------------------- # Presets: so the init
# wizard can configure a common provider in one line
# --------------------------------------------------------------------------- #

#: ``hint`` is only for the wizard's prompt; it is never written into a participant's config.
#: Dangerous switches (--yolo / --dangerously-skip-permissions) are never preset — the user must
#: enable them explicitly, per participant (see DESIGN.md §10).
CLI_PRESETS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Code",
        "adapter": "cli",
        "command": ["claude", "-p"],
        "prompt": "stdin",
        "role": "A pragmatic systems engineer who puts maintainability and operational cost first",
    },
    "kimi": {
        "label": "Kimi CLI",
        "adapter": "cli",
        "command": ["kimi", "--quiet"],
        "prompt": "stdin",
        "role": "A pragmatic systems engineer who puts implementation cost and maintainability first",
        "hint": "add --yolo yourself when it needs to run tools (auto-approves everything)",
    },
    "codex": {
        "label": "Codex CLI",
        "adapter": "cli",
        "command": ["codex", "exec"],
        "prompt": "stdin",
        "role": "An engineer who attends to implementation detail and edge cases",
    },
    "dsh": {
        "label": "DeepSeek Harness",
        "adapter": "cli",
        "command": ["dsh", "--profile", "headless"],
        "prompt": "argv",
        "role": "A bold innovator willing to challenge established assumptions",
    },
    "gemini": {
        "label": "Gemini CLI",
        "adapter": "cli",
        "command": ["gemini", "-p"],
        "prompt": "argv",
        "role": "A wide-angle architect concerned with long-term evolution",
    },
    "aider": {
        "label": "Aider",
        "adapter": "cli",
        "command": ["aider", "--no-git", "--message"],
        "prompt": "argv",
        "role": "A pragmatist who keeps changes minimal",
    },
    "cursor-agent": {
        "label": "Cursor Agent",
        "adapter": "cli",
        "command": ["cursor-agent", "-p"],
        "prompt": "stdin",
        "role": "A refactorer at home in large codebases",
    },
}

API_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "adapter": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env": "DEEPSEEK_API_KEY",
    },
    "kimi": {
        "label": "Kimi",
        "adapter": "openai_compat",
        "base_url": "https://api.kimi.com/coding/v1",
        "model": "kimi-for-coding",
        "env": "KIMI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter (one key, many vendors)",
        "adapter": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-5",
        "env": "OPENROUTER_API_KEY",
    },
    "ollama": {
        "label": "Ollama (local, no key needed)",
        "adapter": "openai_compat",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1",
        "env": None,
    },
    "anthropic": {
        "label": "Claude API",
        "adapter": "anthropic",
        "model": "claude-sonnet-5",
        "env": "ANTHROPIC_API_KEY",
    },
}


def detect_installed_clis() -> list[str]:
    """Probe which agent CLIs are installed on this machine, so the init wizard can offer them
    as checkboxes.
    """
    return [key for key, preset in CLI_PRESETS.items() if shutil.which(preset["command"][0])]


# --------------------------------------------------------------------------- # Config
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    participants: list[ParticipantSpec] = field(default_factory=list)
    protocol: str = "debate"
    rapporteur: str = "rotate"
    proposer: str = "rotate"
    turn_taking: str = "parallel"
    share_thinking: str = "never"
    share_residuals: bool = True
    #: UI language: ``en`` / ``zh``. Left empty, it is guessed from the system locale, falling back
    #: to English.
    #: **This is a different thing from the deliberation language.** This setting governs only the
    #: wording of the CLI, wizard, TUI and reports; which language the parties speak is decided by
    #: the task text itself (see prompts.pick_language) — asking in Chinese should get you Chinese
    #: deliverables even when the interface is in English.
    language: str | None = None
    max_rounds: int = 4
    stability_window: int = 2
    confidence_threshold: float = 0.6
    min_coverage: float = 0.0
    max_usd: float | None = None
    max_tokens: int | None = None
    #: Wall-clock limit for the whole run. **Unlimited by default.**
    #: This used to default to 900s while ``turn_timeout`` defaulted to 1800s — **a single turn's
    #: limit at twice the whole run's limit**, which does not hold together at all. Measured, on the
    #: first real user: three parties over four rounds, the slowest turn each round taking 150–690s,
    #: so at least 2700s in total; they had configured 2000s, so by the start of round 3 the
    #: remaining budget was too small, every call's timeout was squeezed down to 1 second, that
    #: round was doomed, and it nearly buried the results of the first three rounds with it.
    #: More fundamentally: **the total is already structurally bounded** — at most ``max_rounds``
    #: rounds, none longer than ``turn_timeout``. A wall-clock limit adds no protection at all; it
    #: adds exactly one failure mode, "cut off part-way".
    #: The three that do work are elsewhere: ``turn_timeout`` (how long one call may take), the
    #: adapter's ``idle_timeout`` (how long it may go quiet after it has started speaking), and
    #: ``max_usd`` / ``max_tokens`` (real money).
    max_wall_seconds: float | None = None
    turn_timeout: float = 1800.0
    sources: list[Path] = field(default_factory=list)

    # ------------------------------------------------------------------ #

    def select(self, ids: list[str] | None) -> list[ParticipantSpec]:
        """Select participants by id; ``None`` means all of them."""
        if not ids:
            return list(self.participants)
        table = {p.id: p for p in self.participants}
        missing = [i for i in ids if i not in table]
        if missing:
            known = ", ".join(table) or t("(the configuration has no participants at all)")
            raise ConfigError(
                t(
                    "No such participant: {missing}. Configured: {known}",
                    missing=", ".join(missing),
                    known=known,
                )
            )
        return [table[i] for i in ids]

    def validate(self, chosen: list[ParticipantSpec]) -> None:
        if len(chosen) < 2:
            raise ConfigError(
                t(
                    "A deliberation needs at least 2 participants; there are {n}. "
                    "Run `sesa init` or `sesa participants add`.",
                    n=len(chosen),
                )
            )
        seen = set()
        for spec in chosen:
            if spec.id in seen:
                raise ConfigError(t("Duplicate participant id: {id}", id=spec.id))
            seen.add(spec.id)


# --------------------------------------------------------------------------- # Reading and writing
# --------------------------------------------------------------------------- #


def _spec_from_dict(raw: dict[str, Any]) -> ParticipantSpec:
    if not raw.get("id"):
        raise ConfigError(t("A participant has no id: {raw}", raw=repr(raw)))
    if not raw.get("adapter"):
        raise ConfigError(t("Participant {id} has no adapter", id=raw["id"]))
    return ParticipantSpec(
        id=str(raw["id"]),
        adapter=str(raw["adapter"]),
        model=raw.get("model"),
        role=raw.get("role"),
        options={k: v for k, v in raw.items() if k not in _SPEC_FIELDS},
    )


def spec_to_dict(spec: ParticipantSpec) -> dict[str, Any]:
    out: dict[str, Any] = {"id": spec.id, "adapter": spec.adapter}
    if spec.model:
        out["model"] = spec.model
    out.update(spec.options)
    if spec.role:
        out["role"] = spec.role
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(t("{path} is not valid YAML: {error}", path=path, error=exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError(t("The top level of {path} must be a mapping (key: value)", path=path))
    return data


def find_project_config(start: Path | None = None) -> Path | None:
    directory = (start or Path.cwd()).resolve()
    for name in PROJECT_CONFIG_NAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _section(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    """Take one section of the config, and confirm it really is a mapping.

    Writing ``rounds: 3`` (where ``rounds: {max: 3}`` was meant) used to be **silently
    ignored**: ``"max" in "三"`` is only a substring test, returns False, and so the number
    of rounds the user set never took effect — with not one word about it in the log.
    Writing a negative number was worse — ``"max" in -5`` raises TypeError, and the user
    sees a Python traceback.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            t(
                "`{key}` in {path} must be a set of key/value pairs, but it is a "
                "{kind}: {value}\n  It should look like:\n    {key}:\n      max: 3",
                key=key,
                path=path,
                kind=type(value).__name__,
                value=repr(value),
            )
        )
    return value


def _number(section: dict[str, Any], key: str, cast, path: Path, *, positive: bool = True):
    """Convert one numeric setting, and if it will not convert, say which one and what was
    written.
    """
    value = section[key]
    try:
        result = cast(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            t(
                "`{key}` in {path} needs a number, but it is {value}",
                key=key,
                path=path,
                value=repr(value),
            )
        ) from exc
    if positive and result <= 0:
        raise ConfigError(
            t(
                "`{key}` in {path} must be greater than 0, but it is {value}. Zero or "
                "a negative number makes this setting silently inert rather than "
                "«unlimited».",
                key=key,
                path=path,
                value=result,
            )
        )
    return result


def _apply(config: Config, data: dict[str, Any], path: Path) -> None:
    config.sources.append(path)

    raw_participants = data.get("participants") or []
    if not isinstance(raw_participants, list):
        raise ConfigError(
            t(
                "`participants` in {path} must be a list, but it is a {kind}. Written "
                "as a string it gets iterated character by character, and the error "
                "you see has nothing to do with the real problem.",
                path=path,
                kind=type(raw_participants).__name__,
            )
        )
    incoming = [_spec_from_dict(p) for p in raw_participants]
    if incoming:
        merged = {p.id: p for p in config.participants}
        for spec in incoming:
            merged[spec.id] = spec  # for a shared id, whichever was loaded later (the
            # project level) wins
        config.participants = list(merged.values())

    if "language" in data:
        config.language = str(data["language"]).strip().lower() or None
    for key in ("protocol", "rapporteur", "proposer", "turn_taking", "share_thinking"):
        if key in data:
            setattr(config, key, str(data[key]))
    if "share_residuals" in data:
        # `bool("false")` is True. Writing a quoted "false" (or "no") in YAML is a common slip, and
        # this field decides whether residuals enter other people's context.
        config.share_residuals = _as_bool(data["share_residuals"])

    rounds = _section(data, "rounds", path)
    if "max" in rounds:
        config.max_rounds = _number(rounds, "max", int, path)
    if "stability_window" in rounds:
        config.stability_window = _number(rounds, "stability_window", int, path)
    if "turn_timeout" in rounds:
        config.turn_timeout = _number(rounds, "turn_timeout", float, path)

    consensus = _section(data, "consensus", path)
    for key, attr in (
        ("confidence_threshold", "confidence_threshold"),
        ("min_coverage", "min_coverage"),
    ):
        if key in consensus:
            # The threshold may legitimately be 0 (no bar at all), so it need not be positive here;
            # but it must be within 0–1.
            value = _number(consensus, key, float, path, positive=False)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(
                    t(
                        "`{key}` in {path} should be between 0 and 1, but it is {value}",
                        key=key,
                        path=path,
                        value=value,
                    )
                )
            setattr(config, attr, value)

    budget = _section(data, "budget", path)
    for key, attr, cast in (
        ("max_usd", "max_usd", float),
        ("max_tokens", "max_tokens", int),
        ("max_wall_seconds", "max_wall_seconds", float),
    ):
        if key in budget:
            value = budget[key]
            setattr(config, attr, None if value is None else _number(budget, key, cast, path))


def load(explicit: Path | None = None, *, cwd: Path | None = None) -> Config:
    """Load the configuration: global, then project level on top. An explicit path reads only
    that file.
    """
    config = Config()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(t("No such config file: {path}", path=path))
        _apply(config, _read_yaml(path), path)
        return config

    if GLOBAL_CONFIG.exists():
        _apply(config, _read_yaml(GLOBAL_CONFIG), GLOBAL_CONFIG)
    if project := find_project_config(cwd):
        _apply(config, _read_yaml(project), project)
    return config


def save_global(config: Config) -> Path:
    """Write the global participant library back. Credentials do not live here — only the
    reference to them.
    """
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "participants": [spec_to_dict(p) for p in config.participants],
        "language": config.language,
        "protocol": config.protocol,
        "rapporteur": config.rapporteur,
        "proposer": config.proposer,
        "turn_taking": config.turn_taking,
        "share_thinking": config.share_thinking,
        "share_residuals": config.share_residuals,
        # turn_timeout has to be written back too: `_apply` reads it out of rounds, and not writing
        # it here means the user's setting disappears at the next `sesa init` — **a field lost on
        # write-back, and what the user sees is "but I definitely configured that"**.
        "rounds": {
            "max": config.max_rounds,
            "stability_window": config.stability_window,
            "turn_timeout": config.turn_timeout,
        },
        "consensus": {
            "confidence_threshold": config.confidence_threshold,
            "min_coverage": config.min_coverage,
        },
        "budget": {
            "max_usd": config.max_usd,
            "max_tokens": config.max_tokens,
            "max_wall_seconds": config.max_wall_seconds,
        },
    }
    GLOBAL_CONFIG.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    GLOBAL_CONFIG.chmod(0o600)
    return GLOBAL_CONFIG


def _as_bool(value: object) -> bool:
    """Parse a boolean that YAML may have carried as a string."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1", "是")
    return bool(value)


def declared_model(spec) -> str | None:
    """Which model this participant uses **according to the configuration**.

    API adapters simply have ``spec.model``. A CLI adapter's model is the CLI's own
    business, but if the command line explicitly carries ``--model X`` / ``-m X``, that is
    authoritative — we passed it in.

    Returns ``None`` when it cannot be determined, and the caller decides how to say so.
    **Do not return "—"**: that dash could mean "not configured" or "configured but unknown
    to us", and the two mean entirely different things to the user (measured: one user's dsh
    was in fact running Kimi K3, and the table showed only a dash).
    """
    if getattr(spec, "model", None):
        return spec.model
    command = (getattr(spec, "options", {}) or {}).get("command") or []
    parts = [str(x) for x in command]
    for flag in ("--model", "-m"):
        if flag in parts:
            index = parts.index(flag)
            if index + 1 < len(parts):
                return parts[index + 1]
    return None
