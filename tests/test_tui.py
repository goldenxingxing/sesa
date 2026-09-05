"""How the TUI behaves.

**The engine knows nothing about terminals** (DESIGN §2) — the TUI is just one more consumer
of the event stream. Two things are tested here: that the interface can render the events,
and that **an intervention really reaches the engine**.

The second is the point. A key that does nothing when pressed is worse than no key at all:
the user believes they have intervened, and then takes a conclusion that does not reflect
their view and makes a decision on it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="needs the optional textual extra: pip install 'sesa[tui]'")

from sesa.engine import Engine
from sesa.protocols import build as build_protocol
from tests.test_engine import participant


def _engine(**kw) -> Engine:
    return Engine(
        [participant("alice"), participant("bob")],
        build_protocol("debate"),
        max_rounds=2,
        **kw,
    )


# ── an intervention has to really reach the engine ──────────────────────────────── #


def test_an_injection_reaches_the_next_round(monkeypatch):
    """A queued intervention has to appear in **the next round's** prompt.

    The engine merges queued interventions **before** opening a new round — merging them after
    plan() wastes the round.
    """
    engine = _engine()
    assert engine.inject("预算只有两个人月") is True

    seen: list[str] = []
    original = engine.protocol.plan

    def spy(state):
        seen.append(" ".join(state.pending_injections))
        return original(state)

    monkeypatch.setattr(engine.protocol, "plan", spy)

    import asyncio

    asyncio.run(_drain(engine))
    assert any("预算只有两个人月" in text for text in seen), (
        f"the intervention reached no round's context: {seen}"
    )


async def _drain(engine: Engine) -> None:
    async for _ in engine.run("该用 Postgres 还是 SQLite？"):
        pass


def test_an_empty_injection_is_refused_rather_than_queued():
    """An empty intervention queued only leaves a blank line in the next round's prompt."""
    engine = _engine()
    assert engine.inject("   ") is False
    assert engine.inject("") is False


def test_wrap_up_stops_opening_new_rounds():
    """Wrapping up early = **write it up once this round finishes**, not cut off mid-way.

    Half a turn yields no stance card, that round's money is wasted, and the consensus assessment
    only records it as "not measured".
    """
    import asyncio

    import sesa.events as ev

    engine = _engine()
    rounds: list[int] = []

    async def drive():
        async for event in engine.run("该用 Postgres 还是 SQLite？"):
            if isinstance(event, ev.RoundStart):
                rounds.append(event.round)
                engine.request_stop()  # request the wrap-up right at the start of round 0

    asyncio.run(drive())
    assert rounds == [0], f"a new round started after the wrap-up was requested: {rounds}"


def test_wrap_up_makes_the_verdict_stop_hoping_for_later_rounds():
    """A human wrap-up and an exhausted budget are equivalent on the point that matters — there is
    no next round.

    Without telling the assessment that, it reports an optimistic intermediate state on the
    assumption that rounds remain.
    """
    import inspect

    source = inspect.getsource(Engine.run)
    assert "or self._stop_requested" in source


# ── the interface ───────────────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_the_app_renders_events_without_crashing(monkeypatch):
    """The event-rendering path has to hold for every kind of event — a TUI that crashes part-way
    leaves the user with a frozen black box while the deliberation is in fact still running.

    ``on_mount`` really starts a deliberation (spawning subprocesses). The test has to cut that
    off: leaving it in is not only slow but makes this test **intermittently red** — and an
    intermittent red is worse than a steady one, because people learn "just run it again" and
    then let a real red through the same way.
    """
    import sesa.events as ev
    from sesa.tui.app import SesaApp

    monkeypatch.setattr(SesaApp, "run_deliberation", lambda self: None)
    app = SesaApp(_engine(), "该用 Postgres 还是 SQLite？", ["alice", "bob"])
    async with app.run_test() as pilot:
        for event in (
            ev.RoundStart(round=0, rapporteur="alice"),
            ev.TurnStart(round=0, participant="alice"),
            ev.TurnDelta(round=0, participant="alice", text="我认为…"),
            ev.TurnEnd(
                round=0, participant="alice", chars=4, duration_s=1.0, usage={}, truncated=True
            ),
            ev.ConsensusUpdate(
                round=0,
                unresolved=1,
                min_confidence=0.7,
                matrix={"alice": {"bob": "agree"}, "bob": {"alice": "unknown"}},
                state="open",
            ),
            ev.ErrorEvent(where="round 0", message="某处出错"),
            ev.VerdictFinal(
                outcome="consensus",
                run_id="r1",
                drafted_by="alice",
                rounds_used=1,
                unresolved=0,
                result_path="RESULT.md",
            ),
        ):
            app._render(event)
        await pilot.pause()
        assert app.exit_code == "consensus"


def test_interventions_are_reachable_by_key():
    """All four keys have to be really bound to an action.

    This one **does not need the app running** — it checks the bindings and methods on the class.
    It used to be wrapped in a ``run_test()``, so every run really started a deliberation, turning
    a purely static check into an intermittently red asynchronous test.
    """
    from sesa.tui.app import SesaApp

    bound = {binding[0] for binding in SesaApp.BINDINGS}
    assert {"i", "v", "f", "s", "q"} <= bound

    for action in ("intervene_say", "intervene_veto", "intervene_follow", "wrap_up"):
        assert callable(getattr(SesaApp, f"action_{action}", None)), f"{action} is not implemented"


# ── a resume must be able to open the TUI too ───────────────────────────────────── #


def test_resume_accepts_tui_too():
    """The first resume command I gave the user carried `--tui`, and resume had no such option.

    **A resume needs it more than a first run**: you came back with a specific question, wanting
    to watch how they answer it and add another sentence at any moment.
    """
    from typer.testing import CliRunner

    from sesa.cli import app

    out = CliRunner().invoke(app, ["resume", "--help"]).output
    assert "--tui" in out


def test_run_and_resume_share_one_tui_launch_path():
    """Two implementations drift apart eventually — and drifting apart here means one interface
    behaving differently under two commands.
    """
    import inspect

    from sesa import cli

    assert "_launch_tui" in inspect.getsource(cli.run)
    assert "_launch_tui" in inspect.getsource(cli.resume)


@pytest.mark.asyncio
async def test_the_app_shows_what_was_carried_over_and_injected(monkeypatch):
    """On a resume the interface has to say two things clearly: how many rounds were carried over,
    and what you added.

    Otherwise you are looking at a blank interface with no idea whether it really picked up the
    previous run.
    """
    from sesa.state import DeliberationState, RoundRecord
    from sesa.tui.app import SesaApp
    from sesa.types import ParticipantSpec

    monkeypatch.setattr(SesaApp, "run_deliberation", lambda self: None)
    prior = DeliberationState(
        task="t",
        participants=[ParticipantSpec(id=p, adapter="cli") for p in ("alice", "bob")],
        max_rounds=2,
    )
    prior.rounds.extend([RoundRecord(0), RoundRecord(1), RoundRecord(2)])

    app = SesaApp(
        _engine(),
        "议题",
        ["alice", "bob"],
        prior=prior,
        inject="预算不是问题",
        resumed_from="20260903-111342-8fab2e",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = app.query_one("#timeline").lines
    text = " ".join(str(line) for line in rendered)
    assert "carrying over 3 rounds" in text
    assert "预算不是问题" in text


# ── select and copy ─────────────────────────────────────────────────────────────── #


def _app_with(monkeypatch, text: str = "可以复制的发言"):
    from sesa.tui.app import SesaApp

    monkeypatch.setattr(SesaApp, "run_deliberation", lambda self: None)
    return SesaApp(_engine(), "议题", ["alice", "bob"]), text


@pytest.mark.asyncio
async def test_copying_is_bound_to_a_key(monkeypatch):
    """Textual has drag-select built in but **binds no copy** — you can select and not take it away."""
    from sesa.tui.app import SesaApp

    keys = {b[0] for b in SesaApp.BINDINGS}
    assert "ctrl+c" in keys and "y" in keys


@pytest.mark.asyncio
async def test_the_fallback_copies_real_text_not_internal_objects(monkeypatch):
    """`panel.lines` holds Strip objects, and `str()` of one gives an internal representation like
    `Strip([Segment('…')], 26)` — **paste that into a document and it is all garbage**.
    """
    import sesa.events as ev

    app, text = _app_with(monkeypatch)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append

    async with app.run_test() as pilot:
        app._render(ev.TurnDelta(round=0, participant="alice", text=text))
        await pilot.pause()
        app.set_focus(app._panels["alice"]._log)
        await pilot.press("y")
        await pilot.pause()

    assert copied, "copy was pressed and nothing reached the clipboard"
    assert copied[-1] == text
    assert "Strip(" not in copied[-1] and "Segment(" not in copied[-1]


@pytest.mark.asyncio
async def test_the_fallback_says_it_copied_the_whole_panel(monkeypatch):
    """**It has to say the copy was the whole thing and not the selection**, or the user assumes they
    have the few lines they just dragged over and pastes a whole screen.

    And it has to name the panel — `[alice]` gets eaten by rich as markup, a hole walked into for
    the third time today (the `sesa[tui]` install hint, the CLI's E(), and here).
    """
    import sesa.events as ev

    app, text = _app_with(monkeypatch)
    app.copy_to_clipboard = lambda _: None

    async with app.run_test() as pilot:
        app._render(ev.TurnDelta(round=0, participant="alice", text=text))
        await pilot.pause()
        app.set_focus(app._panels["alice"]._log)
        await pilot.press("y")
        await pilot.pause()
        timeline = " ".join(line.text for line in app.query_one("#timeline").lines)

    assert "copied all of the" in timeline
    assert "alice" in timeline, "the panel name was eaten by rich as markup"


@pytest.mark.asyncio
async def test_an_empty_panel_says_so_instead_of_copying_nothing(monkeypatch):
    """Copying an empty panel and reporting "copied" leaves the user thinking there is something on
    the clipboard.
    """
    app, _ = _app_with(monkeypatch)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append

    async with app.run_test() as pilot:
        app.set_focus(app._panels["alice"]._log)
        await pilot.press("y")
        await pilot.pause()
        timeline = " ".join(line.text for line in app.query_one("#timeline").lines)

    assert not copied
    assert "no content yet" in timeline
