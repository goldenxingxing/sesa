"""Keep the tests entirely apart from **the real configuration on this machine**.

How it started: the first real user finished `sesa init`, and the machine then had a
``~/.config/sesa/config.yaml`` with six real participants. From then on the 16 tests in
``pytest`` that spawn a subprocess running `python -m sesa.cli run` **read it**, and so
really called claude / kimi / deepseek — the tests hung on timeouts, **and it spent their
real money**.

Every test had been green until then, only because this machine happened to have no global
configuration. The textbook version of "it works on my machine".

The isolation has to be complete **before any sesa module is imported**:
``config.GLOBAL_DIR`` is a module-level constant, fixed the moment it is imported. So this
sets the environment variable at module top level rather than in a fixture — by the time a
fixture runs it is already too late.

It uses ``os.environ`` rather than monkeypatch because **subprocesses have to inherit it**:
most of those 16 are subprocesses, and monkeypatch affects only this process.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

#: One empty config directory shared by the whole test session. Empty is correct — a test either
#: brings its own `--config` or should be seeing "no participants configured yet".
_ISOLATED = Path(tempfile.mkdtemp(prefix="sesa-tests-config-"))
os.environ["SESA_CONFIG_DIR"] = str(_ISOLATED)

#: Block credentials while we are at it: a real run must not go near the user's keyring.
os.environ.setdefault("SESA_TEST_MODE", "1")


@pytest.fixture(autouse=True)
def _keep_global_config_isolated(monkeypatch):
    """Every test reconfirms it, in case something inside a test changes it."""
    monkeypatch.setenv("SESA_CONFIG_DIR", str(_ISOLATED))


def pytest_report_header(config):
    return f"sesa: global config isolated to {_ISOLATED} (never reads ~/.config/sesa)"


@pytest.fixture(autouse=True)
def _reset_ui_language():
    """Every test starts in English — **the language is process-wide global state**.

    One test calling ``i18n.use("zh")`` and forgetting to restore it leaves the next test
    running under a Chinese interface; each is green on its own and they only go red together.
    That kind of failure is hard to attribute, so rather than relying on every test to be
    disciplined, it is caught here in one place.
    """
    from sesa import i18n

    i18n.use("en")
    yield
    i18n.use("en")
