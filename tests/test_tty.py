"""Running the CLI through **a real terminal**.

This whole class of bug was found by a Sesa deliberation: `sesa run` crashed for certain
under a TTY (`ConsensusReport(unresolved=…)` passing a property as a field), while both a
pipe and `--json` bypass that rendering branch.

So the 264 tests of the time, and every manual check of mine that redirected output to a
file, **never once executed the path a new user takes at step one**.

This runs a real process through a pseudo-terminal, bringing that branch into coverage.
"""

from __future__ import annotations

import os
import pty
import selectors
import subprocess
import sys
from pathlib import Path

import pytest

FAKE = str(Path(__file__).parent / "fake_agent.py")


def run_in_tty(args: list[str], cwd: Path, timeout: float = 90) -> tuple[int, str]:
    """Run a command in a pseudo-terminal and return the exit code and all the output.

    The crucial part is that stdout must be a tty — the CLI takes the rich-rendering branch only
    when `sys.stdout.isatty()` is true, and the bug lives only on that branch.
    """
    primary, secondary = pty.openpty()
    proc = subprocess.Popen(
        args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=secondary, stderr=secondary, close_fds=True
    )
    os.close(secondary)
    chunks: list[bytes] = []
    selector = selectors.DefaultSelector()
    selector.register(primary, selectors.EVENT_READ)
    try:
        while True:
            if not selector.select(timeout=timeout):
                proc.kill()
                raise TimeoutError(f"{args} 超时")
            try:
                data = os.read(primary, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        selector.close()
        os.close(primary)
    return proc.wait(), b"".join(chunks).decode("utf-8", "replace")


def _project(tmp_path: Path, protocol: str = "debate") -> Path:
    (tmp_path / "sesa.yaml").write_text(
        "version: 1\nparticipants:\n"
        + "".join(
            f"  - id: {pid}\n    adapter: cli\n"
            f"    command: ['{sys.executable}', '{FAKE}']\n"
            f"    prompt: stdin\n    timeout: 30\n    env: {{FAKE_ID: {pid}}}\n"
            for pid in ("alice", "bob")
        )
        + f"protocol: {protocol}\nrounds: {{max: 2}}\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize("protocol", ["debate", "reflect"])
def test_a_full_run_renders_without_crashing_in_a_terminal(tmp_path, protocol):
    """The first thing a new user does is `sesa run` in a terminal. It has to run to the end."""
    code, out = run_in_tty(
        [sys.executable, "-m", "sesa.cli", "run", "该用 Postgres 还是 SQLite？"],
        _project(tmp_path, protocol),
    )

    assert "Traceback" not in out, f"the TTY rendering path raised:\n{out[-1500:]}"
    assert "TypeError" not in out
    assert code in (0, 2, 3, 4), f"exit code {code}, output:\n{out[-800:]}"


def test_the_consensus_matrix_actually_renders(tmp_path):
    """The disagreement matrix is rendered only under a TTY — which is exactly where the crash was."""
    _, out = run_in_tty(
        [sys.executable, "-m", "sesa.cli", "run", "议题"],
        _project(tmp_path),
    )

    assert "alice" in out and "bob" in out
    assert "Traceback" not in out


def test_doctor_and_runs_render_in_a_terminal(tmp_path):
    project = _project(tmp_path)
    run_in_tty([sys.executable, "-m", "sesa.cli", "run", "议题"], project)

    for command in (["doctor"], ["runs"], ["eval"]):
        code, out = run_in_tty([sys.executable, "-m", "sesa.cli", *command], project)
        assert "Traceback" not in out, f"`sesa {command[0]}` raised under a TTY:\n{out[-800:]}"
        assert code in (0, 1), f"`sesa {command[0]}` exited {code}"


def test_a_non_measuring_protocol_shows_no_disagreement_count(tmp_path):
    """reflect structurally produces no peer assessment, so "N open disagreements" must not appear
    in the progress display.

    After the outcome layer was fixed, the same error remained in the TTY's progress output: the
    matrix rendered as a field of "unknown" with "2 open disagreements" beneath it — labelling
    missing data as disagreement.
    """
    _, out = run_in_tty(
        [sys.executable, "-m", "sesa.cli", "run", "议题"],
        _project(tmp_path, "reflect"),
    )

    assert "cells with an explicit objection" not in out, (
        f"reflect reported open disagreements:\n{out[-800:]}"
    )
    assert "no peer assessment" in out  # the interface defaults to English; Chinese is
    # covered by the catalogue completeness test
    assert "Traceback" not in out


def test_a_measuring_protocol_still_shows_the_matrix(tmp_path):
    """The counterexample: for a protocol that does peer-assess, the matrix and the unresolved-cell
    accounting still have to be shown.
    """
    _, out = run_in_tty(
        [sys.executable, "-m", "sesa.cli", "run", "议题"],
        _project(tmp_path, "debate"),
    )

    assert "alice" in out and "bob" in out
    assert "未解决" in out or "明确反对" in out or "not measured" in out
    assert "cells with an explicit objection" not in out, (
        "'unresolved' = opposition + not measured, and the whole must not be called 'disagreement'"
    )


def test_a_real_run_renders_in_a_real_terminal(tmp_path):
    """**Pipe mode never reaches the rendering branch.**

    While migrating to i18n I used `t()` in cli.py and forgot to import it. Under a pipe the
    JSONL branch runs and never touches that code — so every pipe check was green, while a real
    terminal gave `NameError: name 't' is not defined` and crashed on the first run.

    This is the same hole as the earlier "sesa run crashes for certain under a real TTY while
    every check went through a pipe". So the rendering path has to have a test through a real
    pseudo-terminal.
    """
    import os
    import pty
    import re
    import select
    import subprocess
    import sys
    import time

    fake = Path(__file__).parent / "fake_agent.py"
    config = tmp_path / "sesa.yaml"
    config.write_text(
        "version: 1\nparticipants:\n"
        + "".join(
            f'  - {{id: {pid}, adapter: cli, command: ["{sys.executable}", "{fake}"], '
            f"prompt: stdin, timeout: 20, env: {{FAKE_ID: {pid}}}}}\n"
            for pid in ("alice", "bob")
        )
        + "protocol: debate\nrounds: {max: 1}\n",
        encoding="utf-8",
    )

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "sesa.cli", "run", "-c", str(config), "Postgres or SQLite?"],
        stdout=slave,
        stderr=slave,
        stdin=subprocess.DEVNULL,
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm", "COLUMNS": "100", "SESA_LANG": "en"},
        close_fds=True,
    )
    os.close(slave)
    out, deadline = b"", time.time() + 90
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif proc.poll() is not None:
                break
    finally:
        proc.kill()
        os.close(master)

    text = re.sub(rb"\x1b\[[0-9;]*m", b"", out).decode("utf-8", "replace")
    assert "Traceback" not in text, f"it crashed under a real terminal:\n{text[:1200]}"
    assert "NameError" not in text
    assert "Round 0" in text, f"the round was not rendered:\n{text[:600]}"
