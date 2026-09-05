"""Whether the verification command imports a package of the same name from **outside** the
working copy.

This is the most insidious cause of "the green tests actually tested nothing": once the
repository has been `pip install -e`-ed, running pytest inside a different working copy
imports **the original repository's** code — whatever the participant changed is invisible
and the tests stay green forever.

This project has been bitten twice, once nearly concluding wrongly that "our tests cannot
catch this bug". In a deliberation it is worse: **the entire execution-evidence layer is
silently disabled while everybody "passes".**
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sesa.evidence.runner import shadowed_imports


def _package(root: Path, name: str = "mypkg") -> Path:
    pkg = root / "src" / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return pkg


def test_an_outside_copy_is_reported(tmp_path):
    """The interpreter can import a package of the same name from elsewhere ⇒ it has to be
    reported.
    """
    outside = tmp_path / "installed"
    _package(outside)
    work = tmp_path / "work"
    _package(work)

    command = f"PYTHONPATH={outside / 'src'} {sys.executable} -m pytest -q"
    checkable, shadowed = shadowed_imports(command, work)

    assert checkable
    assert shadowed, "it imports from outside the working copy and did not report it"
    assert "mypkg" in shadowed[0]
    assert str(outside) in shadowed[0]


def test_pointing_the_path_at_the_checkout_clears_it(tmp_path):
    """PYTHONPATH pointing back at the working copy makes it fine — the command prefix has to be
    counted.
    """
    work = tmp_path / "work"
    _package(work)

    command = f"PYTHONPATH=src {sys.executable} -m pytest -q"

    assert shadowed_imports(command, work) == (True, [])


def test_an_unrecognised_command_says_it_could_not_check(tmp_path):
    """**"Could not check" and "nothing wrong" are two different things.**

    Both used to return an empty list while the docstring claimed "the list distinguishes the
    two" — what the documentation said and what the code did were different things. Now the
    first return value is "could it be checked".
    """
    work = tmp_path / "work"
    _package(work)

    checkable, shadowed = shadowed_imports("npm test && npm run lint", work)

    assert checkable is False, "an unrecognised interpreter = could not check"
    assert shadowed == []


def test_a_package_that_is_not_installed_anywhere_is_fine(tmp_path):
    work = tmp_path / "work"
    _package(work, "brandnewpkg_xyz")

    assert shadowed_imports(f"PYTHONPATH=src {sys.executable} -m pytest -q", work) == (True, [])


async def test_the_engine_warns_before_trusting_the_evidence(tmp_path):
    """The warning has to come before the evidence is gathered — otherwise a whole deliberation's
    evidence is empty.
    """
    from sesa.engine import Engine
    from sesa.evidence import EvidenceRunner
    from sesa.protocols import build
    from sesa.record import Recorder, new_run_id
    from sesa.workspace import GitWorktreeWorkspace
    from tests.test_engine import drive, participant

    repo = tmp_path / "repo"
    _package(repo)
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, capture_output=True, check=True)
    (repo / "test_x.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)

    outside = tmp_path / "installed"
    _package(outside)

    engine = Engine(
        [participant("alice"), participant("bob")],
        build("debate"),
        recorder=Recorder(tmp_path, new_run_id()),
        workspace=GitWorktreeWorkspace(repo, "run1"),
        evidence=EvidenceRunner(
            f"PYTHONPATH={outside / 'src'} {sys.executable} -m pytest -q test_x.py"
        ),
        max_rounds=1,
    )
    events = await drive(engine, task="改点东西")

    warned = [e for e in events if e.t == "error" and e.where.startswith("verify")]
    assert warned, (
        "the verification command imports code from elsewhere and the engine did not warn"
    )
    assert "passing tests prove nothing" in warned[0].message
    first_evidence = next(i for i, e in enumerate(events) if e.t == "evidence")
    assert events.index(warned[0]) < first_evidence, (
        "the warning has to come before the first piece of evidence"
    )
