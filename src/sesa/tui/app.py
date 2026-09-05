"""The interface for watching a deliberation and stepping into it.

Why it exists: once a deliberation is running it is a black box, and **many problems appear
only part-way through** — one round timing out, one participant failing every round,
evidence red throughout — and none of that need leave a trace in the outcome. `sesa watch`
solved "being able to see"; this solves "being able to reach in".

All four interventions (DESIGN §9) become events and are replayable:

* **Interject** — append a constraint into the next round's context
* **Veto a premise** — declare a premise invalid, and everyone must work around it
* **Follow one side** — "carry on along X's lines"
* **Wrap up early** — write it up once this round finishes; start no new round

The first three are one pipeline (``state.pending_injections``) with different wording; the
fourth goes through ``Engine.request_stop()``. **No button is built that is not wired to the
engine** — a key that does nothing when pressed is worse than no key at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import ClassVar

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from .. import events as ev
from ..engine import Engine
from ..i18n import t
from ..types import Outcome

#: How many characters each participant's panel keeps at most. A deliberation can produce hundreds
#: of thousands of characters, and keeping all of it in memory both stalls the interface and serves
#: nobody — nobody scrolls back that far.
PANEL_LIMIT = 20_000


@dataclass
class Intervention:
    """One human intervention. ``kind`` exists only to say which sort it was in the event stream."""

    kind: str
    text: str


class InterventionModal(ModalScreen[Intervention | None]):
    """Take one line of human intervention."""

    # BINDINGS is **a class attribute, evaluated at import time**, when the language has not been
    # resolved from the config yet. Wrapping it in t() would only freeze it into the language at
    # resolution time, which is worse. The footer key bar stays in English.
    BINDINGS: ClassVar = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, kind: str, prompt: str, placeholder: str = "") -> None:
        super().__init__()
        self.kind, self.prompt_text, self.placeholder = kind, prompt, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label(self.prompt_text, id="modal-title")
            yield Input(placeholder=self.placeholder, id="modal-input")
            with Horizontal(id="modal-buttons"):
                yield Button(t("Send"), variant="primary", id="ok")
                yield Button(t("Cancel"), id="cancel")

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    @on(Button.Pressed, "#ok")
    @on(Input.Submitted)
    def _accept(self) -> None:
        text = self.query_one("#modal-input", Input).value.strip()
        self.dismiss(Intervention(self.kind, text) if text else None)

    @on(Button.Pressed, "#cancel")
    def _reject(self) -> None:
        self.dismiss(None)


class ParticipantPanel(Static):
    """One participant's turn, live."""

    def __init__(self, pid: str) -> None:
        super().__init__(id=f"panel-{pid}")
        self.pid = pid
        self.border_title = pid
        self._log: RichLog | None = None

    def compose(self) -> ComposeResult:
        # can_focus: one click selects the panel, after which `y` / ctrl+c copies the whole thing.
        self._log = RichLog(wrap=True, markup=False, auto_scroll=True, id=f"log-{pid_id(self.pid)}")
        self._log.can_focus = True
        yield self._log

    def append(self, text: str) -> None:
        if self._log is not None:
            self._log.write(text, expand=True)

    def mark(self, note: str) -> None:
        self.border_subtitle = note


def pid_id(pid: str) -> str:
    """A participant id may contain dots, spaces and other characters a CSS selector does not
    accept.
    """
    return "".join(ch if ch.isalnum() else "_" for ch in pid)


class SesaApp(App[int]):
    """Watch and intervene. The return value is the exit code, matching `sesa run`'s convention."""

    CSS = """
    Screen { layout: vertical; }
    #panels { height: 1fr; }
    ParticipantPanel {
        width: 1fr; height: 100%; border: round $primary;
        padding: 0 1; margin: 0 1 0 0;
    }
    #bottom { height: 14; }
    #matrix { width: 2fr; border: round $secondary; padding: 0 1; }
    #timeline { width: 3fr; border: round $accent; padding: 0 1; }
    #modal {
        width: 70; height: auto; border: thick $primary;
        background: $surface; padding: 1 2;
    }
    #modal-buttons { height: auto; align-horizontal: right; }
    """

    BINDINGS: ClassVar = [
        # Drag-select comes with Textual, but **it binds no copy** — you can select and not take it
        # away. Both keys are given: ctrl+c is muscle memory (Textual takes it for quit by default
        # and it is overridden here — quitting by accident while a deliberation is running is far
        # worse than copying by accident), and y for the vim-handed.
        ("ctrl+c", "copy_selection", "copy selection"),
        ("y", "copy_selection", "copy selection"),
        ("i", "intervene_say", "interject"),
        ("v", "intervene_veto", "veto a premise"),
        ("f", "intervene_follow", "follow one side"),
        ("s", "wrap_up", "wrap up"),
        ("q", "quit", "quit"),
    ]

    status: reactive[str] = reactive(t("Preparing…"))

    def __init__(
        self,
        engine: Engine,
        task: str,
        participants: list[str],
        *,
        prior=None,
        inject: str | None = None,
        resumed_from: str | None = None,
    ) -> None:
        super().__init__()
        # `task` collides with Textual's App.task (a read-only property) and has to be renamed.
        self.engine, self.topic = engine, task
        self.participants = participants
        # The resume trio. **Not only run may use the TUI** — a resume is equally an occasion for
        # watching closely and interjecting, and it needs it more: you came back with a specific
        # question.
        self._prior, self._inject, self._resumed_from = prior, inject, resumed_from
        self.exit_code = Outcome.EXHAUSTED.value
        self._panels: dict[str, ParticipantPanel] = {}
        self._pending: list[str] = []
        self._finished = False

    # ------------------------------------------------------------------ # Layout
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panels"):
            for pid in self.participants:
                panel = ParticipantPanel(pid)
                self._panels[pid] = panel
                yield panel
        with Horizontal(id="bottom"):
            matrix = DataTable(id="matrix")
            matrix.border_title = t("disagreement matrix")
            yield matrix
            timeline = RichLog(id="timeline", wrap=True, markup=True, auto_scroll=True)
            timeline.can_focus = True
            timeline.border_title = t("progress and interventions")
            yield timeline
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.topic[:60]
        self._note("[dim]" + t("task: {what}", what=self.topic[:200]) + "[/dim]")
        if self._resumed_from:
            rounds = len(self._prior.rounds) if self._prior else 0
            self._note(
                "[cyan]"
                + t(
                    "resuming {run}: carrying over {n} rounds of discussion",
                    run=self._resumed_from,
                    n=rounds,
                )
                + "[/cyan]"
            )
        if self._inject:
            self._note("[magenta]" + t("what you added: {text}", text=self._inject) + "[/magenta]")
        self.run_deliberation()

    # ------------------------------------------------------------------ # The engine
    # ------------------------------------------------------------------ #

    @work(exclusive=True)
    async def run_deliberation(self) -> None:
        """Run the engine and push its events to the interface.

        The engine is an async generator, so no thread is needed here — **and none may be used**:
        `pending_injections` would be touched from both sides at once.
        """
        try:
            async for event in self.engine.run(
                self.topic,
                prior=self._prior,
                inject=self._inject,
                resumed_from=self._resumed_from,
            ):
                self._render(event)
                # Yield control, or a dense stream of deltas starves the interface.
                await asyncio.sleep(0)
        except Exception as exc:  # If the engine blows up people have to see it, rather
            # than the interface silently freezing
            self._note(
                "[red]"
                + t("engine error: {error}", error=f"{type(exc).__name__}: {exc}")
                + "[/red]"
            )
        finally:
            self._finished = True
            self.status = t("finished")
            self._note("[bold]" + t("Deliberation finished. Press q to quit.") + "[/bold]")

    def _render(self, event) -> None:
        if isinstance(event, ev.RoundStart):
            self.status = t("round {n} · drafted by {who}", n=event.round, who=event.rapporteur)
            self._note(
                "\n[bold cyan]── "
                + t("Round {n} ── drafted by {who}", n=event.round, who=event.rapporteur)
                + "[/bold cyan]"
            )
            for panel in self._panels.values():
                panel.mark(t("round {n}", n=event.round))
        elif isinstance(event, ev.TurnStart):
            if panel := self._panels.get(event.participant):
                panel.mark(t("speaking…"))
        elif isinstance(event, ev.TurnDelta):
            if panel := self._panels.get(event.participant):
                panel.append(event.text)
        elif isinstance(event, ev.TurnEnd):
            if panel := self._panels.get(event.participant):
                mark = t(
                    "{chars} chars · {secs}s", chars=event.chars, secs=f"{event.duration_s:.0f}"
                )
                if event.truncated:
                    mark += " · " + t("truncated")
                if event.error:
                    mark += " · " + t("failed")
                panel.mark(mark)
            if event.error:
                self._note(
                    "[red]"
                    + t(
                        "{who} failed: {reason}",
                        who=event.participant,
                        reason=str(event.error)[:160],
                    )
                    + "[/red]"
                )
            elif event.truncated:
                # Truncation is invisible in the outcome, and it means this person's stance card is
                # not adopted.
                self._note(
                    "[yellow]"
                    + t("{who}'s statement was cut off by the output budget", who=event.participant)
                    + "[/yellow]"
                )
        elif isinstance(event, ev.ConsensusUpdate):
            self._update_matrix(event)
        elif isinstance(event, ev.Evidence):
            flag = (
                t("passed") if event.exit_code == 0 else t("failed({code})", code=event.exit_code)
            )
            self._note(
                "[dim]" + t("evidence {who}: {flag}", who=event.participant, flag=flag) + "[/dim]"
            )
        elif isinstance(event, ev.ErrorEvent):
            self._note(f"[red]{event.where}：{str(event.message)[:200]}[/red]")
        elif isinstance(event, ev.BudgetWarn):
            self._note("[yellow]" + t("budget: {reason}", reason=event.reason) + "[/yellow]")
        elif isinstance(event, ev.HumanInject):
            self._note(
                "[magenta]"
                + t("injected ({kind}): {text}", kind=event.kind, text=event.text)
                + "[/magenta]"
            )
        elif isinstance(event, ev.VerdictFinal):
            self.exit_code = event.outcome
            self.status = t("outcome: {what}", what=event.outcome)
            self._note(
                "\n[bold green]" + t("outcome: {what}", what=event.outcome) + "[/bold green]"
            )
            if event.result_path:
                self._note(f"[dim]{event.result_path}[/dim]")

    def _update_matrix(self, event: ev.ConsensusUpdate) -> None:
        table = self.query_one("#matrix", DataTable)
        table.clear(columns=True)
        ids = sorted(event.matrix)
        if not ids:
            return
        table.add_columns("", *ids)
        marks = {
            "agree": t("agree"),
            "partial": t("partial"),
            "disagree": t("disagree"),
            "unknown": t("not measured"),
        }
        for source in ids:
            row = [source]
            for target in ids:
                row.append(
                    "—"
                    if source == target
                    else marks.get(event.matrix[source].get(target, "unknown"), "?")
                )
            table.add_row(*row)
        table.border_subtitle = t(
            "unresolved {n} · lowest confidence {v} · {state}",
            n=event.unresolved,
            v=event.min_confidence,
            state=event.state,
        )

    def _note(self, markup: str) -> None:
        self.query_one("#timeline", RichLog).write(markup)

    def watch_status(self, value: str) -> None:
        self.title = f"sesa · {value}"

    # ------------------------------------------------------------------ # Interventions
    # ------------------------------------------------------------------ #

    def _submit(self, result: Intervention | None) -> None:
        if result is None:
            return
        if self._finished:
            self._note(
                "[yellow]"
                + t("The deliberation has ended; this will not take effect.")
                + "[/yellow]"
            )
            return
        # Three interventions share one pipeline with different wording — the engine takes them at
        # the start of the next round.
        self._queue(result)

    def _queue(self, result: Intervention) -> None:
        """Queue an intervention for the next round.

        **It does not take effect immediately**: the round being written cannot see it. The
        interface has to say so, or the user thinks the key did nothing.
        """
        prefix = {
            "say": "",
            "veto": t(
                "The moderator declares the following premise invalid; everyone must "
                "argue around it: "
            ),
            "follow": t("The moderator asks that you proceed along this line: "),
        }[result.kind]
        if self.engine.inject(f"{prefix}{result.text}"):
            self._note(
                "[magenta]"
                + t("queued for next round ({kind}): {text}", kind=result.kind, text=result.text)
                + "[/magenta]"
            )
        else:
            self._note("[yellow]" + t("Empty intervention; nothing was queued.") + "[/yellow]")

    def action_intervene_say(self) -> None:
        self.push_screen(
            InterventionModal(
                "say",
                t("Interject — add a constraint that everyone sees next round"),
                t("e.g. the budget is only two person-months; re-estimate on that basis"),
            ),
            self._submit,
        )

    def action_intervene_veto(self) -> None:
        self.push_screen(
            InterventionModal(
                "veto",
                t("Veto a premise — declare it invalid; everyone must argue around it"),
                t("e.g. do not assume downtime for maintenance is available"),
            ),
            self._submit,
        )

    def action_intervene_follow(self) -> None:
        self.push_screen(
            InterventionModal(
                "follow",
                t("Follow one side — proceed along whose line"),
                t("e.g. carry on with claude's layered design"),
            ),
            self._submit,
        )

    def action_copy_selection(self) -> None:
        """Put the drag-selected text on the clipboard.

        With nothing selected it **does not fail silently** — the user would think the copy worked
        and paste something else. The next best thing: copy the whole of the focused panel, and say
        plainly what was done.
        """
        text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)
            lines = len(text.splitlines())
            self._note(
                "[green]"
                + t(
                    "copied {n} characters ({lines} lines) to the clipboard",
                    n=len(text),
                    lines=lines,
                )
                + "[/green]"
            )
            return

        # No selection: fall back to copying the whole of the focused panel.
        # **It has to say the copy was the whole thing and not the selection**, or the user assumes
        # they have the few lines they dragged over and pastes a whole screen.
        # Note: Textual gives focus to the first focusable widget on startup, so "is anything
        # focused" cannot tell whether the user ever clicked — I did try to use it for that, and it
        # does not work. Saying it plainly in the message is more reliable than guessing the user's
        # intent.
        panel = self.focused if isinstance(self.focused, RichLog) else None
        if panel is None:
            self._note(
                "[yellow]"
                + t("Nothing is selected and no panel has focus.")
                + "[/yellow][dim]"
                + t("Drag to select, then press y; or click a panel first.")
                + "[/dim]"
            )
            return
        # `panel.lines` holds Strip objects, and `str()` of one gives an internal representation
        # like `Strip([Segment('…')], 26)` — **paste that into a document and it is all garbage**.
        # `.text` is the line of words a person wants.
        whole = "\n".join(line.text for line in panel.lines).strip()
        if not whole:
            self._note("[yellow]" + t("This panel has no content yet.") + "[/yellow]")
            return
        self.copy_to_clipboard(whole)
        label = getattr(panel.parent, "pid", None) or t("timeline")
        # **`[alice]` gets eaten by rich as markup.** The timeline is a RichLog with markup=True, so
        # any external string put into it must be escaped first — this is the third time today I
        # have fallen into this hole (the `sesa[tui]` install hint, E() in the CLI, and here).
        self._note(
            "[green]"
            + t(
                "No selection; copied all of the «{label}» panel ({n} characters)",
                label=escape(label),
                n=len(whole),
            )
            + "[/green]"
        )

    def action_wrap_up(self) -> None:
        if self._finished:
            return
        self.engine.request_stop()
        self._note(
            "[bold yellow]"
            + t(
                "Wrap-up requested: the current round finishes and is written up; no "
                "new round will open."
            )
            + "[/bold yellow]\n[dim]"
            + t(
                "The round in progress is not cut off mid-way — half a statement gets "
                "no stance card, that round's money is wasted, and the consensus "
                "judgement would only record it as «not measured»."
            )
            + "[/dim]"
        )


def run_tui(
    engine: Engine,
    task: str,
    participants: list[str],
    *,
    prior=None,
    inject: str | None = None,
    resumed_from: str | None = None,
) -> str:
    """Run it and return the outcome string."""
    app = SesaApp(engine, task, participants, prior=prior, inject=inject, resumed_from=resumed_from)
    app.run()
    return app.exit_code
