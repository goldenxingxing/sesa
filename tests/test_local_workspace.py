"""The default workspace = the directory you started in.

The first real user's first deliberation: they ran, inside a directory of documents,

    sesa run --tui "review the product requirements document in this folder"

All three agent CLIs were placed in `/var/folders/.../sesa-XXXX/<id>/` — **empty
directories**. Not one of the six documents was visible. And the deliberation ran on, the
interface scrolled on, the deliverables were produced, and **nothing anywhere told them
that the three models were reviewing a document they had never seen**.

This is the same thing DESIGN records as "the spec never reached the participants, and the
deliverable looked perfectly normal", except that this time it hid in the **default
configuration**, where every new user walks into it.
"""

from __future__ import annotations

import pytest

from sesa.workspace import EphemeralWorkspace, LocalWorkspace


def test_everyone_works_in_the_invocation_directory(tmp_path):
    ws = LocalWorkspace(tmp_path)
    out = ws.prepare(["a", "b", "c"])
    assert {c.path for c in out.values()} == {tmp_path.resolve()}


def test_participants_can_see_the_files_that_were_already_there(tmp_path):
    """This is the crux of the whole thing: the task says "the documents in this folder", so the
    participants have to really be able to see them.
    """
    (tmp_path / "需求文档.md").write_text("内容", encoding="utf-8")
    ws = LocalWorkspace(tmp_path)
    seen = ws.prepare(["a"])["a"].path
    assert (seen / "需求文档.md").exists()


def test_it_does_not_secretly_give_each_participant_a_subdirectory(tmp_path):
    """Do not quietly split it into subdirectories here — that would look like isolation while
    providing none, and "believing there is" is more dangerous than "knowing there is not".
    """
    out = LocalWorkspace(tmp_path).prepare(["a", "b"])
    assert out["a"].path == out["b"].path


# ── without isolation, some questions cannot be asked at all — say so plainly ────── #


def test_a_shared_workspace_declares_that_it_does_not_isolate():
    assert LocalWorkspace().isolates_participants is False
    assert EphemeralWorkspace().isolates_participants is True


def test_adoption_detection_is_skipped_and_explained_on_a_shared_workspace():
    """In a shared directory everyone's snapshot is identical by construction.

    Computing it anyway is not only wasted — measured, it took the test suite from 150s to
    850s, because it snapshots the whole repository once per participant per round — it also
    produces an empty "no copying detected" conclusion whose real meaning is "this question
    cannot be asked in this kind of workspace".
    """
    import inspect

    from sesa.engine import Engine

    source = inspect.getsource(Engine.run)
    assert "isolates_participants" in source
    assert "cannot run" in source


@pytest.mark.asyncio
async def test_the_run_says_so_once_not_every_round(tmp_path):
    import sesa.events as ev
    from sesa.engine import Engine
    from sesa.protocols import build as build_protocol
    from tests.test_engine import participant

    engine = Engine(
        [participant("alice"), participant("bob")],
        build_protocol("debate"),
        max_rounds=2,
        workspace=LocalWorkspace(tmp_path),
    )
    notes = [
        e
        async for e in engine.run("该用 Postgres 还是 SQLite？")
        if isinstance(e, ev.ErrorEvent) and e.where == "adoption"
    ]
    assert len(notes) == 1, f"it should say this once, and it said it {len(notes)} times"
    assert "--repo" in notes[0].message, "it has to tell the user how to get isolation"


# ── scanning the directory must not walk the entire project ─────────────────────── #


def test_the_workspace_walk_prunes_heavy_directories(tmp_path):
    """`rglob("*")` walks into .venv / node_modules first and filters afterwards — 30,000 entries
    under this repository's root, and this function runs once per turn.
    """
    from sesa.patch import _walk

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("x", encoding="utf-8")
    for heavy in (".venv", "node_modules", ".git", "__pycache__"):
        (tmp_path / heavy).mkdir()
        (tmp_path / heavy / "junk.py").write_text("y", encoding="utf-8")

    found = {p.relative_to(tmp_path).as_posix() for p in _walk(tmp_path)}
    assert found == {"src/keep.py"}, f"pruning missed these: {found}"
