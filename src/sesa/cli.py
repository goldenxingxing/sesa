"""The command-line entry point.

The CLI is just one consumer of the event stream — all the orchestration lives in
:class:`sesa.engine.Engine`. On a tty it renders the live deliberation interface; piped
somewhere it emits JSONL, ready to wire into someone else's toolchain.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _escape_markup
from rich.panel import Panel
from rich.table import Table

from . import config as cfg
from . import events as ev
from . import i18n
from ._install import install_hint
from .adapters import available as available_adapters
from .adapters import build as build_adapter
from .budget import Budget
from .consensus.matrix import StanceMatrix, render_matrix
from .engine import Engine
from .i18n import t
from .protocols import available as available_protocols
from .protocols import build as build_protocol
from .record import Recorder, load_state, new_run_id
from .types import Outcome, ParticipantSpec

app = typer.Typer(
    name="sesa",
    help=(
        "Sesa — open sesame. Let different models speak until they agree; when a door "
        "will not open, it tells you exactly where it is stuck."
    ),
    no_args_is_help=True,
    add_completion=False,
)
participants_app = typer.Typer(
    name="participants", help="Manage participants", no_args_is_help=True
)
app.add_typer(participants_app)

console = Console()


def E(value: object) -> str:
    """Escape rich markup, **and turn the value into a string first**.

    `rich.markup.escape` only accepts str, and feeding it a Path raises
    ``TypeError: expected string or bytes-like object``. It wraps 20-odd external
    interpolations here, several of which pass a Path, an int or None — and most of those are
    on **error paths**, never reached on a normal run, so the crash happens only when the user
    is already in trouble.

    Measured: `sesa runs` crashed outright when `.sesa/runs` did not exist yet — the first
    command a new user runs.
    """
    return _escape_markup(str(value))


err = Console(stderr=True)

#: Outcome → (English source string, rich style). ``t()`` translates the label at print time; the
#: style never changes with language.
OUTCOME_STYLE = {
    Outcome.CONSENSUS.value: ("✅ Consensus reached", "green"),
    Outcome.CONSENSUS_WITH_RESERVATIONS.value: (
        "🟡 Consensus with reservations — nobody objected, but reservations are on record",
        "yellow",
    ),
    Outcome.DEADLOCK.value: ("⚠️  No consensus — the deliberation deadlocked", "yellow"),
    Outcome.EXHAUSTED.value: ("⏳ Unfinished — rounds or budget ran out", "yellow"),
    Outcome.NOT_MEASURED.value: (
        "⚪ This protocol does not measure consensus — everyone answers independently",
        "cyan",
    ),
    Outcome.PARTIAL_COVERAGE_CONSENSUS.value: (
        "🟠 Consensus over partial coverage — no objections in what was measured, "
        "but some cells were never measured",
        "yellow",
    ),
    Outcome.FALSE_CONSENSUS.value: (
        "🔁 False consensus — the stance cards claim agreement, the statements conflict",
        "magenta",
    ),
}
# A missing entry has the terminal print a bare enum value. Both banner tables (this one and
# report.OUTCOME_BANNER) must each cover every outcome — last time only one of them was filled in.
# Use raise rather than assert: `python -O` strips assert lines entirely, so this integrity guard
# would silently vanish in production — while what it guards against is exactly the kind of thing
# ("the outcome degrading to a bare enum value") that only shows up on a real run.
if set(OUTCOME_STYLE) != {o.value for o in Outcome}:
    raise RuntimeError(
        "No banner configured for these outcomes: "
        f"{sorted({o.value for o in Outcome} - set(OUTCOME_STYLE))}"
    )


def _granularity_cell(metrics) -> str:
    """Count × length. The similarity correlates strongly with it (r≈0.85), so they must be shown
    side by side.
    """
    count, length, cv = metrics.residual_granularity
    if not count:
        return "—"
    flag = "[yellow]*[/yellow]" if cv > 0.25 else ""
    return f"{count:g}×{length:.0f}{flag}"


def _restatement_cell(metrics) -> str:
    """The median similarity of newly added residuals to the previous round — no threshold
    involved.

    When semantic comparison cannot be installed, leave it blank honestly rather than
    substituting turnover.
    """
    from .semantic import SemanticUnavailable

    if not metrics.residual_flow:
        return "—"
    try:
        value = metrics.residual_similarity()
    except SemanticUnavailable:
        return "[dim]" + t("not installed") + "[/dim]"
    return f"{value:.2f}" if value is not None else "—"


def _install_abort_handler(recorder: Recorder, *, stream_json: bool = False) -> None:
    """When taken away by SIGTERM, leave a record in the event stream before exiting.

    Otherwise an aborted run is merely "the event stream stopped", and afterwards it can only
    be inferred from "verdict.final is missing" — inference misses cases, and it cannot say who
    aborted it.
    """
    import contextlib
    import signal

    def handler(signum, _frame):  # pragma: no cover - needs a real signal to fire
        name = signal.Signals(signum).name
        event = ev.RunAborted(
            reason=t("received {signal}; the run was stopped from outside", signal=name)
        )
        try:
            # **recorder.emit must not be used.** This handler can land in the middle of an emit,
            # and emitting again re-enters the same buffer, which CPython answers with a
            # RuntimeError — which the `finally: raise SystemExit` below swallows, so the abort
            # record is silently lost, while leaving that record is the whole point of this handler.
            if not recorder.emit_abort(event):
                # A failure has to be said out loud too. Losing the abort record in silence leaves
                # only "verdict.final is missing" to guess from afterwards — and guessing misses
                # cases, and cannot say who aborted it.
                err.print(
                    "[yellow]"
                    + t("The abort record could not be written to the event stream.")
                    + "[/yellow]"
                )
            recorder.close()
            if stream_json:
                # Whatever is downstream of the pipe also needs to know this run did not finish;
                # otherwise it sees an event stream that stops for no reason
                print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)
        finally:
            raise SystemExit(130)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        # Skip silently off the main thread or on a platform that does not support it — this is a
        # best-effort backstop and must not fail because of it
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def _fail(message: str) -> None:
    err.print("[red]" + t("Error") + f"[/red] {message}")
    raise typer.Exit(1)


def _load_config(path: Path | None) -> cfg.Config:
    try:
        conf = cfg.load(path)
        # The language has to be settled **before any user-facing output**. Anywhere else and the
        # first few lines come out in the wrong language, leaving the user with half English and
        # half Chinese.
        i18n.use(conf.language)
        return conf
    except cfg.ConfigError as exc:
        _fail(str(exc))
        raise  # pragma: no cover


def _build_engine(
    conf: cfg.Config,
    chosen: list[ParticipantSpec],
    recorder: Recorder,
    max_rounds: int,
    protocol=None,
    workspace=None,
    evidence=None,
) -> Engine:
    """Build an engine from the configuration. Shared by run and resume, so their arguments cannot
    drift apart.
    """
    if protocol is None:
        protocol = build_protocol(
            conf.protocol,
            turn_taking=conf.turn_taking,
            **({"proposer": conf.proposer} if conf.protocol == "adversarial" else {}),
        )
    return Engine(
        chosen,
        protocol,
        matrix=StanceMatrix(conf.confidence_threshold, conf.stability_window, conf.min_coverage),
        budget=Budget(conf.max_usd, conf.max_tokens, conf.max_wall_seconds),
        recorder=recorder,
        max_rounds=max_rounds,
        share_thinking=conf.share_thinking,
        share_residuals=conf.share_residuals,
        rapporteur=conf.rapporteur,
        workspace=workspace,
        evidence=evidence,
        turn_timeout=conf.turn_timeout,
    )


# --------------------------------------------------------------------------- # run
# --------------------------------------------------------------------------- #


@app.command()
def run(
    task: str = typer.Argument(None, help="the task; omit to read from --file or stdin"),
    participant: list[str] = typer.Option(
        None, "-p", "--participant", help="use only these participants"
    ),
    file: Path = typer.Option(
        None, "--file", "-f", help="use a file as the task (an RFC to review, say)"
    ),
    protocol: str = typer.Option(
        None, "--protocol", help=f"deliberation protocol: {'/'.join(available_protocols())}"
    ),
    rounds: int = typer.Option(None, "--rounds", help="maximum rounds"),
    config_path: Path = typer.Option(None, "--config", "-c", help="use this config file"),
    as_json: bool = typer.Option(False, "--json", help="force JSONL event-stream output"),
    tui: bool = typer.Option(
        False,
        "--tui",
        help="full-screen view: watch everyone write, interject / veto a premise / wrap up at any time",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="do not print statements, only progress"
    ),
    output: Path = typer.Option(None, "--out", help="output directory (default ./.sesa)"),
    repo: Path = typer.Option(
        None,
        "--repo",
        help="code task: work on this git repo, one isolated worktree per participant",
    ),
    verify: str = typer.Option(
        None, "--verify", help='code task: verification command run each round, e.g. "pytest -q"'
    ),
    tests: list[str] = typer.Option(
        None,
        "--tests",
        help="code task: where the tests live; given this, the last round cross-tests (repeatable)",
    ),
) -> None:
    """Have several agents debate one topic until consensus converges or the disagreements are
    reported honestly.

    Exit codes: 0 = full consensus; 3 = consensus with reservations (nobody objected, but
    reservations are on record); 2 = no consensus; **4 = not one cell measured** (the protocol
    structurally produces no peer assessment, or no stance card parsed at all — this is not
    "no consensus", it is not measured); 1 = a configuration or invocation error.
    """
    body = _resolve_task(task, file)
    conf = _load_config(config_path)

    try:
        chosen = conf.select(participant)
        conf.validate(chosen)
    except cfg.ConfigError as exc:
        _fail(str(exc))
        return

    protocol_name = protocol or conf.protocol
    try:
        engine_protocol = build_protocol(
            protocol_name,
            turn_taking=conf.turn_taking,
            # Only adversarial knows proposer. Passing it to another protocol triggers the "you
            # configured an option I do not know" warning — and this value is synthesised by the CLI
            # itself; the user configured nothing.
            **(
                {"proposer": "input" if (file and conf.proposer == "rotate") else conf.proposer}
                if protocol_name == "adversarial"
                else {}
            ),
        )
    except ValueError as exc:
        _fail(str(exc))
        return

    recorder = Recorder(Path(output or ".sesa"), new_run_id())
    stream_json = as_json or not sys.stdout.isatty()
    _install_abort_handler(recorder, stream_json=stream_json)

    workspace, evidence = None, None
    if repo or verify:
        if not repo:
            _fail(
                t(
                    "--verify needs --repo: the verification command has to run inside "
                    "a working copy of some repository."
                )
            )
        from .evidence import EvidenceRunner
        from .workspace import GitError, GitWorktreeWorkspace

        workspace = GitWorktreeWorkspace(repo, recorder.run_id)
        try:
            workspace.assert_clean()
        except GitError as exc:
            _fail(str(exc))
            return
        if verify:
            evidence = EvidenceRunner(
                verify, timeout=conf.turn_timeout, test_paths=list(tests or [])
            )

    engine = _build_engine(
        conf,
        chosen,
        recorder,
        rounds or conf.max_rounds,
        engine_protocol,
        workspace=workspace,
        evidence=evidence,
    )
    if tui:
        outcome_value = _launch_tui(engine, body, [p.id for p in chosen], stream_json=stream_json)
        raise typer.Exit(exit_code_for(outcome_value))

    try:
        exit_code = asyncio.run(
            _drive(engine, body, stream_json, quiet, protocol_name, engine_protocol)
        )
    except KeyboardInterrupt:
        recorder.emit(ev.RunAborted(reason=t("interrupted by the user (Ctrl-C)")))
        err.print(
            "\n[yellow]"
            + t("Interrupted.")
            + "[/yellow] "
            + t("What finished is kept in {path}", path=recorder.dir)
        )
        raise typer.Exit(130) from None
    finally:
        recorder.close()
    raise typer.Exit(exit_code)


def _resolve_task(task: str | None, file: Path | None) -> str:
    parts = []
    if task:
        parts.append(task)
    if file:
        if not file.exists():
            _fail(t("No such file: {path}", path=file))
        try:
            body = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Users point --file at a PDF, an image or a GBK text file all the time. Crashing inside
            # a UnicodeDecodeError traceback leaves them unable to see which step failed or why.
            _fail(
                t(
                    "Cannot read {path}: it is not UTF-8 text (that happens when it "
                    "points at a binary file or another encoding). Convert it to UTF-8 "
                    "first.",
                    path=file,
                )
            )
        except OSError as exc:
            _fail(t("Cannot read {path}: {error}", path=file, error=exc))
        parts.append(t("# Material under review (from {path})", path=file) + f"\n\n{body}")
    if not parts and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            parts.append(piped)
    if not parts:
        _fail(t("No task given. Pass one as an argument, via --file, or on stdin."))
    return "\n\n".join(parts)


async def _drive(
    engine: Engine,
    task: str,
    stream_json: bool,
    quiet: bool,
    protocol_name: str,
    protocol,
    *,
    prior=None,
    inject: str | None = None,
    resumed_from: str | None = None,
) -> int:
    """Consume the event stream and render it. In JSONL mode it emits one event per line, ready to
    wire into someone else's toolchain.
    """
    outcome = Outcome.EXHAUSTED.value
    result_path: str | None = None
    pending: dict[str, list[str]] = {}
    anomalies: list[str] = []

    async for event in engine.run(task, prior=prior, inject=inject, resumed_from=resumed_from):
        # The outcome must be captured before the rendering branches: in JSON mode the elif chain
        # below never runs, and putting it inside the chain would leave the pipe mode's exit code
        # stuck at its initial value forever.
        if isinstance(event, ev.VerdictFinal):
            outcome, result_path = event.outcome, event.result_path

        # Accumulate "what went wrong" as we go. The event stream holds everything, but one
        # deliberation has tens of thousands of events and an anomaly drowns among them — two real
        # defects today were found only by a person reading the raw events one by one.
        if isinstance(event, ev.TurnEnd) and (event.error or event.truncated):
            why = (
                t("the turn failed — {reason}", reason=str(event.error).splitlines()[0][:70])
                if event.error
                else t(
                    "cut off by the output budget ({n} chars); stance card not accepted",
                    n=event.chars,
                )
            )
            anomalies.append(
                t("round {n} {who}: {why}", n=event.round, who=event.participant, why=why)
            )
        elif isinstance(event, ev.FilesApplied) and event.silently_dropped:
            anomalies.append(
                t(
                    "round {n} {who}: wrote {fences} code fences but landed no files",
                    n=event.round,
                    who=event.participant,
                    fences=event.fences_seen,
                )
            )
        elif isinstance(event, ev.AdoptionEvent):
            anomalies.append(
                t(
                    "round {n} {who}: copied {foe}'s {path} wholesale",
                    n=event.round,
                    who=event.participant,
                    foe=event.adopted_from,
                    path=event.path,
                )
            )

        if stream_json:
            print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)
        elif isinstance(event, ev.RunStart):
            note = ""
            if hasattr(protocol, "proposer"):
                note = " · " + t("proposer {who}", who=protocol.proposer)
            console.print(
                Panel(
                    f"[bold]{task.splitlines()[0][:70]}[/bold]\n\n"
                    + t(
                        "participants {who} · protocol {protocol}{note} · at most {n} rounds",
                        who=", ".join(event.participants),
                        protocol=protocol_name,
                        note=note,
                        n=event.max_rounds,
                    ),
                    title="Sesa",
                    border_style="cyan",
                )
            )
        elif isinstance(event, ev.RoundStart):
            console.rule(
                "[cyan]"
                + t("Round {n}", n=event.round)
                + "[/cyan]  "
                + t("drafted by {who}", who=event.rapporteur)
            )
        elif isinstance(event, ev.TurnStart):
            pending[event.participant] = []
            console.print("[dim]" + t("{who} is speaking…", who=E(event.participant)) + "[/dim]")
        elif isinstance(event, ev.TurnDelta):
            pending.setdefault(event.participant, []).append(event.text)
        elif isinstance(event, ev.TurnEnd):
            if event.error:
                console.print(
                    f"  [red]✗ {E(event.participant)}[/red] {E(event.error.splitlines()[0])}"
                )
            else:
                console.print(
                    f"  [green]✓ {E(event.participant)}[/green] "
                    "[dim]"
                    + t("{chars} chars · {secs}s", chars=event.chars, secs=event.duration_s)
                    + "[/dim]"
                )
                if not quiet:
                    body = "".join(pending.get(event.participant, [])).strip()
                    if body:
                        console.print(Panel(body, title=event.participant, border_style="dim"))
            pending.pop(event.participant, None)
        elif isinstance(event, ev.StanceEmit) and event.degraded:
            state = (
                t("recorded as an unknown stance")
                if event.stance.get("unknown")
                else t("obtained on retry")
            )
            console.print(
                "  [yellow]! "
                + t(
                    "could not parse {who}'s stance card; {state}",
                    who=E(event.participant),
                    state=state,
                )
                + "[/yellow]"
            )
        elif isinstance(event, ev.ConsensusUpdate):
            # This branch runs only outside JSON mode, so a blank line is safe.
            console.print()
            if not protocol.measures_consensus:
                # This protocol structurally produces no peer assessment, so the matrix is
                # necessarily all "unknown". Rendering it and adding "N open disagreements" commits,
                # in the progress display, the error just fixed at the outcome layer: labelling
                # missing data as disagreement.
                console.print(
                    "[dim]"
                    + t(
                        "(in this protocol nobody sees anyone else, so there is no "
                        "peer assessment and no matrix)"
                    )
                    + "[/dim]"
                )
            else:
                console.print(render_matrix_from_event(event))
                console.print(
                    "[dim]"
                    + E(_describe_update(event))
                    + " · "
                    + t("lowest confidence {v}", v=event.min_confidence)
                    + "[/dim]"
                )
        elif isinstance(event, ev.FalseConsensus):
            console.print(
                "[magenta]"
                + t("False consensus detected: {what}", what=E("；".join(event.conflicts)))
                + "[/magenta]"
            )
        elif isinstance(event, ev.HumanInject):
            console.print("[cyan]" + t("Injected: {text}", text=E(event.text)) + "[/cyan]")
        elif isinstance(event, ev.WriterMismatch):
            console.print(
                "[yellow]"
                + t(
                    "rapporteur disagrees with the matrix ({kind}): {detail}",
                    kind=E(event.kind),
                    detail=E(event.detail),
                )
                + "[/yellow]"
            )
        elif isinstance(event, ev.Evidence):
            mark = (
                "[green]" + t("passed") + "[/green]"
                if event.exit_code == 0
                else "[red]" + t("failed({code})", code=event.exit_code) + "[/red]"
            )
            console.print(
                "  [dim]"
                + t("evidence")
                + f"[/dim] {E(event.participant)}: {E(event.cmd)} → {mark}"
            )
        elif isinstance(event, ev.BudgetWarn):
            console.print(
                "[yellow]" + t("Budget warning: {reason}", reason=E(event.reason)) + "[/yellow]"
            )
        elif isinstance(event, ev.ErrorEvent):
            console.print(f"[yellow]! {E(event.where)}：{E(event.message)}[/yellow]")

    if not stream_json:
        label, style = OUTCOME_STYLE.get(outcome, (outcome, "white"))
        console.print()
        console.print(Panel(label, border_style=style))
        if result_path:
            console.print(t("Result: ") + f"[bold]{E(result_path)}[/bold]")
            console.print(t("Minutes: {path}", path=Path(result_path).with_name("REPORT.md")))

    if not stream_json:
        if anomalies:
            console.print("\n[yellow]" + t("What went wrong in this run:") + "[/yellow]")
            for note in anomalies:
                console.print(f"[yellow]  · {E(note)}[/yellow]")
            console.print(
                "[dim]"
                + t("(only known kinds are listed; a new kind will not show up here)")
                + "[/dim]"
            )
        else:
            console.print(
                "\n[dim]"
                + t(
                    "No anomaly of a known kind was found. **That is not the same as "
                    "«everything is fine»** — the list was distilled from potholes "
                    "already hit and cannot recognise a new kind."
                )
                + "[/dim]"
            )

    return exit_code_for(outcome)


def _launch_tui(engine, task: str, participants: list[str], *, stream_json: bool, **resume) -> str:
    """Start the TUI. **`run` and `resume` share this one path** — two implementations would drift
    apart sooner or later.
    """
    if stream_json:
        # Opening a full-screen interface when piped would only pour control characters downstream.
        _fail(
            t(
                "--tui needs a real terminal; drop it when output is redirected, or "
                "pass --json to ask for the event stream explicitly."
            )
        )
    try:
        from .tui import run_tui
    except ImportError:
        _fail(
            t(
                "The TUI dependency is not installed. Run `{hint}` to get it.",
                hint=E(install_hint("tui")),
            )
        )
        raise
    return run_tui(engine, task, participants, **resume)


def exit_code_for(outcome: str) -> int:
    """Outcome → exit code. **There is only one implementation of this.**

    The exit codes let CI decide on them, each grade distinct rather than conflated:
    0 full consensus / 3 consensus with reservations / 2 not reached / 4 this protocol does not
    measure consensus.

    The fourth is listed separately because reflect structurally cannot reach consensus —
    folding it into "not reached" would have the control baseline permanently red in CI.

    It is a function because the TUI needs it too. Two implementations of one set of semantics
    drift apart eventually — and "drift apart" here means the same deliberation gives a
    different exit code depending on whether the TUI was used.
    """
    if outcome == Outcome.CONSENSUS.value:
        return 0
    if outcome == Outcome.CONSENSUS_WITH_RESERVATIONS.value:
        return 3
    if outcome == Outcome.NOT_MEASURED.value:
        return 4
    return 2


def _describe_update(event: ev.ConsensusUpdate) -> str:
    """Split "unresolved" into "opposed" and "not measured" — the two are not the same thing."""
    opposed = sum(1 for row in event.matrix.values() for v in row.values() if v == "disagree")
    unmeasured = sum(1 for row in event.matrix.values() for v in row.values() if v == "unknown")
    if not (opposed or unmeasured):
        return t("no unresolved cells")
    parts = []
    if opposed:
        parts.append(t("{n} cells with an explicit objection", n=opposed))
    if unmeasured:
        parts.append(t("{n} cells not measured (which is not objection)", n=unmeasured))
    return " · ".join(parts)


def render_matrix_from_event(event: ev.ConsensusUpdate) -> str:
    from .types import ConsensusReport

    return render_matrix(
        # `unresolved` is a property of ConsensusReport, not a field — passing it as a field raises
        # TypeError outright. This path **runs only in a real terminal** (pipe and JSON modes bypass
        # it), so the whole test suite and every manual check redirected to a file miss it, while a
        # new user's first `sesa run` crashes for certain.
        ConsensusReport(
            round=event.round,
            matrix=event.matrix,
            min_confidence=event.min_confidence,
            converged=event.state == "converged",
            stalled_rounds=0,
        )
    )


# --------------------------------------------------------------------------- # doctor
# --------------------------------------------------------------------------- #


@app.command()
def init() -> None:
    """The first-run wizard: probe for installed agent CLIs, configure API models, store
    credentials safely.
    """
    from .wizard import run_wizard

    try:
        run_wizard()
    except KeyboardInterrupt:
        err.print("\n[yellow]" + t("Cancelled; nothing was written.") + "[/yellow]")
        raise typer.Exit(130) from None


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="the run_id to resume"),
    inject: str = typer.Option(
        ..., "--inject", "-i", help="what you are adding; it enters the next round's context"
    ),
    rounds: int = typer.Option(
        None, "--rounds", help="how many more rounds (defaults to the configured max)"
    ),
    root: Path = typer.Option(Path(".sesa"), "--root", help="output directory"),
    config_path: Path = typer.Option(None, "--config", "-c"),
    as_json: bool = typer.Option(False, "--json", help="force JSONL event-stream output"),
    tui: bool = typer.Option(
        False,
        "--tui",
        help="full-screen view: watch everyone write, interject / veto a premise / wrap up at any time",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Add one piece of information and carry on from where the last run stopped.

    This turns a deadlock from a terminus into an ordinary step in the loop: they cannot agree →
    the report tells you what is missing → you supply it → they carry on. Everyone's earlier
    turns and positions come along; nothing has to be re-run from the start.
    """
    run_dir = root / "runs" / run_id
    if not run_dir.exists():
        _fail(t("Cannot find {path}. Run `sesa runs` to see what is recorded.", path=run_dir))

    conf = _load_config(config_path)
    try:
        chosen = conf.select(None)
        conf.validate(chosen)
        prior = load_state(
            run_dir,
            chosen,
            max_rounds=rounds or conf.max_rounds,
            share_thinking=conf.share_thinking,
        )
    except (cfg.ConfigError, ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
        return

    recorder = Recorder(root, new_run_id())
    # A resume is a long run too, and being taken away by SIGTERM should leave a run.aborted here as
    # well — without it, an aborted resume looks merely like "the event stream stopped".
    _install_abort_handler(recorder, stream_json=as_json or not sys.stdout.isatty())
    engine = _build_engine(conf, chosen, recorder, rounds or conf.max_rounds, None)
    console.print(
        "[dim]"
        + t(
            "resuming {run}: carrying over {n} rounds of discussion",
            run=run_id,
            n=len(prior.rounds),
        )
        + "[/dim]"
        if not (as_json or not sys.stdout.isatty())
        else ""
    )
    stream_json = as_json or not sys.stdout.isatty()
    if tui:
        # The same path as `run --tui`. **A resume needs it more** — you came back with a specific
        # question, wanting to watch how they answer it and add another sentence at any moment.
        outcome_value = _launch_tui(
            engine,
            prior.task,
            [p.id for p in chosen],
            stream_json=stream_json,
            prior=prior,
            inject=inject,
            resumed_from=run_id,
        )
        recorder.close()
        raise typer.Exit(exit_code_for(outcome_value))

    try:
        code = asyncio.run(
            _drive(
                engine,
                prior.task,
                stream_json,
                quiet,
                conf.protocol,
                engine.protocol,
                prior=prior,
                inject=inject,
                resumed_from=run_id,
            )
        )
    except KeyboardInterrupt:
        err.print(
            "\n[yellow]"
            + t("Interrupted.")
            + "[/yellow] "
            + t("What finished is kept in {path}", path=recorder.dir)
        )
        raise typer.Exit(130) from None
    finally:
        recorder.close()
    raise typer.Exit(code)


@app.command()
def doctor(config_path: Path = typer.Option(None, "--config", "-c")) -> None:
    """Check each participant one by one: can it be called, are its credentials ready."""
    conf = _load_config(config_path)
    if not conf.participants:
        console.print(
            "[yellow]"
            + t("No participants configured yet. Run `sesa init` to start.")
            + "[/yellow]"
        )
        raise typer.Exit(1)

    console.print(
        "[dim]"
        + t("configuration from {sources}", sources=", ".join(str(x) for x in conf.sources))
        + "[/dim]\n"
    )
    table = Table(show_header=True, header_style="bold")
    for column in (
        t("participant"),
        t("adapter"),
        t("model (configured)"),
        t("model (self-reported)"),
        t("status"),
        t("detail"),
    ):
        table.add_column(column)

    async def check_all():
        results = []
        for spec in conf.participants:
            try:
                results.append((spec, await build_adapter(spec).check()))
            except Exception as exc:
                from .adapters.base import CheckResult

                results.append((spec, CheckResult(False, f"{type(exc).__name__}: {exc}")))
        return results

    ok_count = 0
    for spec, result in asyncio.run(check_all()):
        ok_count += bool(result.ok)
        latency = f"{result.latency_s:.1f}s" if result.latency_s else ""
        declared = cfg.declared_model(spec)
        reported = result.reported_model
        # Two separate columns: what the config says, and what it says about itself. **Flag a
        # mismatch on the spot** — that is the signal for "you think you are using A and B is
        # running".
        mismatch = bool(declared and reported and declared not in reported)
        table.add_row(
            spec.id,
            spec.adapter,
            declared or "[dim]" + t("decided by the CLI") + "[/dim]",
            (f"[yellow]{E(reported)} ⚠[/yellow]" if mismatch else E(reported or "—")),
            "[green]" + t("usable") + "[/green]"
            if result.ok
            else "[red]" + t("unusable") + "[/red]",
            f"{E(result.detail)} {latency}".strip(),
        )
    console.print(table)

    if len(conf.participants) < 2:
        console.print(
            "\n[yellow]" + t("A deliberation needs at least 2 participants.") + "[/yellow]"
        )
    if ok_count < 2:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- # participants
# --------------------------------------------------------------------------- #


@participants_app.command("list")
def participants_list(config_path: Path = typer.Option(None, "--config", "-c")) -> None:
    """List the configured participants."""
    conf = _load_config(config_path)
    if not conf.participants:
        console.print(
            "[yellow]"
            + t("No participants configured yet. Run `sesa init` to start.")
            + "[/yellow]"
        )
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("id", t("adapter"), t("model"), t("stance")):
        table.add_column(column)
    for spec in conf.participants:
        table.add_row(
            spec.id,
            spec.adapter,
            spec.model or "—",
            spec.role or "[dim]" + t("(default)") + "[/dim]",
        )
    console.print(table)
    console.print(f"\n[dim]{', '.join(str(s) for s in conf.sources)}[/dim]")


@participants_app.command("add")
def participants_add() -> None:
    """Add a participant to the global library (credentials go into the system keyring by default)."""
    from .wizard import add_one_participant

    try:
        if not add_one_participant():
            console.print("[dim]" + t("No participant was added.") + "[/dim]")
    except KeyboardInterrupt:
        err.print("\n[yellow]" + t("Cancelled.") + "[/yellow]")
        raise typer.Exit(130) from None


@participants_app.command("test")
def participants_test(
    participant_id: str, config_path: Path = typer.Option(None, "--config", "-c")
) -> None:
    """Send one message to a single participant, to check availability and latency."""
    conf = _load_config(config_path)
    try:
        spec = conf.select([participant_id])[0]
    except cfg.ConfigError as exc:
        _fail(str(exc))
        return
    result = asyncio.run(build_adapter(spec).check())
    if result.ok:
        console.print(
            "[green]" + t("usable") + f"[/green] {E(spec.describe())} · {result.latency_s:.1f}s"
        )
        console.print(f"[dim]{E(result.detail)}[/dim]")
    else:
        console.print("[red]" + t("unusable") + f"[/red] {E(spec.describe())}\n{E(result.detail)}")
        raise typer.Exit(1)


@participants_app.command("remove")
def participants_remove(participant_id: str) -> None:
    """Remove a participant from the global configuration (clearing its keyring credential too)."""
    conf = cfg.load(cfg.GLOBAL_CONFIG) if cfg.GLOBAL_CONFIG.exists() else cfg.Config()
    remaining = [p for p in conf.participants if p.id != participant_id]
    if len(remaining) == len(conf.participants):
        _fail(t("No participant {id} in the global configuration", id=E(participant_id)))
    conf.participants = remaining
    cfg.save_global(conf)

    from .credentials import keyring_delete

    cleared = keyring_delete(participant_id)
    console.print("[green]" + t("Removed") + f"[/green] {E(participant_id)}")
    if not cleared:
        # With credentials, "I thought it was deleted" is more dangerous than "I know it was not".
        console.print(
            "[yellow]"
            + t(
                "The keyring credential could not be cleared as well (keyring may not "
                "be installed, the backend may have refused, or nothing was stored). "
                "If something was stored, check by hand:"
            )
            + "[/yellow]\n"
            f"  [dim]keyring get sesa {E(participant_id)}[/dim]"
        )


# --------------------------------------------------------------------------- # runs / report
# --------------------------------------------------------------------------- #


@app.command("runs")
def list_runs(root: Path = typer.Option(Path(".sesa"), "--root", help="output directory")) -> None:
    """List the deliberation records in this directory."""
    runs_dir = root / "runs"
    if not runs_dir.exists():
        console.print(
            "[yellow]" + t("No runs recorded under {path} yet.", path=E(runs_dir)) + "[/yellow]"
        )
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("run_id", t("outcome"), t("task")):
        table.add_column(column)
    for entry in sorted(runs_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        result = entry / "RESULT.json"
        if result.exists():
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # A run killed part-way leaves a half-written RESULT.json. Letting the whole `sesa
                # runs` crash on one bad record hides every other run because of a single broken
                # one. Let it fall through to the "running / interrupted" branch below — which is
                # its true state.
                data = None
            if data is not None:
                table.add_row(entry.name, str(data.get("outcome")), str(data.get("task", ""))[:60])
                continue
        # One without a RESULT.json **must not be skipped**. It is either running or was
        # interrupted, and both are exactly the states the user most needs to see — while this used
        # to `continue` straight past, so "the run in progress" did not exist in the list at all.
        events = entry / "events.jsonl"
        if not events.exists():
            continue
        task, aborted = "", False
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("t") == "run.start":
                task = str(event.get("task", ""))
            elif event.get("t") == "run.aborted":
                aborted = True
        idle = time.time() - events.stat().st_mtime
        if aborted:
            state = "[red]" + t("aborted") + "[/red]"
        elif idle < 120:
            state = "[cyan]" + t("running") + "[/cyan]" + t(" (activity {n}s ago)", n=f"{idle:.0f}")
        else:
            state = "[yellow]" + t("silent for {n} minutes", n=f"{idle / 60:.0f}") + "[/yellow]"
        table.add_row(entry.name, state, task[:60])
    console.print(table)
    console.print(
        "[dim]"
        + t("To watch a running one: `sesa watch` (no argument means the newest).")
        + "[/dim]"
    )


@app.command()
def watch(
    run_id: str = typer.Argument(None, help="omit to follow the newest run"),
    root: Path = typer.Option(Path(".sesa"), "--root", help="output directory"),
    follow: bool = typer.Option(
        True, "--follow/--no-follow", help="keep following until it finishes"
    ),
) -> None:
    """Follow a deliberation as it happens — **without waiting for it to finish**.

    The event stream is written to `events.jsonl` throughout, and there used to be no way in to
    look at it: `report` waits for RESULT.md and `runs` skipped anything unfinished. So once a
    deliberation started it was a black box, and **many problems are precisely in the middle** —
    one round timing out, one participant failing every round, evidence red throughout — none of
    which need leave a trace in the outcome.
    """
    runs_dir = root / "runs"
    if not runs_dir.is_dir():
        _fail(t("No runs recorded under {path} yet.", path=runs_dir))
        return
    if run_id is None:
        candidates = sorted(d for d in runs_dir.iterdir() if (d / "events.jsonl").exists())
        if not candidates:
            _fail(t("No runs recorded under {path} yet.", path=runs_dir))
            return
        run_id = candidates[-1].name
    path = runs_dir / run_id / "events.jsonl"
    if not path.exists():
        _fail(t("Cannot find {path}", path=path))
        return

    console.print(
        "[dim]"
        + t(
            "following {run} — Ctrl-C to stop watching (the run itself is unaffected)",
            run=E(run_id),
        )
        + "[/dim]\n"
    )
    seen, quiet = 0, 0.0
    seen_anomalies: list[str] = []
    try:
        while True:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines[seen:]:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if summary := _describe_event(event):
                    console.print(summary)
                if note := _anomaly_of(event):
                    seen_anomalies.append(note)
                if event.get("t") == "verdict.final":
                    if seen_anomalies:
                        console.print(
                            "\n[yellow]" + t("What went wrong in this run:") + "[/yellow]"
                        )
                        for item in seen_anomalies:
                            console.print(f"[yellow]  · {E(item)}[/yellow]")
                    return
            if len(lines) > seen:
                seen, quiet = len(lines), 0.0
            elif not follow:
                return
            else:
                quiet += 1.0
                if quiet in (60.0, 300.0):
                    console.print(
                        "[yellow]"
                        + t("no new events for {n} minutes", n=f"{quiet / 60:.0f}")
                        + "[/yellow]"
                    )
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]" + t("Stopped watching. The run is still going.") + "[/dim]")


def _anomaly_of(event: dict) -> str | None:
    """Whether this event counts as "something went wrong". Anomalies drown among tens of thousands
    of events and have to be picked out.
    """
    kind, who, index = event.get("t"), event.get("participant"), event.get("round")
    if kind == "turn.end" and event.get("error"):
        return t(
            "round {n} {who}: the turn failed — {reason}",
            n=index,
            who=who,
            reason=str(event["error"]).splitlines()[0][:70],
        )
    if kind == "turn.end" and event.get("truncated"):
        return t(
            "round {n} {who}: cut off by the output budget ({chars} chars); stance card "
            "not accepted",
            n=index,
            who=who,
            chars=event.get("chars"),
        )
    if kind == "files.applied" and not event.get("files") and event.get("fences_seen"):
        return t(
            "round {n} {who}: wrote {fences} code fences but landed no files",
            n=index,
            who=who,
            fences=event["fences_seen"],
        )
    if kind == "adoption":
        return t(
            "round {n} {who}: copied {foe}'s {path} wholesale",
            n=index,
            who=who,
            foe=event.get("adopted_from"),
            path=event.get("path"),
        )
    return None


def _describe_event(event: dict) -> str | None:
    """Render one event as a line of human language. Returning None means it is not worth
    interrupting the reader for.
    """
    kind = event.get("t")
    who = E(str(event.get("participant", "")))
    if kind == "run.start":
        return (
            f"[bold]{E(str(event.get('task', ''))[:70])}[/bold]\n[dim]"
            + t(
                "participants {who} · protocol {protocol}",
                who=E("、".join(event.get("participants") or [])),
                protocol=E(str(event.get("protocol"))),
            )
            + "[/dim]"
        )
    if kind == "round.start":
        return (
            "\n[bold]"
            + t("Round {n}", n=event.get("round"))
            + "[/bold] [dim]"
            + t("drafted by {who}", who=E(str(event.get("rapporteur"))))
            + "[/dim]"
        )
    if kind == "turn.start":
        return "  [dim]" + t("{who} is speaking…", who=who) + "[/dim]"
    if kind == "turn.end":
        if error := event.get("error"):
            return f"  [red]✗ {who}[/red] {E(str(error).splitlines()[0][:90])}"
        mark = "[yellow]" + t("(truncated)") + "[/yellow]" if event.get("truncated") else ""
        return (
            f"  [green]✓[/green] {who} "
            + t(
                "{chars} chars · {secs}s",
                chars=event.get("chars"),
                secs=event.get("duration_s"),
            )
            + f" {mark}"
        )
    if kind == "evidence":
        ok = event.get("exit_code") == 0
        return (
            "  [dim]"
            + t("evidence {who}:", who=who)
            + "[/dim]"
            + (
                "[green]" + t("passed") + "[/green]"
                if ok
                else "[red]" + t("failed({code})", code=event.get("exit_code")) + "[/red]"
            )
        )
    if kind == "files.applied":
        if event.get("files"):
            return (
                "  [dim]"
                + t("{who} wrote {files}", who=who, files=E("、".join(event["files"])))
                + "[/dim]"
            )
        if event.get("fences_seen"):
            return (
                "  [yellow]"
                + t(
                    "{who} wrote {n} code fences but landed no files",
                    who=who,
                    n=event["fences_seen"],
                )
                + "[/yellow]"
            )
        return None
    if kind == "adoption":
        return (
            "  [yellow]⚠ "
            + t(
                "{who} copied {foe}'s {path} wholesale",
                who=who,
                foe=E(str(event.get("adopted_from"))),
                path=E(str(event.get("path"))),
            )
            + "[/yellow]"
        )
    if kind == "consensus.update":
        return f"  [dim]{_describe_update_dict(event)}[/dim]"
    if kind == "error":
        return (
            f"  [red]! {E(str(event.get('where')))}[/red] {E(str(event.get('message', ''))[:100])}"
        )
    if kind == "verdict.final":
        label, style = OUTCOME_STYLE.get(
            str(event.get("outcome")), (str(event.get("outcome")), "white")
        )
        return f"\n[{style}]{t(label)}[/{style}]  [dim]{E(str(event.get('result_path', '')))}[/dim]"
    return None


def _describe_update_dict(event: dict) -> str:
    matrix = event.get("matrix") or {}
    opposed = sum(1 for row in matrix.values() for v in row.values() if v == "disagree")
    unmeasured = sum(1 for row in matrix.values() for v in row.values() if v == "unknown")
    parts = []
    if opposed:
        parts.append(t("{n} cells with an explicit objection", n=opposed))
    if unmeasured:
        parts.append(t("{n} cells not measured", n=unmeasured))
    return " · ".join(parts) or t("no unresolved cells")


@app.command()
def report(
    run_id: str, root: Path = typer.Option(Path(".sesa"), "--root", help="output directory")
) -> None:
    """Print the result of one deliberation."""
    path = root / "runs" / run_id / "RESULT.md"
    if not path.exists():
        _fail(t("Cannot find {path}", path=path))
    console.print(path.read_text(encoding="utf-8"))


@app.command("eval")
def evaluate_runs(
    root: Path = typer.Option(Path(".sesa"), "--root", help="output directory"),
    as_json: bool = typer.Option(
        False, "--json", help="emit structured metrics for an analysis pipeline"
    ),
    min_rounds: int = typer.Option(
        1, "--min-rounds", help="only count runs with at least this many rounds"
    ),
) -> None:
    """Compute metrics over the existing deliberation records — what did the debate actually change.

    Every metric is computed from the persisted event stream, needing no ground truth and no
    further model calls. The column that matters most is "position drift": a persistent value
    near 0 means the extra rounds only added detail, nobody was really convinced, and the cost
    was not justified.
    """
    from .evaluate import collect, to_dict

    everything = collect(root, only_usable=False)
    interrupted = [m for m in everything if not m.usable]
    runs = [m for m in everything if m.usable and m.rounds_used >= min_rounds]
    if not runs:
        console.print(
            "[yellow]" + t("No runs to summarise under {path}.", path=root / "runs") + "[/yellow]"
        )
        if interrupted:
            console.print(
                "[yellow]"
                + t("({n} incomplete records were excluded)", n=len(interrupted))
                + "[/yellow]"
            )
            for m in interrupted[:3]:
                why = m.aborted or t("no verdict.final; the reason was not recorded")
                console.print(f"[dim]  {m.run_id[-13:]}：{E(why)}[/dim]")
        raise typer.Exit(1)

    if as_json:
        print(json.dumps([to_dict(m) for m in runs], ensure_ascii=False, indent=2))
        return

    table = Table(show_header=True, header_style="bold")
    for column, justify in (
        ("run", "left"),
        (t("participants"), "left"),
        (t("rounds"), "right"),
        (t("outcome"), "left"),
        (t("residuals"), "right"),
        (t("granularity"), "right"),
        (t("similarity"), "right"),
        (t("stance change"), "right"),
        (t("degraded"), "right"),
        (t("unresolved"), "right"),
        (t("wall clock"), "right"),
    ):
        table.add_column(column, justify=justify)

    for m in runs:
        opportunities = sum(len(m.participants) for _ in m.rounds[1:])
        table.add_row(
            m.run_id[-13:],
            ",".join(m.participants),
            str(m.rounds_used),
            m.outcome,
            "→".join(str(v) for v in m.residual_counts.values()) or "—",
            _granularity_cell(m),
            _restatement_cell(m),
            f"{m.stance_changes}/{opportunities}" if opportunities else "—",
            f"{m.degraded_rate:.0%}",
            str(m.final_unresolved),
            f"{m.wall_seconds:.0f}s",
        )
    console.print(table)

    if interrupted:
        # An interrupted run has an empty outcome and drift of 0, and mixing it into the mean
        # silently pulls the conclusion towards zero
        console.print(
            "\n[yellow]"
            + t(
                "Excluded {n} unfinished records — averaging them in would drag the "
                "conclusion silently towards zero.",
                n=len(interrupted),
            )
            + "[/yellow]"
        )
        for m in interrupted[:4]:
            why = m.aborted or t("no verdict.final; the reason was not recorded")
            console.print(f"[dim]  {m.run_id[-13:]}：{E(why)}[/dim]")

    ceiling = [m for m in runs if m.evidence_ceiling]
    if ceiling:
        console.print(
            "\n[yellow]"
            + t(
                "In {n} runs every participant's self-test passed — the task may be too "
                "easy for this line-up: they get it right alone, so debate has nothing "
                "to improve.",
                n=len(ceiling),
            )
            + "[/yellow]\n[dim]"
            + t(
                "Observed: on a parsing task with 6 error edges, all four "
                "implementations from the debate and self-review arms scored full "
                "marks on the held-out tests, while debate cost 17% more words and 15% "
                "more time. Under a ceiling effect no method can show a difference, and "
                "multi-round debate is pure markup — consider `ensemble`, or a harder "
                "task."
            )
            + "[/dim]"
        )

    multi = [m for m in runs if m.rounds_used >= 2]
    if multi:
        degraded = sum(m.degraded_rate for m in multi) / len(multi)
        console.print(
            "\n[dim]"
            + t(
                "{n} multi-round runs · mean degradation {rate} (share of stance cards "
                "that fell back to the degraded extraction)",
                n=len(multi),
                rate=f"{degraded:.0%}",
            )
            + "[/dim]"
        )
        console.print(
            "[dim]"
            + t(
                "«Residuals» counts reservations per round. Items are compared as exact "
                "strings, and in practice zero items are byte-identical, so «dropped» "
                "vs «added» cannot be told apart — only the count and semantic "
                "similarity are reported. «Granularity» is count × mean length; "
                "similarity correlates strongly with it (r≈0.85), so the two have to be "
                "read together."
            )
            + "[/dim]"
        )
        washed = [m for m in multi if m.residual_flow]
        if washed:
            from .semantic import SemanticUnavailable

            trend = sum(m.residual_trend for m in washed) / len(washed)
            console.print(
                "[dim]"
                + t(
                    "Mean net change in reservation count {trend} — negative means the "
                    "disagreement is converging, near zero means the total does not "
                    "fall with more rounds.",
                    trend=f"{trend:+.1f}",
                )
                + "[/dim]"
            )
            try:
                sims = [v for m in washed if (v := m.residual_similarity()) is not None]
            except SemanticUnavailable:
                console.print(
                    "[dim]"
                    + t(
                        "Counts cannot show whether the content moved. Install "
                        "`{hint}` and this table will report semantic similarity.",
                        hint=E(install_hint("semantic")),
                    )
                    + "[/dim]"
                )
            else:
                if sims:
                    console.print(
                        "[dim]"
                        + t(
                            "Median similarity between newly added residuals and the "
                            "previous round {v} — higher looks like restating in "
                            "different words, lower means moving on to new issues.",
                            v=f"{sum(sims) / len(sims):.2f}",
                        )
                        + "[/dim]"
                    )
                    base = washed[0]
                    if any(not base.comparable_with(m) for m in washed[1:]):
                        console.print(
                            "[yellow]"
                            + t(
                                "These runs differ a lot in residual granularity (the "
                                "«granularity» column), so their similarity scores are "
                                "not directly comparable — the score correlates with "
                                "item count (r≈0.83) and item length (r≈0.86), so "
                                "comparing across granularities compares prose style, "
                                "not deliberation."
                            )
                            + "[/yellow]"
                        )
                    if any(m.residual_granularity[2] > 0.25 for m in washed):
                        console.print(
                            "[yellow]"
                            + t(
                                "In runs marked *, item length swings a lot between "
                                "rounds, so their cross-round similarity trend is "
                                "contaminated by style change too."
                            )
                            + "[/yellow]"
                        )

    _report_adoption(root, runs)


def _report_adoption(root: Path, runs: list) -> None:
    """Report "someone lifted a rival's code wholesale".

    It reports the fact and does not judge it good or bad — that depends on whether the
    execution evidence improved. In the measurements all three copies came with a drop in score,
    but the difference between groups (debate regressing 4/16 vs reflect 0/16) has Fisher
    p=0.101, **not significant**, which will not support the claim that copying is harmful.
    """
    from .evaluate import code_adoption

    measurable, hits = 0, []
    for metrics in runs:
        report = code_adoption(root / "runs" / metrics.run_id)
        if not report.measurable:
            continue
        measurable += 1
        hits.extend((metrics.run_id, a) for a in report.events)

    if not measurable:
        return
    if not hits:
        console.print(
            "\n[dim]"
            + t(
                "{n} runs could be checked for wholesale copying; none was found. (Only "
                "participants whose files the engine writes can be checked; an agent "
                "CLI writes its own files, so the code never enters the statement.)",
                n=measurable,
            )
            + "[/dim]"
        )
        return

    console.print(
        "\n[yellow]"
        + t(
            "{hits} instances of wholesale copying across {n} runs:",
            hits=len(hits),
            n=measurable,
        )
        + "[/yellow]"
    )
    for run_id, a in hits:
        console.print(
            "[yellow]  "
            + t(
                "{run} round {n} {who} copied {foe}'s {path} (like the peer {peer}, "
                "like their own previous round {own})",
                run=run_id[-13:],
                n=a.round,
                who=E(a.participant),
                foe=E(a.adopted_from),
                path=E(a.path),
                peer=f"{a.similarity_to_peer:.2f}",
                own=f"{a.similarity_to_own:.2f}",
            )
            + "[/yellow]"
        )
    console.print(
        "[dim]"
        + t(
            "This is a statement of fact, not a claim that copying is bad — what "
            "settles that is whether the execution evidence improved. It is still "
            "worth a look: converging on a peer and converging on the right answer are "
            "two different things."
        )
        + "[/dim]"
    )


@app.command("judge")
def judge_run(
    run_id: str = typer.Argument(..., help="the run_id to judge"),
    judge: str = typer.Option(
        ..., "--judge", "-j", help="participant id acting as judge (must be configured)"
    ),
    root: Path = typer.Option(Path(".sesa"), "--root", help="output directory"),
    config_path: Path = typer.Option(None, "--config", "-c"),
    repeat: int = typer.Option(
        1, "--repeat", help="judge this many times, to see the agreement rate"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Have a model read the transcript directly and answer "did the positions substantively
    change".

    Neither counting nor embedding proxies can answer that question; a model can simply
    understand the semantics. The judge reads a transcript of something **already over** and
    influences no deliberation — a different matter entirely from the "no referee" principle in
    the design.

    Three defences: quotations must pass a **mechanical check** against the transcript, and an
    invented verdict is void; the judge may not be a participant in that run; ``--repeat`` shows
    one judge's certainty — note that is **not** reliability, for which you need a different
    ``--judge`` and a cross-model judgement.
    """
    from . import judge as jd
    from .adapters import build as build_adapter
    from .types import Done, TextDelta

    run_dir = root / "runs" / run_id
    if not run_dir.exists():
        _fail(t("Cannot find {path}", path=run_dir))
    conf = _load_config(config_path)
    try:
        spec = conf.select([judge])[0]
    except cfg.ConfigError as exc:
        _fail(str(exc))
        return

    transcript, participants = jd.build_transcript(run_dir)
    try:
        jd.assert_not_participant(judge, participants, conf.participants)
    except ValueError as exc:
        _fail(str(exc))
        return

    async def ask() -> str:
        parts: list[str] = []
        async for chunk in build_adapter(spec).stream(
            jd.build_prompt(transcript), timeout=conf.turn_timeout
        ):
            if isinstance(chunk, TextDelta):
                parts.append(chunk.text)
            elif isinstance(chunk, Done):
                pass
        return "".join(parts)

    reports, stray = [], []
    for _ in range(max(1, repeat)):
        report = jd.parse(asyncio.run(ask()), transcript, run_id, judge)
        stray += report.drop_unknown_participants(participants)
        reports.append(report)

    if as_json:
        print(json.dumps([jd.to_dict(r) for r in reports], ensure_ascii=False, indent=2))
        return

    report = reports[0]
    table = Table(show_header=True, header_style="bold")
    for column in (t("participant"), t("verdict"), t("quote check"), t("reasoning")):
        table.add_column(column)
    for verdict in report.verdicts:
        table.add_row(
            verdict.participant,
            verdict.verdict,
            "[green]" + t("passed") + "[/green]"
            if verdict.trustworthy
            else "[red]" + t("failed · discarded") + "[/red]",
            verdict.reason[:60],
        )
    console.print(table)
    if report.overall:
        console.print("\n" + t("Overall: {what}", what=report.overall))
    console.print(f"\n[dim]{jd.change_is_not_quality()}[/dim]")
    if stray:
        console.print(
            "[yellow]"
            + t(
                "Dropped {n} participants that do not exist ({who}) — the judge "
                "mistook a filename or heading for a participant.",
                n=len(stray),
                who=", ".join(sorted({v.participant for v in stray})[:3]),
            )
            + "[/yellow]"
        )
    if report.rejected:
        console.print(
            "[yellow]"
            + t(
                "{n} verdicts were discarded because their quotes could not be verified "
                "against the transcript — the judge may be inventing them.",
                n=len(report.rejected),
            )
            + "[/yellow]"
        )
    rate = report.verification_rate
    style = "green" if rate >= 0.8 else ("yellow" if rate >= 0.5 else "red")
    console.print(
        f"[{style}]" + t("Judge quote-verification rate {rate}", rate=f"{rate:.0%}") + f"[/{style}]"
    )
    if rate < 0.5:
        console.print(
            "[red]"
            + t(
                "That rate is too low to trust this judge on this task — judge again "
                "with a different model."
            )
            + "[/red]"
        )
    if len(reports) > 1:
        rates = jd.agreement(reports)
        # What cannot be computed has to be said too, and it has to say whether it was "never
        # judged" or "judged and rejected".
        gaps = jd.agreement_gaps(reports)
        console.print(
            "\n[dim]"
            + t(
                "Agreement across {n} runs of the same judge: {rates}",
                n=len(reports),
                rates=rates,
            )
            + "[/dim]"
        )
        console.print(
            "[dim]"
            + t(
                "Note: repeating the same judge measures **determinism**, not "
                "correctness — errors from a homogeneous jury are highly correlated "
                "(ρ≈0.95 observed), so they agree when they are wrong together. To "
                "estimate reliability you need a cross-model judgement: run again with "
                "a different `--judge` and compare."
            )
            + "[/dim]"
        )
        for pid, why in gaps.items():
            console.print(f"[yellow]{E(pid)}：{E(why)}[/yellow]")
        if rates and min(rates.values()) < 0.7:
            console.print(
                "[yellow]"
                + t(
                    "Not even determinate — this judge's output is unstable on this "
                    "task and should not be trusted."
                )
                + "[/yellow]"
            )


@app.command()
def calibrate(
    model: str = typer.Option(None, "--model", help="the embedding model to calibrate"),
) -> None:
    """Calibrate semantic comparison on known cases, and see whether it separates "reworded" from
    "a new question".

    **Look at this table before using it.** Three metrics in this project were used first and
    calibrated afterwards, and two of them were inverted across the interval that matters: "same
    conclusion, reworded" scored higher than "opposite conclusions". A metric that cannot
    separate the two has every number it produces read as though it meant what its name says.
    """
    from . import semantic

    name = model or semantic.DEFAULT_MODEL
    state = semantic.availability(name)
    if not state.ok:
        console.print(
            "[yellow]" + t("Semantic comparison is unavailable") + f"[/yellow]\n{E(state.detail)}"
        )
        raise typer.Exit(1)

    table = Table(show_header=True, header_style="bold")
    for column in (
        t("case"),
        t("expected"),
        t("similarity"),
        t("verdict at threshold {v}", v=semantic.DEFAULT_THRESHOLD),
        "",
    ):
        table.add_column(column)
    rows = semantic.calibrate(name)
    for case, score, expected, verdict in rows:
        table.add_row(
            case,
            t("rewording") if expected else t("new issue"),
            f"{score:.3f}",
            t("rewording") if verdict else t("new issue"),
            "[green]✓[/green]" if verdict == expected else "[red]✗ " + t("wrong") + "[/red]",
        )
    console.print(table)

    restate = [s for _, s, e, _ in rows if e]
    novel = [s for _, s, e, _ in rows if not e]
    gap = min(restate) - max(novel)
    console.print(
        "\n"
        + t(
            "rewording lowest {low} · new issue highest {high} · gap {gap}",
            low=f"{min(restate):.3f}",
            high=f"{max(novel):.3f}",
            gap=f"{gap:+.3f}",
        )
    )
    if gap <= 0.05:
        console.print(
            "[red]"
            + t("The two classes cannot be separated — no usable threshold exists.")
            + "[/red]\n"
            + t(
                "This model measures topic similarity, not equivalence of claims. **Do "
                "not draw conclusions from its numbers**; calibrate a different model, "
                "or use another method."
            )
        )
        raise typer.Exit(2)
    console.print(
        "[green]"
        + t(
            "The two classes separate; a threshold of {v} is suggested",
            v=f"{(min(restate) + max(novel)) / 2:.2f}",
        )
        + "[/green]"
    )


@app.command()
def version() -> None:
    """Show the version and the available adapters / protocols."""
    from . import __version__

    console.print(f"sesa {__version__}")
    console.print(t("adapters: {list}", list=", ".join(available_adapters())))
    console.print(t("protocols: {list}", list=", ".join(available_protocols())))


if __name__ == "__main__":  # pragma: no cover
    app()
