"""The tests must never read the real configuration on this machine.

After the first real user finished `sesa init`, the whole test suite broke on the spot: the
16 tests that spawn a subprocess running `python -m sesa.cli run` read their
``~/.config/sesa/config.yaml`` and so **really called claude / kimi / deepseek** — the tests
hung on timeouts (41 of them took 224 seconds) and it spent their real money.

Every test had been green until then, only because this machine happened to have no global
configuration. The textbook version of "it works on my machine".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_session_runs_against_an_isolated_config_dir():
    """conftest has to change it **before any sesa module is imported**.

    ``config.GLOBAL_DIR`` is a module-level constant, fixed the moment it is imported — changing
    it in a fixture is already too late.
    """
    from sesa import config as cfg

    isolated = os.environ.get("SESA_CONFIG_DIR")
    assert isolated, "conftest did not set SESA_CONFIG_DIR"
    assert Path(cfg.GLOBAL_DIR) == Path(isolated), (
        f"GLOBAL_DIR still points at {cfg.GLOBAL_DIR} — the isolation was set too late"
    )
    assert Path(cfg.GLOBAL_DIR) != Path.home() / ".config" / "sesa"


def test_a_subprocess_inherits_the_isolation():
    """Most of those 16 are subprocesses — monkeypatch affects only this process and cannot stop
    them.
    """
    done = subprocess.run(
        [sys.executable, "-c", "from sesa import config; print(config.GLOBAL_DIR)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == os.environ["SESA_CONFIG_DIR"]


def test_running_the_cli_without_a_config_says_so_instead_of_calling_models():
    """This is the premise those 16 tests rest on: with no configuration it should fail early.

    Reading the user's real configuration has it call real models — the test goes from "an error
    in 1 second" to "a timeout in minutes", and it costs money.
    """
    done = subprocess.run(
        [sys.executable, "-m", "sesa.cli", "run", "议题"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "COLUMNS": "120"},
    )
    assert done.returncode != 0
    combined = done.stdout + done.stderr
    assert "participant" in combined, f"it does not say what is missing: {combined[-400:]}"
