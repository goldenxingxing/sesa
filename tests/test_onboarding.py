"""A new user's path in: `sesa init` → `sesa doctor` → can they start a run.

This is the side that had **no test coverage at all**. All 618 tests were testing internal
quality, while this project's goal is "open source, easy for other people to pick up" — and not
one test touched that side.

The wizard only runs in **a real terminal** (rich's Prompt reads a tty), so this drives real
processes through `pty.openpty()`. Verifying through a pipe is not enough: `sesa run` once
crashed for certain under a real TTY, and every check of mine went through a pipe, so it stayed
green.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SESA = Path(sys.executable).parent / "sesa"

pytestmark = pytest.mark.skipif(
    not SESA.exists(), reason="需要装好的 sesa 可执行文件（uv pip install -e .）"
)


def _drive(
    keys: list[str], home: Path, timeout: float = 45.0, lang: str = "zh"
) -> tuple[str, int | None]:
    """Run `sesa init` in a real pseudo-terminal, feeding the keys in as prompted.

    ``lang`` defaults to Chinese here: these assertions are written against the Chinese wording,
    and Chinese is the **catalogue** path — which this exercises exactly.
    The default language (English) is covered separately by test_the_wizard_runs_in_english.
    Both are needed: testing one leaves nobody knowing when the other breaks.
    """
    env = {
        **os.environ,
        "SESA_LANG": lang,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        # conftest points SESA_CONFIG_DIR at a session-level isolated directory (so the tests cannot
        # read the user's real configuration). What is wanted here is **this test's own** directory,
        # which takes priority and has to be overridden explicitly — otherwise the wizard writes
        # elsewhere and every assertion falls flat.
        "SESA_CONFIG_DIR": str(home / ".config" / "sesa"),
        "TERM": "xterm",
        "COLUMNS": "100",
    }
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(SESA), "init"], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
    )
    os.close(slave)
    out, pending, deadline = b"", list(keys), time.time() + timeout
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif pending:
                os.write(master, (pending.pop(0) + "\n").encode())
            elif proc.poll() is not None:
                break
            if proc.poll() is not None and not ready:
                break
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)
    return out.decode("utf-8", "replace"), proc.returncode


def test_the_wizard_completes_and_writes_a_loadable_config(tmp_path):
    """When the wizard finishes it has to leave a configuration that **really loads**.

    Writing one that cannot be read back is worse than writing none: the user believes it is
    configured.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)

    # decline every installed CLI (what is installed on this machine is uncertain and the test must
    # not depend on it), add one DeepSeek, choose "skip for now" for the credential, take the
    # defaults for the rest, and do not verify at the end.
    out, code = _drive(["n"] * 6 + ["1", "", "", "3", "", "", "", "", "n", ""], home)

    assert code == 0, f"the wizard did not finish normally (exit code {code}):\n{out[-1500:]}"
    written = home / ".config" / "sesa" / "config.yaml"
    assert written.exists(), f"no configuration was written:\n{out[-1500:]}"

    from sesa import config as cfg

    loaded = cfg.load(written)
    assert loaded.participants, "the configuration written holds not one participant"
    assert loaded.max_rounds > 0


def test_the_wizard_never_writes_a_secret_into_the_config(tmp_path):
    """Credentials go to the keyring or an environment variable only, and **never land in plaintext**."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    _drive(["n"] * 6 + ["1", "", "", "3", "", "", "", "", "n", ""], home)

    written = home / ".config" / "sesa" / "config.yaml"
    text = written.read_text(encoding="utf-8") if written.exists() else ""
    assert "api_key:" not in text
    assert "sk-" not in text
    # having chosen "skip for now", do not write a fake api_key_env — doctor would only say "cannot
    # read it" and the user would have no idea why.
    assert "<无>" not in text


def test_validation_errors_speak_the_same_language_as_the_prompt(tmp_path):
    """A user who has just been guided all the way through in Chinese, meeting an English sentence at
    the moment they get stuck, concludes they have hit a program error rather than an input error.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    out, _ = _drive(["n"] * 6 + ["", "", "abc", "4", "900", "n", ""], home)
    assert "valid integer number" not in out, (
        "rich's English validation message leaked into the Chinese interface"
    )


def test_doctor_explains_why_each_participant_is_unusable(tmp_path):
    """`doctor`'s value is not in saying "unusable" but in saying **why**.

    Measured, it recovers the failure reason an agent CLI wrote to **stdout** (claude's "Not logged
    in · Please run /login" appears only there) — reporting "exit code 1, stderr empty" throws away
    the answer we are holding.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    _drive(["n"] * 6 + ["1", "", "", "3", "", "", "", "", "n", ""], home)

    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        # the same directory as _drive, or doctor reads the empty configuration in the isolated one.
        "SESA_CONFIG_DIR": str(home / ".config" / "sesa"),
        "TERM": "xterm",
        "COLUMNS": "120",
    }
    done = subprocess.run(
        [str(SESA), "doctor"], env=env, capture_output=True, text=True, timeout=90
    )
    # doctor prints the reason inside a table cell, wrapped to the column width, with a vertical bar
    # and padding at the end of each line — flatten the table characters and whitespace together
    # before comparing, or the assertion is really guarding a particular terminal width.
    flat = " ".join(re.sub(r"[│┃|]", " ", done.stdout).split())
    assert "no credential configured" in flat, (
        f"it does not say what is missing:\n{done.stdout[-800:]}"
    )


# ── the event stream is the only source of truth, so it has to work on the day it breaks ─ #


def test_one_corrupt_line_does_not_destroy_the_whole_run(tmp_path):
    """The process is SIGKILL-ed mid-write, which no signal handler can stop.

    `read_events` used to raise on a broken line, so **one bad byte made a whole deliberation
    unreadable forever** — report could not read it, resume could not continue it, eval could not
    compute over it, while 99.9% of the content was perfectly good.
    """
    import warnings

    from sesa.record import read_events

    (tmp_path / "events.jsonl").write_text(
        '{"t": "run.start", "ts": 1.0}\n'
        '{"t": "turn.st\n'  # torn in half by a kill
        '{"t": "verdict.final", "ts": 2.0}\n',
        encoding="utf-8",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        events = read_events(tmp_path)

    assert [e["t"] for e in events] == ["run.start", "verdict.final"]
    assert caught, "skipping something without a word is worse than raising"
    assert "incomplete" in str(caught[0].message)


def test_a_run_with_no_readable_events_is_still_not_a_crash(tmp_path):
    (tmp_path / "events.jsonl").write_text("{坏\n{也坏\n", encoding="utf-8")
    import warnings

    from sesa.record import read_events

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        assert read_events(tmp_path) == []


def test_the_abort_record_lands_even_when_the_normal_write_path_is_unusable(tmp_path):
    """The abort handler can land in the middle of an ``emit``.

    Calling ``emit`` then re-enters the same ``BufferedWriter``, and CPython raises
    ``RuntimeError: reentrant call`` — which the handler's ``finally: raise SystemExit`` swallows
    along with everything else. **The result is that the abort record is silently lost, and leaving
    that record is the only reason this handler exists.**

    ``emit_abort`` goes to the raw fd and never touches Python's buffer, so it has no reentrancy
    problem **by construction**. This verifies that property by making the normal write path raise
    — which tests the same thing as engineering a real signal race, and does so deterministically.

    (Stated honestly: I could not faithfully reproduce CPython's reentrancy RuntimeError with a
    Python-level mock. This fix **eliminates the risk by construction** rather than patching a
    reproduced failure.)
    """
    import json

    import sesa.events as ev
    from sesa.record import Recorder

    recorder = Recorder(tmp_path, "run1")
    recorder.emit(
        ev.RunStart(run_id="run1", task="t", participants=["a"], protocol="debate", max_rounds=1)
    )

    def unusable(*_args, **_kwargs):
        raise RuntimeError("reentrant call inside <_io.BufferedWriter>")

    recorder._events.write = unusable
    assert recorder.emit_abort(ev.RunAborted(reason="收到 SIGTERM，运行被外部中止")) is True

    recorder._events.write = lambda *_a, **_k: None
    recorder.close()

    lines = [
        json.loads(line)
        for line in (tmp_path / "runs" / "run1" / "events.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    ]
    assert {e["t"] for e in lines} == {"run.start", "run.aborted"}


def test_emit_abort_reports_failure_instead_of_swallowing_it(tmp_path):
    """Losing the abort record in silence leaves only "verdict.final is missing" to guess from
    afterwards — and guessing misses cases, and cannot say who aborted it.
    """
    import sesa.events as ev
    from sesa.record import Recorder

    recorder = Recorder(tmp_path, "run1")
    recorder._events.close()  # the fd is closed, so the write fails
    assert recorder.emit_abort(ev.RunAborted(reason="SIGTERM")) is False


# ── what the first real user hit immediately: "why can I only pick one" ───────── #


def test_the_api_step_says_it_accepts_more_than_one(tmp_path):
    """Adding API models is a **loop** and several can be added. And the wording made it look like
    only one could be picked.

    The original was "pick one (Enter to finish)" — which reads as "only one allowed" while it
    means "enter one number at a time". The first real user asked about this straight away.

    The loop itself is right; everything wrong was in how it was said:
      1. "pick one" is ambiguous
      2. what was already added stays in the list unmarked, so there is no sign the last step worked
      3. "Enter to skip" is said once at the start and never repeated after one is added, so there
         is no reminder of how to finish
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)

    # decline every CLI, add two API models (1, 2), then Enter to finish
    out, code = _drive(
        ["n"] * 6 + ["1", "", "", "3", "2", "", "", "3", "", "", "", "", "n", ""], home
    )
    assert code == 0, f"the wizard did not run to the end:\n{out[-1200:]}"

    assert "可加多个" in out, "the opening does not say several can be added"
    assert "可一次多个" in out, "it has to say several numbers can be entered at once"
    assert "直接回车结束" in out, "every round has to say how to finish"
    assert "（已加入）" in out, (
        "what was added is unmarked, so the user cannot tell whether the last step worked"
    )

    from sesa import config as cfg

    loaded = cfg.load(home / ".config" / "sesa" / "config.yaml")
    ids = {p.id for p in loaded.participants}
    assert len(ids) >= 2, f"the loop did not work; only {ids} were added"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1 ,2 ,5", [1, 2, 5]),  # what the first real user actually typed
        ("1,2,5", [1, 2, 5]),
        ("1 2 5", [1, 2, 5]),
        ("1、2、5", [1, 2, 5]),  # an ideographic comma
        ("1，2，5", [1, 2, 5]),  # a full-width comma
        ("3", [3]),
        ("1,,2", [1, 2]),
    ],
)
def test_the_api_step_accepts_several_numbers_at_once(raw, expected):
    """Number a list and people will want to enter several at once.

    The first real user typed exactly `1 ,2 ,5`, and only a single number was accepted, answering
    "please enter a number from the list". **Their instinct was right; something was missing here.**
    """
    from sesa.wizard import _parse_choices

    assert _parse_choices(raw, 5) == (expected, [])


@pytest.mark.parametrize("raw,bad", [("1,9", ["9"]), ("abc", ["abc"]), ("1,x,2", ["x"])])
def test_an_unrecognised_piece_cancels_the_whole_input(raw, bad):
    """**One unrecognised entry and the whole line does nothing.**

    Partial success leaves people unsure how many actually went in, and every addition in this step
    means pasting an API key — getting it wrong means cleaning up. The costs are asymmetric.
    """
    from sesa.wizard import _parse_choices

    _, rejected = _parse_choices(raw, 5)
    assert rejected == bad


# ── the first real user's second misunderstanding: thinking step two configures the CLI's key ─ #


def test_the_wizard_says_the_clis_need_no_key_from_us(tmp_path):
    """The two lists overlap by vendor (Claude Code ↔ Claude API, Kimi CLI ↔ Kimi, DeepSeek Harness ↔
    DeepSeek), and the wizard never said plainly that they are **two different things**.

    The first real user took "add an API model" to be configuring credentials for the claude/kimi
    above, pasted keys twice for nothing, and ended up with two extra independent participants, one
    of which was unusable because the key was wrong.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    out, _ = _drive(["n"] * 6 + ["", "", "", "", "n", ""], home)

    assert "自己的登录" in out, (
        "it does not say the CLIs use their own accounts and need no key from us"
    )
    assert "另外新增的参与者" in out, (
        "it does not say step two adds a participant rather than giving step one a key"
    )
    assert "不是" in out and "配 key" in out


def test_adding_an_api_model_whose_vendor_is_already_at_the_table_asks_first(tmp_path):
    """Point out the collision on the spot, and **do not add by default**.

    A user pressing Enter for the default should get "do not add a duplicate" — because adding a
    duplicate is precisely what one does under the misunderstanding.
    """
    import shutil

    if not shutil.which("claude"):
        pytest.skip("本机没装 claude CLI，构造不出撞车场景")

    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    # add the claude CLI (y + the default stance), decline the rest, then choose 5 = Claude API
    out, _ = _drive(["y", "", "n", "n", "n", "n", "5", "n", "", "", "", "", "n", ""], home)

    assert "已经在桌上了" in out, "the same vendor is already on the table and there was no warning"
    assert "另一个独立参与者" in out
    assert "确定要加吗" in out

    from sesa import config as cfg

    written = home / ".config" / "sesa" / "config.yaml"
    ids = {p.id for p in cfg.load(written).participants} if written.exists() else set()
    assert "anthropic" not in ids, "the default answer should be 'do not add'"


# ── re-running init must offer a way to start over ───────────────────────────── #


def _seeded(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "sesa").mkdir(parents=True)
    (home / ".config" / "sesa" / "config.yaml").write_text(
        "version: 1\n"
        "participants:\n"
        '  - {id: old_a, adapter: cli, command: ["echo","x"], prompt: stdin}\n'
        '  - {id: old_b, adapter: cli, command: ["echo","x"], prompt: stdin}\n'
        "protocol: debate\n",
        encoding="utf-8",
    )
    return home


def test_rerunning_init_offers_to_start_over(tmp_path):
    """It used to offer only "add more? y/n": y could only pile on, n exited.

    **Once the first attempt was wrong (and the first attempt is easy to get wrong), the user's only
    remaining route was to delete the config file by hand** — and the wizard never told them where
    it was or what to remove.
    """
    home = _seeded(tmp_path)
    out, code = _drive(["2", "n", "n", "n", "", "", "", "", "n", ""], home)
    assert code == 0, out[-800:]
    assert "清空重配" in out, "no 'start over' option was offered"

    from sesa import config as cfg

    loaded = cfg.load(home / ".config" / "sesa" / "config.yaml")
    assert {p.id for p in loaded.participants} == set(), f"not cleared: {loaded.participants}"


def test_choosing_to_exit_changes_nothing(tmp_path):
    """Choosing "change nothing" really has to change not one character."""
    home = _seeded(tmp_path)
    before = (home / ".config" / "sesa" / "config.yaml").read_text(encoding="utf-8")
    out, _ = _drive(["3"], home)
    assert "没有改动" in out
    assert (home / ".config" / "sesa" / "config.yaml").read_text(encoding="utf-8") == before


def test_clearing_does_not_touch_the_keyring(tmp_path):
    """Clearing touches only the config file. **The credentials stay** — otherwise the user has to dig
    out and paste every key again, and they have most likely revoked the originals already.
    """
    home = _seeded(tmp_path)
    out, _ = _drive(["2", "n", "n", "n", "", "", "", "", "n", ""], home)
    assert "钥匙串里的凭据没有动" in out


def test_an_existing_keyring_entry_is_offered_for_reuse():
    """After clearing and reconfiguring, a participant of the same name still has its credential in the
    keyring. Without asking, the user has to paste it again.
    """
    import inspect

    from sesa import wizard

    source = inspect.getsource(wizard._store_credential)
    assert "keyring_has" in source
    assert "Reuse it?" in source


def test_keyring_has_never_raises():
    """This decides only whether to ask the user about reusing it — being wrong costs one extra
    question, whereas raising would abort the whole wizard.
    """
    from sesa.credentials import keyring_has

    assert keyring_has("绝不可能存在的参与者-xyz") is False


def test_already_added_is_not_reported_as_not_detected(tmp_path):
    """ "They are all added already" is not "none were detected".

    The user had all three CLIs in the configuration, re-ran init, chose "add more", and saw "no
    installed agent CLI was detected" — from which the reasonable inference is that sesa cannot find
    their claude, so they go and check PATH, and there is nothing wrong at all.

    Writing "there is nothing to do" as "there is nothing" is this project's recurring fault in
    another place.
    """
    from sesa import config as cfg

    installed = cfg.detect_installed_clis()
    if not installed:
        pytest.skip("本机没装任何 agent CLI，构造不出「已全部加入」的场景")

    # **Write every detected one into the configuration** — with only one written, the rest can
    # still be added and this branch is not the one taken. The fixture has to be built from what is
    # actually on this machine.
    entries = "".join(
        f'  - {{id: {key}, adapter: cli, command: ["echo","x"], prompt: stdin}}\n'
        for key in installed
    )
    home = tmp_path / "home"
    (home / ".config" / "sesa").mkdir(parents=True)
    (home / ".config" / "sesa" / "config.yaml").write_text(
        f"version: 1\nparticipants:\n{entries}protocol: debate\n", encoding="utf-8"
    )
    out, _ = _drive(["1", "", "", "", "", "n", ""], home)

    assert "都已经在配置里了" in out
    assert "没有探测到已安装的 agent CLI。" not in out, (
        "wrote 'they are already added' as 'none were detected'"
    )


def test_when_nothing_is_installed_it_says_what_it_looked_for(tmp_path):
    """When there really are none, say which ones were looked for — otherwise the user does not know
    what to install.
    """
    import inspect

    from sesa import wizard

    source = inspect.getsource(wizard.run_wizard)
    assert "looked for claude" in source


def test_the_wizard_runs_in_english_by_default(tmp_path):
    """**The default-language path has to be walked too.**

    The assertions above are written against the Chinese wording and go through the catalogue;
    testing Chinese alone leaves nobody knowing when English (that is, the default) breaks.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    out, code = _drive(["n"] * 6 + ["", "", "", "", "n", ""], home, lang="en")
    assert code == 0, out[-800:]
    # rich inserts colour codes around fragments like numbers, so matching a whole sentence gets cut
    # off. Pick a fragment with no interpolation that colouring will not break up.
    assert "A deliberation needs at least" in out
    assert "Deliberation settings" in out
    # Do not assert on the wording in the middle: one misaligned keystroke has the prompt repeat and
    # run into the timeout truncation, and the assertion then tests "were my keystrokes right"
    # rather than "is the language right".
    assert "议事" not in out, "Chinese leaked into the English interface"
