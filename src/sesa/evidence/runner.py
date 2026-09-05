"""Run verification commands, and cross-test.

**Why cross-testing is needed**: in a code task the participants write both the
implementation and the tests, so **a green light proves nothing** — it may be
``assert True``, or assertions that happen to match their own bug. This is not a
theoretical risk; it is typical agent behaviour.

The answer is to run **A's tests against B's implementation**:

```
                claude's impl   kimi's impl
claude's tests       ✅              ❌
kimi's tests         ✅              ✅
```

If someone's tests pass **only for themselves**, either the tests encode their private
assumptions or the others really are wrong — and either way, **the disagreement has been
located on one specific test**, which beats ten rounds of arguing.

> The stance matrix is what they say. **The cross-test matrix is what they did.**
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import t
from ..state import EvidenceRecord
from ..workspace.base import Checkout

#: How far output is truncated. Evidence has to be readable without pouring an entire test log into
#: the next round's context.
SUMMARY_CHARS = 1200


#: Patterns for recognising the interpreter in a verification command. Only the commonest forms are
#: handled; unrecognised means no check.
_ASSIGNMENTS_ONLY = re.compile(r"^(?:\s*[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]*\s*)*$")
_PY_INVOCATION = re.compile(r"(?:^|\s|=)((?:[\w./~-]*)python[\d.]*)\b")


def shadowed_imports(command: str, cwd: Path) -> tuple[bool, list[str]]:
    """Check whether the verification command will import a package of the same name from
    **outside the working copy**.

    This is the most insidious cause of "the green tests actually tested nothing": once the
    repository has been ``pip install -e``-ed, running pytest inside a different working copy
    imports **the original repository's** code — whatever the participant changed is
    invisible, and the tests stay green forever.

    This project has been bitten twice, once nearly concluding wrongly that "our tests cannot
    catch this bug". In a deliberation it silently disables the entire execution-evidence
    layer: everybody "passes", and not one person's change was ever really run.

    Returns ``(could it be checked, list of shadowed package names)``.

    **"Could not check" and "nothing wrong" have to be kept apart** — with both returning an
    empty list, this function would be committing the very error it exists to prevent. Its
    earlier docstring claimed the list distinguished the two while the code did ``return []``
    in both cases: **what the documentation said and what the code did were different things.**
    """
    match = _PY_INVOCATION.search(command)
    if not match:
        return False, []  # Interpreter unrecognised: could not check, which is not "nothing
        # wrong"
    interpreter = match.group(1)
    # What precedes the interpreter is often an environment assignment like `PYTHONPATH=src`, and
    # that is precisely the usual way of fixing shadowing. A probe that leaves it out reports an
    # already-fixed command as broken.
    prefix = command[: match.start(1)]
    roots = [p for p in (cwd / "src", cwd) if p.is_dir()]
    candidates = {
        entry.name
        for root in roots
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists() and not entry.name.startswith(".")
    }
    shadowed = []
    for name in sorted(candidates):
        probe = f"import {name},os,sys;print(os.path.realpath({name}.__file__))"
        try:
            got = subprocess.run(
                # prefix is the user's command verbatim **before** the interpreter (usually an
                # assignment like `PYTHONPATH=src `). It goes into the shell as-is, and this is a
                # one-off subprocess for probing — not the user's command itself (which has to run
                # with shell=True anyway). It is still restricted to assignment forms, so that
                # something like `; rm -rf /` cannot ride along.
                f"{prefix if _ASSIGNMENTS_ONLY.match(prefix) else ''}"
                f"{shlex.quote(interpreter)} -c {shlex.quote(probe)}",
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        resolved = got.stdout.strip()
        if got.returncode or not resolved:
            continue
        try:
            Path(resolved).relative_to(cwd.resolve())
        except ValueError:
            shadowed.append(f"{name} → {resolved}")
    return True, shadowed


class _Leaked(Exception):
    """Even SIGKILL failed to reap it after the timeout — the process is still alive.

    This has to be kept apart from "terminated": with the latter the workspace is still, with
    the former it is still being written to.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__(t("the process was still alive after the {n}s timeout", n=timeout))
        self.timeout = timeout


def run_verify(command: str, cwd: Path, timeout: float = 600.0) -> tuple[int, str]:
    """Run the verification command in a given directory; returns (exit code, summary).

    It runs through a shell, because a verify command is usually a line the user wrote
    themselves (``pytest -q``, ``npm test && npm run lint``) — splitting it into words would
    break their intent.
    """
    try:
        # `start_new_session` puts the child in its own process group, so the whole group can be
        # reaped on timeout. Otherwise, under `shell=True`, only the shell is killed and the process
        # really running inside `npm test && npm run lint` is orphaned, still holding CPU and ports.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            # `wait()` gets **a cap**: if killpg is silently swallowed (the process changed group,
            # permissions are insufficient), an uncapped wait hangs here forever — inside what is
            # supposed to be the timeout handler.
            # But failing to reap cannot pass in silence either: it means the child is **still
            # alive**, still holding the working directory and writing files, while we are about to
            # treat this workspace as "finished" for fingerprint comparison and cross-testing.
            leaked = False
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                leaked = True
            if leaked:
                # Carry the fact out on a different exception. Re-raising as-is would fall into the
                # shared "terminated" branch below, and that branch would be lying — the process is
                # not terminated at all.
                raise _Leaked(timeout) from None
            raise
        result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    except _Leaked as leak:
        return 124, t(
            "the command did not finish within {cap:.0f}s and **could not be killed**: "
            "the process may still be running and still writing to this working "
            "directory. Fingerprints and cross-tests based on this workspace are no "
            "longer trustworthy.",
            cap=leak.timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, t(
            "the command did not finish within {cap:.0f}s and was terminated", cap=timeout
        )
    except OSError as exc:
        return 127, t("could not execute: {exc}", exc=exc)

    output = (result.stdout or "") + (result.stderr or "")
    # The important part of test output is usually at the end (the failure summary, the statistics
    # line), so keep the tail and drop the head
    if len(output) > SUMMARY_CHARS:
        output = t("…(truncated)\n") + output[-SUMMARY_CHARS:]
    return result.returncode, output.strip()


@dataclass
class CrossTestMatrix:
    """A's tests × B's implementation. The diagonal is self-testing; only off the diagonal is it
    a cross-check.
    """

    command: str
    #: (test author, implementation author) -> exit code
    results: dict[tuple[str, str], int] = field(default_factory=dict)

    def passed(self, tester: str, implementer: str) -> bool | None:
        code = self.results.get((tester, implementer))
        return None if code is None else code == 0

    def only_own_tests_pass(self, participant: str) -> bool:
        """Whether someone's tests pass only for themselves — the signal that the tests encode
        private assumptions.

        .. warning::
           **This test needs the diagonal (self-tests), and ``cross_test`` runs only off the
           diagonal.** Used alone it is always ``False``: the caller has to feed the self-test
           results in as well, or the question asked can never be true. See
           :meth:`suspicious_testers`.
        """
        own = self.passed(participant, participant)
        others = [
            self.passed(participant, impl)
            for (tester, impl) in self.results
            if tester == participant and impl != participant
        ]
        return bool(own) and bool(others) and not any(others)

    def suspicious_testers(self, self_tests: dict[tuple[str, str], int]) -> list[str]:
        """Those whose tests pass only for themselves — the signal that **the tests encode private
        assumptions**.

        ``cross_test`` skips the diagonal, so the self-test results have to be supplied by the
        caller. :meth:`only_own_tests_pass` used to be dead code: not one call site in the whole
        codebase, so this signal was never asked for — not "always False", simply never asked.
        """
        merged = CrossTestMatrix(command=self.command, results={**self_tests, **self.results})
        testers = {tester for tester, _ in self.results}
        flagged = [pid for pid in sorted(testers) if merged.only_own_tests_pass(pid)]
        # **Everyone suspicious = nobody suspicious.** With two participants it is the norm for a's
        # tests to pass only for a and b's only for b (each writing their own implementation and
        # tests), and naming both there offers no discrimination at all — it only teaches the reader
        # to skip this section. A signal has to separate people to be a signal.
        return [] if len(flagged) == len(testers) else flagged

    def universally_passing(self) -> list[str]:
        """Participants whose implementation passes **everyone's** tests. This is the hardest form of
        evidence there is.
        """
        implementers = {impl for _, impl in self.results}
        out = []
        for impl in sorted(implementers):
            verdicts = [self.passed(tester, impl) for (tester, i) in self.results if i == impl]
            if verdicts and all(verdicts):
                out.append(impl)
        return out

    def render(self) -> str:
        from ..consensus.matrix import _display_width, _pad

        testers = sorted({tester for tester, _ in self.results})
        impls = sorted({impl for _, impl in self.results})
        if not testers or not impls:
            return t("(no cross-test data)")

        labels = [t("{pid}'s tests", pid=pid) for pid in testers]
        corner = t("A's tests ＼ B's implementation")
        left = max(_display_width(x) for x in [*labels, corner])
        cell = max([_display_width(i) for i in impls] + [4]) + 2

        lines = [_pad(corner, left) + "  " + "".join(_pad(i, cell) for i in impls)]
        for tester, label in zip(testers, labels, strict=True):
            cells = []
            for impl in impls:
                ok = self.passed(tester, impl)
                mark = "—" if ok is None else (t("pass") if ok else t("fail"))
                cells.append(_pad(mark, cell))
            lines.append(_pad(label, left) + "  " + "".join(cells))
        return "\n".join(lines)


class EvidenceRunner:
    """Run the verification command and produce evidence labelled with its source."""

    def __init__(self, command: str, timeout: float = 600.0, test_paths: list[str] | None = None):
        self.command = command
        self.timeout = timeout
        #: Protected baseline test paths. A participant modifying these is in violation.
        self.test_paths = test_paths or []

    def self_test(
        self, checkouts: dict[str, Checkout], revisions: dict[str, str | None]
    ) -> list[EvidenceRecord]:
        """Each runs once in their own worktree — **the weakest form of evidence**.

        When whoever writes the implementation also writes the tests, a green light says next to
        nothing.
        """
        records = []
        for pid, checkout in checkouts.items():
            code, summary = run_verify(self.command, checkout.path, self.timeout)
            records.append(
                EvidenceRecord(
                    participant=pid,
                    cmd=self.command,
                    exit_code=code,
                    summary=summary,
                    source="engine",
                    against=pid,  # testing yourself
                    revision=revisions.get(pid),
                )
            )
        return records

    def cross_test(
        self,
        checkouts: dict[str, Checkout],
        revisions: dict[str, str | None],
        copy_paths: list[str],
    ) -> tuple[CrossTestMatrix, list[EvidenceRecord]]:
        """Move each person's tests into someone else's worktree and run them.

        ``copy_paths`` are the relative paths the tests live at. **Only the tests move, never the
        implementation** — otherwise what runs is no longer the other's implementation. The scene
        must be restored afterwards, or the next round's participant sees code they did not write.
        """
        matrix = CrossTestMatrix(command=self.command)
        records: list[EvidenceRecord] = []
        participants = sorted(checkouts)

        for tester in participants:
            for implementer in participants:
                if tester == implementer:
                    continue
                target = checkouts[implementer].path
                # `backups` must exist and be visible **before** the try. It used to be written as
                # `backups = self._install_tests(...)` and then enter the try — and if installation
                # raised on the Nth path (copytree failing, mkdir without permission), the tests
                # already copied in **would never be restored**, polluting the other party's working
                # copy for good, while "the scene must be restored afterwards" is the very premise
                # of cross-testing.
                backups: list[tuple[Path, Path | None]] = []
                try:
                    self._install_tests(checkouts[tester].path, target, copy_paths, backups)
                    code, summary = run_verify(self.command, target, self.timeout)
                finally:
                    self._restore(backups)
                matrix.results[(tester, implementer)] = code
                records.append(
                    EvidenceRecord(
                        participant=tester,
                        cmd=self.command
                        + t(
                            " ({tester}'s tests × {impl}'s implementation)",
                            tester=tester,
                            impl=implementer,
                        ),
                        exit_code=code,
                        summary=summary,
                        source="engine",
                        against=implementer,
                        revision=revisions.get(implementer),
                    )
                )
        return matrix, records

    # ------------------------------------------------------------------ #

    @staticmethod
    def _unused_name(path: Path) -> Path:
        """Find a name that overwrites no existing file."""
        index = 1
        while (candidate := path.with_name(f"{path.name}.{index}")).exists():
            index += 1
        return candidate

    @staticmethod
    def _install_tests(
        source: Path,
        target: Path,
        paths: list[str],
        backups: list[tuple[Path, Path | None]] | None = None,
    ) -> list[tuple[Path, Path | None]]:
        """Move source's tests into target, **appending** each restorable backup to backups one at a
        time.

        The list the caller passed in is the restore manifest — **register every file the moment
        it is touched**, or the caller's finally cannot restore after an exception part-way. The
        return value exists only for compatibility with older call sites.
        """
        backups = [] if backups is None else backups
        for rel in paths:
            src, dst = source / rel, target / rel
            if not src.exists():
                continue
            saved = None
            if dst.exists():
                saved = dst.with_name(dst.name + ".sesa-backup")
                if saved.exists():
                    # **The last run crashed before restoring**: this backup may be the only
                    # surviving copy of the original file. Deleting it unconditionally and copying
                    # the tests in loses the original for good. Pick a name that does not collide,
                    # and keep both.
                    spare = EvidenceRunner._unused_name(saved)
                    saved.rename(spare)
                dst.rename(saved)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            backups.append((dst, saved))
        return backups

    @staticmethod
    def _restore(backups: list[tuple[Path, Path | None]]) -> None:
        """Restore the scene — a cross-test must leave no trace."""
        for dst, saved in reversed(backups):
            if dst.exists():
                _remove(dst)
            if saved and saved.exists():
                saved.rename(dst)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
