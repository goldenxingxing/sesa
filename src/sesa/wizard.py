"""The `sesa init` interactive wizard.

Participants are configured **once and reused for a long time** — this is the
make-or-break of the first-run experience.

Credentials go into the system keyring by default and are **never written in
plaintext to the config file**: an open-source project should not teach people
to store keys in the clear.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import config as cfg
from ._install import install_hint
from .credentials import env_hints_for, keyring_has, keyring_set
from .i18n import t
from .types import ParticipantSpec

console = Console()


class _IntPrompt(IntPrompt):
    """Integer input whose **error message speaks the interface's language**.

    rich ships an English "Please enter a valid integer number". In a wizard
    running in another language, a user who has been guided along in their own
    language suddenly reads English at the exact moment they are stuck — and
    concludes they hit a program error rather than a typo.
    """

    @property
    def validate_error_message(self) -> str:  # type: ignore[override]
        return "[prompt.invalid]" + t("Please enter a whole number")


def _store_credential(participant_id: str, base_url: str | None, env_hint: str | None) -> dict:
    """Return **how to reach the credential**, never the credential itself."""
    if env_hint is None:
        console.print("  [dim]" + t("This preset needs no credential.") + "[/dim]")
        return {}

    hints = [env_hint, *env_hints_for(base_url)]
    seen = list(dict.fromkeys(h for h in hints if h))

    # **If the keyring already holds it, don't make them paste it again.**
    # After "clear and start over" the credential is still in the keyring — clearing only touches
    # the config file. Without asking, the user has to dig out every key again, and by then they
    # have usually closed the page they copied it from.
    if keyring_has(participant_id):
        console.print(
            "  [green]"
            + t("A credential for {id} is already in the keyring.", id=participant_id)
            + "[/green]"
        )
        if Confirm.ask("  " + t("Reuse it?"), default=True):
            return {"api_key": f"keyring:{participant_id}"}

    console.print(
        "  "
        + t(
            "Where should the credential live? [dim](1 = system keyring, recommended; "
            "2 = environment variable {env}; 3 = skip for now)[/dim]",
            env=(seen[0] if seen else t("(none)")),
        )
    )
    choice = Prompt.ask("  " + t("Choose"), choices=["1", "2", "3"], default="1")

    if choice == "2":
        if not seen:
            console.print(
                "  [dim]" + t("This preset has no conventional variable name; pick one.") + "[/dim]"
            )
        name = Prompt.ask("  " + t("Variable name"), default=(seen[0] if seen else ""))
        if not name.strip():
            console.print("  [yellow]" + t("Nothing entered; skipping for now.") + "[/yellow]")
            return {}
        return {"api_key_env": name.strip()}
    if choice == "3":
        # **"Skip for now" means skip.** This used to write `api_key_env` anyway, turning something
        # the user explicitly declined into a fake configuration; with `seen` empty it wrote the
        # literal "(none)" — a variable that does not exist, so doctor could only report "cannot
        # read it" and the user had no idea why.
        console.print(
            "  [yellow]"
            + t("No credential configured. Add one later with `sesa participants add`.")
            + "[/yellow]"
        )
        return {}

    key = Prompt.ask("  " + t("Paste the API key"), password=True)
    if not key.strip():
        console.print("  [yellow]" + t("Nothing entered; skipping for now.") + "[/yellow]")
        return {"api_key_env": seen[0]} if seen else {}
    try:
        keyring_set(participant_id, key.strip())
    except Exception:
        # Catching only ImportError is not enough: keyring being **installed but unusable** is the
        # common case — no usable backend (headless Linux), a locked keychain, insufficient
        # permissions. None of those raise ImportError, and the wizard would crash in the user's
        # face when it has a perfectly good fallback.
        console.print(
            "  [yellow]"
            + t(
                "keyring is not available (`{hint}`); falling back to an environment variable.",
                hint=escape(install_hint("keyring")),
            )
            + "[/yellow]"
        )
        return {"api_key_env": (seen[0] if seen else "SESA_API_KEY")}
    console.print("  [green]" + t("Stored in the system keyring") + "[/green]")
    return {"api_key": "keyring"}


def _unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    index = 2
    while f"{base}{index}" in taken:
        index += 1
    return f"{base}{index}"


def add_cli_participant(key: str, taken: set[str]) -> ParticipantSpec:
    preset = cfg.CLI_PRESETS[key]
    pid = _unique_id(key, taken)
    if hint := preset.get("hint"):
        # Dangerous switches are never pre-set, but the user should know they exist — see DESIGN.md
        # §10.
        console.print("  [yellow]" + t("Note: {hint}", hint=t(hint)) + "[/yellow]")
    # The presets are module-level constants, fixed at import time, when the language has not been
    # resolved yet. Translate them **where they are used**: under a Chinese UI the role written into
    # the config should be Chinese too.
    role = Prompt.ask("  " + t("Stance for {id}", id=pid), default=t(preset["role"]))
    return ParticipantSpec(
        id=pid,
        adapter=preset["adapter"],
        role=role,
        options={"command": preset["command"], "prompt": preset["prompt"], "cwd": "{workspace}"},
    )


def add_api_participant(key: str, taken: set[str]) -> ParticipantSpec:
    preset = cfg.API_PRESETS[key]
    pid = _unique_id(key, taken)
    model = Prompt.ask("  " + t("Which model for {id}", id=pid), default=preset["model"])
    role = Prompt.ask(
        "  " + t("Stance for {id}", id=pid),
        default=t("A rigorous sceptic who hunts for holes in the argument"),
    )
    options: dict = {}
    if preset.get("base_url"):
        options["base_url"] = preset["base_url"]
    options.update(_store_credential(pid, preset.get("base_url"), preset.get("env")))
    return ParticipantSpec(
        id=pid, adapter=preset["adapter"], model=model, role=role, options=options
    )


def add_one_participant() -> bool:
    """`sesa participants add`: add one to the global library. Returns whether
    anything changed."""
    existing = cfg.load(cfg.GLOBAL_CONFIG) if cfg.GLOBAL_CONFIG.exists() else cfg.Config()
    taken = {p.id for p in existing.participants}

    console.print("\n[bold]" + t("Add a participant") + "[/bold]")
    options = [("cli", k) for k in cfg.CLI_PRESETS] + [("api", k) for k in cfg.API_PRESETS]

    for index, (kind, key) in enumerate(options, 1):
        presets = cfg.CLI_PRESETS if kind == "cli" else cfg.API_PRESETS
        installed = ""
        if kind == "cli":
            installed = (
                "  [green]" + t("installed") + "[/green]"
                if key in cfg.detect_installed_clis()
                else "  [dim]" + t("not installed") + "[/dim]"
            )
        console.print(f"  {index}. {presets[key]['label']}{installed}")

    raw = Prompt.ask("  " + t("Pick one (Enter to cancel)"), default="")
    if not raw.strip():
        return False
    if not raw.isdecimal() or not 1 <= int(raw) <= len(options):
        console.print("  [yellow]" + t("Please enter a number from the list.") + "[/yellow]")
        return False

    kind, key = options[int(raw) - 1]
    spec = add_cli_participant(key, taken) if kind == "cli" else add_api_participant(key, taken)
    existing.participants.append(spec)
    path = cfg.save_global(existing)
    console.print(
        "\n[green]"
        + t("Added {what} → {path}", what=spec.describe(), path=escape(str(path)))
        + "[/green]"
    )
    return True


#: API preset → the CLI preset from the same vendor. The two lists overlap by vendor, and users
#: assume the second is how you give the first its key.
_SAME_VENDOR = {"anthropic": "claude", "kimi": "kimi", "deepseek": "dsh"}


def _parse_choices(raw: str, count: int) -> tuple[list[int], list[str]]:
    """Parse input like "1,2,5" into a list of numbers.

    Give people a numbered list and they will want to enter several at once —
    the first real user typed exactly ``1 ,2 ,5``, and the code accepted only a
    single number and answered "please enter a number from the list".
    **His instinct was right; the gap was here.**

    Separators accepted: comma (half- and full-width), ideographic comma,
    whitespace — all of them get typed in practice.

    Returns ``(valid numbers, unrecognised fragments)``. **One unrecognised
    fragment cancels the whole line**; the caller decides how to say so.
    Partial application leaves people unsure how many went in, and each
    addition here means pasting a key — cleaning up a mistake costs far more
    than retyping the line.
    """
    import re

    pieces = [p for p in re.split(r"[,，、\s]+", raw.strip()) if p]
    numbers, bad = [], []
    for piece in pieces:
        if piece.isdecimal() and 1 <= int(piece) <= count:
            numbers.append(int(piece))
        else:
            bad.append(piece)
    return numbers, bad


def run_wizard() -> None:
    console.print("\n[bold cyan]Sesa[/bold cyan] —— " + t("open sesame") + "\n")
    console.print(
        t(
            "A deliberation needs at least 2 participants. Different agents and "
            "different models at the same table are what make disagreement "
            "informative — the same model with a different stance counts as a "
            "different participant.\n"
        )
    )

    existing = cfg.load(cfg.GLOBAL_CONFIG) if cfg.GLOBAL_CONFIG.exists() else cfg.Config()
    if existing.participants:
        console.print(
            "[dim]"
            + t("Existing configuration: {path}", path=escape(str(cfg.GLOBAL_CONFIG)))
            + "[/dim]"
        )
        table = Table(show_header=True, header_style="bold")
        for column in ("id", t("adapter"), t("model")):
            table.add_column(column)
        for spec in existing.participants:
            table.add_row(spec.id, spec.adapter, spec.model or "—")
        console.print(table)
        console.print(
            "  [dim]"
            + t(
                "1 = keep what is there and add more; 2 = clear and start over "
                "(keys stay in the keyring and can be reused when a participant "
                "of the same id comes back); 3 = change nothing and quit"
            )
            + "[/dim]"
        )
        # **There has to be a way to start over.** This used to be a single "add more participants?"
        # yes/no: yes could only append, no quit outright. Once the first attempt went wrong — and a
        # first attempt easily does — the only remaining route was deleting the config file by hand,
        # and the wizard never said where it was or what to delete.
        action = Prompt.ask("  " + t("What now"), choices=["1", "2", "3"], default="1")
        if action == "3":
            console.print("[dim]" + t("Nothing changed.") + "[/dim]")
            return
        if action == "2":
            existing.participants = []
            console.print(
                "  [yellow]"
                + t("Participant list cleared (not written yet; finish the wizard to apply).")
                + "[/yellow]\n  [dim]"
                + t(
                    "Keyring credentials were left alone — you will be asked whether "
                    "to reuse them when a participant of the same id is added back."
                )
                + "[/dim]"
            )

    chosen = list(existing.participants)
    taken = {p.id for p in chosen}
    added_api: set[str] = set()

    # ── 1. agent CLIs already installed ──────────────────────────────
    detected = [k for k in cfg.detect_installed_clis() if k not in taken]
    if detected:
        console.print("\n[bold]" + t("Step 1: agent CLIs installed on this machine") + "[/bold]")
        console.print(
            "[dim]"
            + t(
                "They use **their own logins** (`claude` / `kimi` each have an "
                "account); sesa does not need your API key."
            )
            + "[/dim]"
        )
        for key in detected:
            label = cfg.CLI_PRESETS[key]["label"]
            if Confirm.ask("  " + t("Add {label} ({key})?", label=label, key=key), default=True):
                chosen.append(add_cli_participant(key, taken))
                taken.add(chosen[-1].id)
    elif installed := cfg.detect_installed_clis():
        # **"They are all already added" is not "none were detected".**
        # The latter makes the user think sesa cannot find their claude and sends them off to check
        # PATH — when nothing is wrong. Same old failure in a new place: saying "there is nothing"
        # when the truth is "there is nothing left to do".
        already = "、".join(cfg.CLI_PRESETS[k]["label"] for k in installed if k in taken)
        console.print(
            "\n[dim]"
            + t(
                "The agent CLIs on this machine ({names}) are all in the "
                "configuration already; nothing to add.",
                names=already,
            )
            + "[/dim]"
        )
    else:
        console.print(
            "\n[dim]"
            + t(
                "No agent CLI detected (looked for claude / kimi / codex / dsh / "
                "gemini / aider / cursor-agent)."
            )
            + "[/dim]"
        )

    # ── 2. API models ────────────────────────────────────────────────
    #
    # **Several can be added**: this is a loop; it asks again after each one
    # and only a blank line ends it. Yet the first real user immediately asked
    # "why can I only pick one" — and every part of that was wording:
    #   1. "pick one" reads as "only one allowed"
    #   2. what was added stayed in the list unmarked, so nothing looked
    #      different after a successful addition
    #   3. "Enter to skip" was said once at the top and never repeated
    # The loop was right; the words were wrong.
    console.print(
        "\n[bold]"
        + t("Step 2: models reached directly over an API")
        + "[/bold][dim]"
        + t(" (several allowed; press Enter if you need none)")
        + "[/dim]"
    )
    console.print(
        "[dim]"
        + t(
            "These are **additional participants**. They are [bold]not[/bold] how "
            "you give the CLIs above a key — those already work.\n"
            "Add one here only if you want a model that has no CLI installed at "
            "the table. Otherwise just press Enter."
        )
        + "[/dim]"
    )
    options = list(cfg.API_PRESETS)
    while True:
        for index, key in enumerate(options, 1):
            label = cfg.API_PRESETS[key]["label"]
            # Mark what was added. Otherwise the user stares at an unchanged list with no way to
            # tell whether the last step took effect.
            mark = "  [green]" + t("(added)") + "[/green]" if key in added_api else ""
            console.print(f"  {index}. {label}{mark}")
        raw = Prompt.ask(
            "  "
            + t(
                "Enter numbers (several at once is fine, e.g. 1,2,5), or press "
                "Enter to finish ({count} participants so far)",
                count=len(chosen),
            ),
            default="",
        )
        if not raw.strip():
            break
        numbers, bad = _parse_choices(raw, len(options))
        if bad:
            # **Add nothing at all.** Partial application leaves people unsure how many went in —
            # and each addition here means pasting a key, so cleaning up a mistake costs far more
            # than retyping the line.
            console.print(
                "  [yellow]"
                + t(
                    "Did not recognise: {bad}. Enter numbers between 1 and {count}, "
                    "separated by commas or spaces.",
                    bad="、".join(bad),
                    count=len(options),
                )
                + "[/yellow]"
            )
            continue
        for number in numbers:
            picked = options[number - 1]
            # **The same vendor is already at the table as a CLI.**
            # The two lists overlap by vendor (Claude Code ↔ Claude API, Kimi CLI ↔ Kimi, DeepSeek
            # Harness ↔ DeepSeek). The first real user thought picking Claude API here was how you
            # gave the claude CLI above its key; he ended up with two extra participants, pasted two
            # keys for nothing, and one of them was unusable anyway.
            twin = _SAME_VENDOR.get(picked)
            if twin and any(spec.id == twin for spec in chosen):
                console.print(
                    "  [yellow]"
                    + t(
                        "Note: {installed} is already at the table (added in step 1). "
                        "Adding {api} gives you **a second, independent participant** "
                        "— it is not how you give the first one a key.",
                        installed=cfg.CLI_PRESETS[twin]["label"],
                        api=cfg.API_PRESETS[picked]["label"],
                    )
                    + "[/yellow]"
                )
                if not Confirm.ask("  " + t("Add it anyway?"), default=False):
                    continue
            chosen.append(add_api_participant(picked, taken))
            taken.add(chosen[-1].id)
            added_api.add(picked)
            console.print(
                "  [green]"
                + t("Added {label}", label=cfg.API_PRESETS[picked]["label"])
                + "[/green]"
            )

    if len(chosen) < 2:
        console.print(
            "\n[yellow]"
            + t(
                "Only {count} participant(s) configured; a deliberation needs at "
                "least 2. The configuration is still saved — add more later with "
                "`sesa participants add`.",
                count=len(chosen),
            )
            + "[/yellow]"
        )

    # ── 3. deliberation settings ─────────────────────────────────────
    console.print("\n[bold]" + t("Deliberation settings") + "[/bold]")
    existing.participants = chosen
    existing.protocol = Prompt.ask(
        "  " + t("Protocol"),
        choices=["debate", "ensemble", "council", "adversarial"],
        default=existing.protocol,
    )
    existing.max_rounds = _IntPrompt.ask("  " + t("Maximum rounds"), default=existing.max_rounds)
    # **Unlimited by default.** A complex task legitimately running for hours is normal, and total
    # time is already bounded structurally by max_rounds × turn_timeout; a second cap only cuts a
    # run in half.
    console.print(
        "  [dim]"
        + t(
            "Wall-clock cap: 0 = unlimited (recommended). Total time is already "
            "bounded by rounds × per-turn timeout; a second cap here only cuts a "
            "run off halfway."
        )
        + "[/dim]"
    )
    wall = _IntPrompt.ask(
        "  " + t("Wall-clock cap for one run (seconds, 0 = unlimited)"),
        default=int(existing.max_wall_seconds or 0),
    )
    existing.max_wall_seconds = float(wall) if wall > 0 else None

    path = cfg.save_global(existing)
    console.print("\n[green]" + t("Written to {path}", path=escape(str(path))) + "[/green]")

    # Detection answers "is it installed", not "does it work". Without checking here, a new user
    # walks away with a configuration that looks complete and is entirely broken — a real failure:
    # three agent CLIs all written into the config, one not logged in, one with no model set, one
    # crashing outright, and none of it surfaced until the first run.
    if chosen and Confirm.ask(
        "\n" + t("Check now whether each participant can actually be reached?"), default=True
    ):
        broken = _verify(chosen)
        if broken:
            console.print(
                "\n[yellow]"
                + t(
                    "{broken}/{total} participants are currently unusable. The "
                    "configuration is saved; run `sesa doctor` again after fixing them.",
                    broken=len(broken),
                    total=len(chosen),
                )
                + "[/yellow]"
            )
            if len(chosen) - len(broken) < 2:
                console.print(
                    "[yellow]"
                    + t(
                        "Fewer than 2 are usable, so a deliberation cannot start — "
                        "it needs at least two participants that can speak."
                    )
                    + "[/yellow]"
                )
        else:
            console.print("\n[green]" + t("All usable.") + "[/green]")

    console.print("\n" + t("Next:"))
    console.print("  [bold]sesa doctor[/bold]                 " + t("re-check participants"))
    console.print('  [bold]sesa run "..."[/bold]               ' + t("start a deliberation"))


def _verify(specs: list) -> list[str]:
    """Call each one for real; return the ids that are unusable."""
    import asyncio

    from .adapters import build as build_adapter
    from .adapters.base import CheckResult

    async def check_all():
        out = []
        for spec in specs:
            try:
                out.append((spec, await build_adapter(spec).check()))
            except Exception as exc:
                out.append((spec, CheckResult(False, f"{type(exc).__name__}: {exc}")))
        return out

    broken = []
    console.print()
    for spec, result in asyncio.run(check_all()):
        if result.ok:
            console.print(f"  [green]✓[/green] {escape(spec.id)}")
        else:
            broken.append(spec.id)
            # **The first line alone is not enough.**
            # A CLI adapter's message looks like "dsh: exit code 1\n<the real reason>" — the reason
            # is everything after line one. Printing only the first line leaves the user with "exit
            # code 1" while a message that names the cause is sitting right there (observed: dsh's
            # "The requested module 'node:zlib' does not provide an export named
            # 'createZstdDecompress'", which points straight at an old Node).
            # Same mistake as the one the adapter's own comment warns about — "reporting only the
            # exit code throws away the answer we already have" — committed one layer up, in
            # rendering.
            lines = [ln.strip() for ln in (result.detail or "").splitlines() if ln.strip()]
            console.print(f"  [red]✗[/red] {escape(spec.id)}")
            if not lines:
                console.print("      [dim]" + t("(no reason was given)") + "[/dim]")
            for line in lines[:6]:
                console.print(f"      [dim]{escape(line[:160])}[/dim]")
            if len(lines) > 6:
                console.print(
                    "      [dim]"
                    + t(
                        "…{count} more lines; `sesa participants test {id}` shows them all",
                        count=len(lines) - 6,
                        id=escape(spec.id),
                    )
                    + "[/dim]"
                )
    return broken
