"""Extracting files from model output and writing them to disk.

An API model can only produce text and cannot write files. Without this layer, code tasks
would be limited to agent CLIs — while it is exactly the **weaker models** most likely to
show what debate is worth, and weaker models usually have only an API.
"""

from __future__ import annotations

import pytest

from sesa import patch
from sesa.patch import apply_files, extract_files


def test_extracts_only_blocks_with_a_target_path():
    """A code block with no target file named is an illustration and must not be written out."""
    text = (
        "```python name=a.py\nA\n```\n"
        "举个例子：\n```python\nprint('无标注')\n```\n"
        "```python path=b.py\nB\n```\n"
    )
    assert extract_files(text) == {"a.py": "A\n", "b.py": "B\n"}


def test_last_occurrence_wins():
    """Models commonly give a fragment first and the complete version afterwards; taking the last
    is closer to their final intent.
    """
    text = "```python name=a.py\n片段\n```\n```python name=a.py\n完整版\n```\n"
    assert extract_files(text) == {"a.py": "完整版\n"}


def test_writes_files_into_the_workspace(tmp_path):
    result = apply_files("```python name=pkg/mod.py\nX = 1\n```", tmp_path)
    assert result.ok
    assert (tmp_path / "pkg" / "mod.py").read_text("utf-8") == "X = 1\n"
    assert result.applied[0].created is True


def test_path_escape_is_rejected(tmp_path):
    """A model may well emit ../../etc/passwd. Writing that out is more than a bug."""
    result = apply_files(
        "```python name=../outside.py\nbad\n```\n```python name=/tmp/abs.py\nbad\n```",
        tmp_path,
    )
    assert result.applied == []
    reasons = dict(result.rejected)
    assert reasons["../outside.py"] == "escapes the working directory"
    assert reasons["/tmp/abs.py"] == "absolute path"
    assert not (tmp_path.parent / "outside.py").exists()


def test_new_files_can_be_disallowed(tmp_path):
    (tmp_path / "known.py").write_text("旧内容", "utf-8")
    result = apply_files(
        "```python name=known.py\n新内容\n```\n```python name=fresh.py\nX\n```",
        tmp_path,
        allow_new=False,
    )
    assert [a.path for a in result.applied] == ["known.py"]
    assert result.rejected == [("fresh.py", "creating new files is not allowed")]


def test_no_blocks_is_reported_not_silently_ignored(tmp_path):
    """It has to be visible when not one thing was extracted — a silent no-op leaves people
    thinking the write succeeded.
    """
    result = apply_files("我思考了一下，但没有给出文件。", tmp_path)
    assert result.ok is False
    assert result.applied == [] and result.rejected == []


def test_render_workspace_gives_spec_to_file_blind_participants(tmp_path):
    """A participant that cannot write files cannot read them either — without the contents
    injected it can only guess.

    A real failure: two DeepSeek participants both stated plainly that "we have not seen the
    actual contents of SPEC.md", and one of them wrote "intentionally does NOT support ^" on
    that basis, while the spec explicitly required ^.
    """
    (tmp_path / "SPEC.md").write_text("必须支持 ^ 与 ~", encoding="utf-8")
    (tmp_path / "semver.py").write_text("def satisfies(): ...", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x01")

    out = patch.render_workspace(tmp_path)

    assert "必须支持 ^ 与 ~" in out
    assert "def satisfies" in out
    assert "[core]" not in out, "repository internals must not crowd out the context"
    assert "logo.png" not in out, "a binary file must not be injected"


def test_render_workspace_truncates_huge_files(tmp_path):
    (tmp_path / "big.txt").write_text("x" * (patch.MAX_FILE_CHARS + 5000), encoding="utf-8")
    out = patch.render_workspace(tmp_path)
    assert "(truncated)" in out
    assert len(out) < patch.MAX_FILE_CHARS + 2000


def test_render_workspace_empty_dir_is_empty_string(tmp_path):
    assert patch.render_workspace(tmp_path) == ""


def test_count_fences_separates_no_code_from_unlabeled_code():
    """ "There was no code to hand in" and "code was written but no path was marked" are
    indistinguishable in the working directory.
    """
    assert patch.count_fences("纯讨论，没有代码。") == 0
    assert patch.count_fences("```python\nx = 1\n```") == 1
    assert patch.count_fences("```py\na\n```\n文字\n```js\nb\n```") == 2


@pytest.mark.parametrize(
    "label,text,expected",
    [
        ("纯讨论", "我这轮只讨论，没有代码。", 0),
        ("一个完整围栏", "```py name=a.py\nX\n```", 1),
        ("半截围栏（被截断）", "```py name=a.py\nX 写到一半就没 token 了", 1),
        ("两个完整", "```py\nA\n```\n文字\n```py\nB\n```", 2),
        ("两个完整加一个半截", "```py\nA\n```\n```py\nB\n```\n```py\nC 没写完", 3),
    ],
)
def test_a_truncated_fence_still_counts_as_code_attempted(label, text, expected):
    """Count **opening fences**, not pairs.

    The entire reason `count_fences` exists is to separate "there was no code to hand in" from
    "code was written but never handed in" — the two look identical in the working directory
    (nothing landed either way).

    It used to be `total // 2`, so a truncated output (the opening fence written, out of tokens
    before the close) came out as **0**, indistinguishable from "pure discussion".

    The measured case: a participant wrote 28,562 characters, most of it code, and the event
    stream recorded `fences_seen=0` — making it look as though it had never intended to hand
    code over.
    **And none of that left a trace in the outcome; it was found by watching the middle.**
    """
    assert patch.count_fences(text) == expected
