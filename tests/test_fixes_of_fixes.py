"""Problems found by reviewing **the fixes themselves**.

In one day the author at least six times "fixed one and created one" or "plugged half the
door". So that batch of fixes (23 files, 413 lines) was taken out
and reviewed on its own — the question being not "what other bugs are there" but: **were
they fixed thoroughly? Did the fix break anything? Did it overcorrect?**

Result: one overcorrection and one half-plugged door, both locked down here.
"""

from __future__ import annotations

import pytest

from sesa.judge import verify_quote
from sesa.patch import extract_files

TRANSCRIPT = (
    "## r00_p0_alice_draft\n\nalice 说了一句足够长的话\n\n"
    "## r00_p0_alice_bot_draft\n\nalice_bot 说了一句足够长的话\n\n"
    "## r01_p0_alice_revise\n\nalice 第二轮说的另一句话"
)


@pytest.mark.parametrize(
    "info,expected",
    [
        ("python name=a.py", ["a.py"]),
        ("name=b.py", ["b.py"]),  # hard against the fence, perfectly legal and commonly
        # produced by models
        ("python  name=c.py", ["c.py"]),
        ("python path=f.py", ["f.py"]),
        ("python filename=d.py", []),
        ("python x-name=e.py", []),
        ("python", []),
    ],
)
def test_the_name_attribute_regex_is_neither_too_loose_nor_too_tight(info, expected):
    """**The overcorrection.**

    The original defect: `filename=x.py` was taken for `name=` (a missing word boundary).
    My fix required "preceded by start-of-line or whitespace" — and ```name=a.py, hard against
    the fence, is preceded by neither, so **the file was silently discarded**.
    It swung from missing one to a false positive, and the false positive is worse: the user's
    file vanishes.
    """
    assert list(extract_files(f"```{info}\nBODY\n```")) == expected


@pytest.mark.parametrize(
    "speaker,quote,expected",
    [
        ("alice", "alice 说了一句足够长的话", True),
        ("alice", "alice 第二轮说的另一句话", True),
        ("alice", "alice_bot 说了一句足够长的话", False),
        ("alice_bot", "alice_bot 说了一句足够长的话", True),
        ("alice_bot", "alice 说了一句足够长的话", False),
    ],
)
def test_speaker_matching_handles_ids_that_contain_each_other(speaker, quote, expected):
    """**The half-plugged door.**

    The check "a quotation must be from that person" had just been fixed, and the test was
    `speaker in line` — `alice` swallows `alice_bot`'s turns, and **the same putting-words-in-
    someone's-mouth is back in another form**.

    Nor can it simply be split on `_`: splitting `alice_bot` yields `alice`.
    The block heading is `## r{round}_p{phase}_{id}_{kind}`; drop the two fixed leading segments
    and the trailing one, and what remains as a whole is the id.
    """
    assert verify_quote(quote, TRANSCRIPT, speaker=speaker) is expected


def test_a_failed_write_leaves_no_staging_file(tmp_path):
    """A failed temp file has to be cleared, or the next scan reads it into the context as part of
    the working copy.
    """
    from sesa.patch import apply_files

    blocked = tmp_path / "impl.py"
    blocked.mkdir()  # the target is a directory → the write is bound to fail

    result = apply_files("```python name=impl.py\nBODY\n```", tmp_path)

    assert result.rejected, "a failure has to be reported honestly"
    assert not list(tmp_path.glob("*.sesa-partial")), (
        "a half-written file must not be left in the working copy"
    )


def test_self_tests_are_identified_by_their_field_not_by_a_character_in_the_command():
    """Using `"×" not in item.cmd` to identify a self-test is too fragile — the command is the
    user's.
    """
    from sesa.state import EvidenceRecord

    own = EvidenceRecord(
        participant="a", cmd="pytest -q（自测 × 无关字符）", exit_code=0, summary="ok"
    )
    cross = EvidenceRecord(participant="a", cmd="pytest -q", exit_code=0, summary="ok", against="b")

    assert own.is_self_test, (
        "a × that happens to be in the command must not turn it into a cross-test"
    )
    assert not cross.is_self_test


# --------------------------------------------------------------------------- # The third fix to the
# same piece of code: pointed out by deepseek in a deliberation
# --------------------------------------------------------------------------- #

NESTED_HEADINGS = (
    "## r00_p0_alice_draft\n\n"
    "我主张用 Postgres。\n\n"
    "## 理由\n\n"  # ← a subheading the participant wrote themselves, extremely common
    "并发写入是硬需求，SQLite 单写者撑不住\n\n"
    "## r00_p0_alice_bot_draft\n\n"
    "alice_bot 说了一句足够长的话\n\n"
    "## r00_p0_bob_draft\n\n"
    "我主张 SQLite 运维简单"
)


@pytest.mark.parametrize(
    "speaker,quote,expected",
    [
        ("alice", "我主张用 Postgres", True),
        ("alice", "并发写入是硬需求，SQLite 单写者撑不住", True),  # after the subheading
        ("alice", "我主张 SQLite 运维简单", False),
        ("alice", "alice_bot 说了一句足够长的话", False),
        ("alice_bot", "alice_bot 说了一句足够长的话", True),
        ("bob", "我主张 SQLite 运维简单", True),
    ],
)
def test_a_participants_own_markdown_headings_do_not_split_their_speech(speaker, quote, expected):
    """**This is the third fix to the same piece of code**, the first two both mine.

    1. The original defect: a quotation passed the check as long as it appeared anywhere in the
       text (putting words in someone's mouth)
    2. My fix: split by speaker — but the test was `speaker in line`, and `alice` swallows
       `alice_bot`
    3. Fixed again: take the id from the block heading's structure — but **any `## ` was taken
       to mean the speaker changed**

    The third was found by **claude** in the deliberation, in round 0 (the label R3 is theirs
    too); deepseek quoted that label in round 1 and ranked it "most severe". The author briefly
    recorded it as "deepseek pointed it out" — having read deepseek's short stance card first
    and **not opened claude's 9,673 characters**.
    Whoever you read first is easy to credit.

    Claude's analysis went one level deeper on the cause: participants writing `## Premises` /
    `## Conclusion` inside their own turns is **required by our own prompts**, while the checker
    broke on `##` — an external tool cannot find this, because it does not know what this
    deliberation is asking for.
    This check exists to stop the judge inventing quotations, and that change would have it kill
    honest ones instead.

    The correct test: the block heading has the fixed shape `## r{round}_p{phase}_{id}_{kind}`;
    recognise only that shape, and every other `##` is prose.
    """
    assert verify_quote(quote, NESTED_HEADINGS, speaker=speaker) is expected


# --------------------------------------------------------------------------- # critical: the atomic
# write I introduced to fix "a failed write leaves a truncated file" created a sandbox escape
# --------------------------------------------------------------------------- #


def test_a_path_pointing_at_the_workspace_root_cannot_touch_its_parent(tmp_path):
    """**A model writing ```name=. can delete a file outside the workspace.**

    `_is_safe(root, root)` is true — a path is its own descendant — so `name=.` passes the
    out-of-bounds check. And staging is `target.with_name(target.name + ".sesa-partial")`, so
    when target is root it lands in root's **parent**:

        work/               ← the workspace
        work.sesa-partial   ← staging lands here, outside the sandbox

    `write_text` creates or truncates it, `replace` then raises IsADirectoryError, and the
    exception handler unlinks it — **and a file of that name outside the sandbox is deleted**.

    This was created by introducing the atomic write to fix "a failed write leaves a truncated
    original", and it is far more serious than the problem it fixed.
    """
    from sesa.patch import apply_files

    root = tmp_path / "work"
    root.mkdir()
    bystander = tmp_path / "work.sesa-partial"
    bystander.write_text("别人的文件，不该被动", encoding="utf-8")

    result = apply_files("```python name=.\nEVIL\n```", root)

    assert bystander.exists(), "a file outside the sandbox was deleted"
    assert bystander.read_text(encoding="utf-8") == "别人的文件，不该被动"
    assert result.applied == []
    assert result.rejected and "the working directory itself" in result.rejected[0][1]


@pytest.mark.parametrize(
    "raw_path,ok",
    [(".", False), ("sub/..", False), ("../escape.py", False), ("ok.py", True), ("a/b.py", True)],
)
def test_only_paths_that_land_inside_the_workspace_are_written(tmp_path, raw_path, ok):
    from sesa.patch import apply_files

    root = tmp_path / "work"
    root.mkdir()

    result = apply_files(f"```python name={raw_path}\nX\n```", root)

    assert bool(result.applied) is ok
    assert not list(tmp_path.glob("*.sesa-partial")), (
        "under no circumstances should a staging file be left outside the sandbox"
    )
