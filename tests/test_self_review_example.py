"""examples/self-review is the only "run a whole real deliberation" test form in the
repository.

It cannot run in CI (real models, real money, non-deterministic), but its scaffolding has to
stay alive — a command in the documentation that does not work is a hard failing for an open
source project. This at least keeps the conversion script from rotting.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "self-review"


def _convert(payload: dict) -> str:
    got = subprocess.run(
        [sys.executable, str(EXAMPLE / "to_briefing.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return got.stdout


def test_findings_become_a_briefing_with_locations():
    out = _convert(
        {
            "comments": [
                {
                    "severity": "high",
                    "category": "bug",
                    "path": "src/a.py",
                    "start_line": 42,
                    "content": "空指针",
                    "existing_code": "x = None",
                },
                {
                    "severity": "low",
                    "category": "style",
                    "path": "src/b.py",
                    "start_line": 7,
                    "content": "命名",
                },
            ]
        }
    )

    assert "src/a.py:42" in out and "src/b.py:7" in out
    assert "x = None" in out
    assert out.index("high") < out.index("low"), "the severe ones come first"


def test_the_briefing_tells_the_holder_to_verify_not_relay():
    """ "The tool says there is a problem" is not an argument — nobody can examine what they cannot
    see.
    """
    out = _convert({"comments": [{"severity": "high", "path": "a.py", "content": "x"}]})

    assert "may contain false positives" in out
    assert "verify it item by item" in out
    assert "does not hold" in out


def test_an_empty_scan_says_so_instead_of_producing_nothing():
    """Reporting no problems is a valid answer. An empty scan has to say so, rather than handing
    over empty material.
    """
    out = _convert({"comments": []})

    assert "found nothing" in out
    assert "do not force it" in out


def test_the_example_gives_every_participant_the_same_material():
    """The scan results go through `--file` (visible to everyone), not through briefing (private).

    The example used to give it to one participant through briefing, on the grounds of "creating
    information asymmetry". That was a scenario constructed for an experiment: in real use you
    have a scan report in hand and **there is no reason whatsoever to show it to only one of
    them**. An example configuration that demonstrates a usage the user will never have teaches
    them the wrong thing.
    """
    config = yaml.safe_load((EXAMPLE / "sesa.yaml").read_text(encoding="utf-8"))

    briefed = [p for p in config["participants"] if p.get("briefing")]
    assert briefed == [], (
        "the example must not demonstrate private material — the report goes to everyone"
    )
    assert len(config["participants"]) >= 2

    # Both documents have to say this. The example README exists in two languages, and
    # checking one leaves the half-truth "the English reader is told the report is shared
    # and the Chinese reader is not".
    for name, shared in (
        ("README.md", "every participant sees"),
        ("README.zh.md", "所有参与者都看得到"),
    ):
        readme = (EXAMPLE / name).read_text(encoding="utf-8")
        assert "--file" in readme, f"{name}: it has to say which channel the report goes through"
        assert shared in readme, name


def test_the_readme_warns_about_the_import_shadowing_trap():
    """Leaving PYTHONPATH out silently disables the entire evidence layer — that hole has to be in
    the documentation.
    """
    for name, silent in (("README.md", "silently disabled"), ("README.zh.md", "静默失效")):
        readme = (EXAMPLE / name).read_text(encoding="utf-8")
        assert "PYTHONPATH" in readme, name
        assert silent in readme, name


# --------------------------------------------------------------------------- # Against drift: the
# topic template must not hard-code facts readable from the repository
# --------------------------------------------------------------------------- #

#: Assertion shapes that go stale as the code changes. Hard-code them and the participants take a
#: false premise for true.
_STALE_CLAIMS = re.compile(
    r"现有\s*\d+\s*个测试"
    r"|共\s*\d+\s*条"
    r"|\d+\s*个测试(?:全绿|通过)"
    r"|前(?:两|三|四|五)轮"
    r"|(?:两|三|四|五|六)轮下来找出\s*\*{0,2}\d+"
    r"|the existing\s+\d+\s+tests"
    r"|\d+\s+(?:real\s+)?defects (?:over|across|in)\s+(?:two|three|four|five|six)\s+rounds"
    r"|(?:two|three|four|five|six)\s+rounds turned up\s+\*{0,2}\d+"
)


def test_the_readme_does_not_hardcode_the_finding_count_either():
    """One discipline has to cover every outlet, or it emerges from the next one.

    After fixing "do not hard-code derivable facts" in the example, within an hour I wrote "four
    rounds turned up 49 real defects" into the README — **the same error again through a
    different outlet**, which is exactly the pattern this project keeps observing ("calling the
    unmeasured a disagreement" was committed once at each of four places).
    """
    for name, heading in (
        ("README.md", "## We used it to review itself"),
        ("README.zh.md", "## 我们用它审了它自己"),
    ):
        readme = (EXAMPLE.parent.parent / name).read_text(encoding="utf-8")
        section = readme[readme.index(heading) :][:1800]

        found = _STALE_CLAIMS.findall(section)
        assert not found, f"{name} hard-codes a fact that will go stale: {found}"
        assert "test_bottom_lines" in section, f"{name}: it has to point at the living record"


def test_the_task_template_does_not_hardcode_derivable_facts():
    """Anything in the topic that can be read from the repository should not be copied.

    The first version of this example wrote "the existing 264 tests" and "22 items over the
    first two rounds", and two days later both numbers were wrong — while the participants had
    received a premise that claimed authority and was stale.
    Changed to point at `tests/test_bottom_lines*.py` and let them read the test names
    themselves.
    """
    task = "\n".join(
        (EXAMPLE / name).read_text(encoding="utf-8")
        for name in ("review-task.md", "review-task.zh.md")
    )

    found = _STALE_CLAIMS.findall(task)
    assert not found, (
        f"the topic template hard-codes a fact that will go stale: {found}. Point at a file in the repository and let the participants read it themselves."
    )


def test_the_task_template_points_at_the_living_record_instead():
    # The topic template exists in two languages, and both are really sent to participants.
    for name, how in (("review-task.md", "test names"), ("review-task.zh.md", "测试名")):
        task = (EXAMPLE / name).read_text(encoding="utf-8")
        assert "test_bottom_lines" in task, (
            f"{name}: it has to tell the participants where the fixed list is"
        )
        assert how in task, f"{name}: it has to say how to read it"


def test_the_task_template_still_licenses_finding_nothing():
    """ "Reporting no problems is a valid answer" — without that sentence, participants pad the list
    to have something to show.
    """
    for name, nothing, worse in (
        ("review-task.md", "If you find nothing", "worse than reporting none"),
        ("review-task.zh.md", "找不到", "比报零条更糟"),
    ):
        task = (EXAMPLE / name).read_text(encoding="utf-8")
        assert nothing in task, name
        assert worse in task, name


def test_the_referenced_record_files_actually_exist():
    """Pointing at a list that does not exist is worse than hard-coding a stale number."""
    records = sorted((EXAMPLE.parent.parent / "tests").glob("test_bottom_lines*.py"))

    assert records, (
        "the topic points at tests/test_bottom_lines*.py and the repository has none of them"
    )
    for path in records:
        assert "def test_" in path.read_text(encoding="utf-8")


def test_every_example_file_is_actually_in_the_repository():
    """Existing locally ≠ being in the repository.

    Measured: `.gitignore`'s `sesa.yaml` (meant to ignore the user's own project config)
    swallowed `examples/self-review/sesa.yaml` with it — **anyone cloning the repo does not get
    it**, and the whole example is broken. And the test of the time read the local filesystem,
    where the file existed, so it was green throughout. The textbook "it works on my machine".
    """
    tracked = subprocess.run(
        ["git", "ls-files", str(EXAMPLE.relative_to(EXAMPLE.parent.parent))],
        cwd=EXAMPLE.parent.parent,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,  # a hung git should not drag the whole test suite down with it
    ).stdout.split()
    on_disk = {p.name for p in EXAMPLE.iterdir() if p.is_file()}
    in_git = {Path(p).name for p in tracked}

    missing = on_disk - in_git
    assert not missing, (
        f"these files exist only locally and are not in the repository: {sorted(missing)}. "
        "Anyone cloning it does not get them. Check .gitignore."
    )


def test_malformed_tool_output_fails_loudly_not_with_a_traceback():
    """This script consumes an external tool's output, whose format is not ours to control."""
    got = subprocess.run(
        [sys.executable, str(EXAMPLE / "to_briefing.py")],
        input="not json at all",
        capture_output=True,
        text=True,
    )

    assert got.returncode == 1
    assert "cannot read the input JSON" in got.stderr
    assert "Traceback" not in got.stderr


def test_a_toplevel_array_is_not_a_crash():
    """Some tools emit a bare array. It should neither crash nor pretend to have found something."""
    got = subprocess.run(
        [sys.executable, str(EXAMPLE / "to_briefing.py")],
        input="[1, 2, 3]",
        capture_output=True,
        text=True,
        check=True,
    )

    assert "found nothing" in got.stdout
