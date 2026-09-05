"""What the README says has to be true.

How it started: the README's first line was `pip install sesa`, and the package **is not on
PyPI at all** — false for every reader. Meanwhile the "status" section said plainly "not
released yet", so the document contradicted itself. And not one of 612 tests covered this,
because they were all testing internal quality.

**A README that stops the reader at step one is worse than no README**: they conclude the
whole project is of that standard.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Both READMEs, with the section headings each uses. **Every claim has to be checked in both**
#: — a sentence true in one language and false in the other is exactly the shape of untruth
#: this file exists to catch, and the translated one is the easier to forget.
READMES = {
    "README.md": ("## Quick start", "## The core idea"),
    "README.zh.md": ("## 快速开始", "## 核心概念"),
}
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(READMES))
def test_the_quickstart_does_not_claim_a_pypi_release_that_does_not_exist(name):
    """Do not write `pip install sesa` before it is released."""
    text = _read(name)
    start, end = READMES[name]
    quickstart = text[text.index(start) : text.index(end)]
    assert "pip install sesa" not in quickstart
    assert "uv tool install sesa\n" not in quickstart


def test_nothing_in_the_repo_tells_the_user_to_install_from_pypi():
    """**Checking only the "quick start" section is not enough.**

    There used to be 7 places saying `pip install 'sesa[tui]'` — one in the README and six in
    **runtime error messages** (the wizard, doctor's footnote, the _fail on a missing
    dependency). And sesa is not published to PyPI, so that command returns No matching
    distribution found.

    A runtime hint hurts more than the README: the user is already stuck on "keyring is not
    installed", follows the hint, and now has two problems, the second of which looks like the
    project cannot be installed at all.
    The previous version of this test scanned only the quick-start section and caught none of
    those six.
    """
    import re

    offenders = []
    for path in [ROOT / "README.md", ROOT / "README.zh.md", *(ROOT / "src").rglob("*.py")]:
        # `_install.py`'s documentation is about **this very problem**, and quoting the bad command
        # there is right. The test is whether it was handed over as the only instruction, so a line
        # that already explains the source-checkout form is let through, as is this module itself.
        if path.name == "_install.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # The source-checkout form has one phrasing in each language; the test is "was it said
            # on the same line", regardless of which language said it.
            source_note = "from a source checkout" in line or "从源码装" in line
            if re.search(r"pip install ['\"]?sesa\[", line) and not source_note:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, (
        "these places send the user to PyPI for a package that is not released:\n  "
        + "\n  ".join(offenders)
    )


def test_the_quickstart_and_the_status_section_agree():
    """ "Status" says not released while "quick start" tells people to install from PyPI — when a
    document contradicts itself, the reader believes the first command and gets stuck there.
    """
    for name in sorted(READMES):
        text = _read(name)
        unreleased = "尚未发布" in text or "not yet published" in text
        from_source = "git clone" in text or "git+http" in text
        assert unreleased == from_source, (
            f"{name}: the release status and the installation instructions do not match"
        )


def _all_extras_present() -> bool:
    """Whether the optional dependencies are complete."""
    import importlib.util

    return all(
        importlib.util.find_spec(m) is not None for m in ("textual", "sentence_transformers")
    )


@pytest.mark.skipif(
    not _all_extras_present(),
    reason="收集到的测试数取决于装了哪些可选依赖；只在装齐的环境里校对这个数字",
)
def test_the_claimed_test_count_matches_reality():
    """The test count in the README goes stale, and **a stale number is no different from a lie**.

    Measured: it sat at 486 for a long time while the real figure was already 612.

    .. note::
       **This one must be guarded.** The number of tests collected depends on which optional
       extras are installed (missing either `[tui]` or `[semantic]` means the corresponding test
       module is not collected at all), so it goes red in an environment missing one — which is
       exactly the fault I had just fixed by guarding test_semantic, and committed again
       verbatim in this new test an hour later.
       A test that goes red because "you did not install an optional extra" leaves a contributor
       thinking the project is broken.
    """
    # 只校对**确实声称了**数字的那些。一份不写测试数的 README 没有可过期的东西，
    # 这条测试守的是「写了就必须是真的」，不是「必须写」。
    claims = {
        name: hit
        for name, hit in (
            ("README.md", re.search(r"with (\d+) tests", _read("README.md"))),
            ("README.zh.md", re.search(r"(\d+) 个测试", _read("README.zh.md"))),
        )
        if hit is not None
    }
    if not claims:
        pytest.skip("两份 README 都不声称测试数，没有会过期的数字")

    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    got = re.search(r"(\d+) tests? collected", collected.stdout)
    assert got, f"cannot count the tests: {collected.stdout[-300:]}"

    real_n = int(got.group(1))
    wrong = {n: int(h.group(1)) for n, h in claims.items() if int(h.group(1)) != real_n}
    assert not wrong, (
        f"these READMEs' test counts do not match reality ({real_n}): {wrong}. Change the README, not this test."
    )


@pytest.mark.parametrize("command", ["sesa init", "sesa doctor", "sesa run"])
def test_every_command_the_quickstart_names_actually_exists(command):
    """The commands named in the quick start have to really exist.

    A measured counterexample: my smoke script ran `sesa protocols` and reported "passed", while
    that command did not exist at all — the script only looked for a Traceback.
    """
    from typer.testing import CliRunner

    from sesa.cli import app

    name = command.split()[1]
    result = CliRunner().invoke(app, [name, "--help"])
    assert result.exit_code == 0, (
        f"`{command}` does not exist or will not start: {result.output[:200]}"
    )


@pytest.mark.parametrize("name,lang", [("README.md", "en"), ("README.zh.md", "zh")])
def test_the_readme_excerpt_uses_the_labels_the_report_really_renders(name, lang):
    """README 里那段 `RESULT.md` 摘录，字段名必须与渲染器实际输出的一致。

    摘录是手写的，渲染器是代码——两者会走散，而且**走散了看不出来**：读者以为
    自己在看产品的输出，实际看的是我编的措辞。第一版就走散了：README 写着
    「What is needed to decide」「Next step」，而 `report.py` 渲染的是
    「What would settle it」「Next」。
    """
    from sesa.report import render_result
    from sesa.types import Disagreement, Outcome, Result

    # **交付物跟着任务语言走**，不跟界面走——所以要拿一个该语言的任务，
    # 而不是把界面切过去。第一版拿英文任务去验中文标签，渲染器（正确地）
    # 给了英文，测试红在自己的夹具上。
    result = Result(
        task={"en": "which database?", "zh": "该用哪个数据库？"}[lang],
        run_id="x",
        outcome=Outcome.DEADLOCK.value,
        conclusion="",
        drafted_by=None,
        rounds_used=2,
        disagreements=[
            Disagreement(
                topic="scale",
                positions={"a": "one", "b": "two"},
                reasons={"a": "r1", "b": "r2"},
                root_cause="differing premises",
                decisive_question="which?",
            )
        ],
    )
    rendered = render_result(result)

    readme = _read(name)
    # 取出渲染器真正用的三个粗体标签，逐个要求 README 里也有
    labels = re.findall(r"\*\*([^*]+)\*\*(?=[:：])", rendered)
    wanted = [x for x in labels if x not in ("a", "b")]
    assert wanted, "渲染器不再用粗体标签了？那就把这条测试改掉"
    missing = [x for x in wanted if f"**{x}**" not in readme]
    assert not missing, (
        f"{name} 的 RESULT.md 摘录用了渲染器没有的标签；缺少：{missing}。"
        "摘录要照抄产品的输出，不要自己编措辞。"
    )
