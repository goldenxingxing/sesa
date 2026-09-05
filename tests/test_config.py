"""Loading, merging and validating the configuration."""

from __future__ import annotations

import pytest

from sesa import config as cfg


def write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


BASIC = """
version: 1
participants:
  - id: claude
    adapter: cli
    command: ["claude", "-p"]
    prompt: stdin
    role: 务实的工程师
  - id: kimi
    adapter: openai_compat
    base_url: https://api.moonshot.cn/v1
    model: kimi-k2
    api_key_env: MOONSHOT_API_KEY
protocol: council
rounds: {max: 6, stability_window: 3}
consensus: {confidence_threshold: 0.75}
budget: {max_usd: 1.5, max_wall_seconds: 300}
"""


def test_loads_participants_and_settings(tmp_path):
    conf = cfg.load(write(tmp_path / "sesa.yaml", BASIC))
    assert [p.id for p in conf.participants] == ["claude", "kimi"]
    assert conf.protocol == "council"
    assert conf.max_rounds == 6 and conf.stability_window == 3
    assert conf.confidence_threshold == 0.75
    assert conf.max_usd == 1.5 and conf.max_wall_seconds == 300


def test_unknown_keys_flow_into_adapter_options(tmp_path):
    """ "Adding a new agent = writing a few lines of YAML" rests on this: anything that is not a
    first-class field goes to the adapter.
    """
    conf = cfg.load(write(tmp_path / "sesa.yaml", BASIC))
    claude = conf.participants[0]
    assert claude.options["command"] == ["claude", "-p"]
    assert claude.options["prompt"] == "stdin"
    assert "role" not in claude.options and claude.role == "务实的工程师"


def test_project_config_overrides_global_by_id(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    write(global_dir / "config.yaml", BASIC)
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", global_dir / "config.yaml")

    project = tmp_path / "project"
    project.mkdir()
    write(
        project / "sesa.yaml",
        """
participants:
  - id: kimi
    adapter: openai_compat
    base_url: https://example.com/v1
    model: 改过的模型
  - id: gpt5
    adapter: openai_compat
    model: openai/gpt-5
protocol: debate
""",
    )
    conf = cfg.load(cwd=project)
    table = {p.id: p for p in conf.participants}
    assert table["kimi"].model == "改过的模型"  # for a shared id the project level wins
    assert table["claude"].role == "务实的工程师"  # the global one is kept
    assert "gpt5" in table  # the new one is added
    assert conf.protocol == "debate"  # the project level overrides the setting


def test_requires_at_least_two_participants(tmp_path):
    conf = cfg.load(
        write(tmp_path / "sesa.yaml", "participants:\n  - {id: solo, adapter: cli, command: [x]}\n")
    )
    with pytest.raises(cfg.ConfigError, match="at least 2 participants"):
        conf.validate(conf.select(None))


def test_selecting_unknown_participant_lists_known_ones(tmp_path):
    conf = cfg.load(write(tmp_path / "sesa.yaml", BASIC))
    with pytest.raises(cfg.ConfigError, match="claude, kimi"):
        conf.select(["claude", "nobody"])


def test_duplicate_ids_are_rejected(tmp_path):
    conf = cfg.load(write(tmp_path / "sesa.yaml", BASIC))
    with pytest.raises(cfg.ConfigError, match="Duplicate"):
        conf.validate([conf.participants[0], conf.participants[0]])


def test_missing_adapter_is_reported_with_the_id(tmp_path):
    with pytest.raises(cfg.ConfigError, match="oops has no adapter"):
        cfg.load(write(tmp_path / "sesa.yaml", "participants:\n  - {id: oops}\n"))


def test_invalid_yaml_names_the_file(tmp_path):
    path = write(tmp_path / "sesa.yaml", "participants: [\n")
    with pytest.raises(cfg.ConfigError, match="is not valid YAML"):
        cfg.load(path)


def test_missing_explicit_config_is_reported(tmp_path):
    with pytest.raises(cfg.ConfigError, match="No such config file"):
        cfg.load(tmp_path / "nope.yaml")


def test_save_global_never_writes_plaintext_key(tmp_path, monkeypatch):
    """An open-source project should not teach people to store keys in plaintext — the config holds
    only the reference.
    """
    monkeypatch.setattr(cfg, "GLOBAL_DIR", tmp_path)
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "config.yaml")
    conf = cfg.load(write(tmp_path / "src.yaml", BASIC))
    path = cfg.save_global(conf)
    text = path.read_text(encoding="utf-8")
    assert "MOONSHOT_API_KEY" in text
    assert path.stat().st_mode & 0o777 == 0o600  # permissions tightened
