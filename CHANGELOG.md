# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## 0.1.0 — first public version

A working core for a multi-agent deliberation engine. **Its selling point is not "debate
produces better conclusions"** — our own measurements do not support that sentence; see the
opening of the README and section 14 of DESIGN.

### What it can do

- **Three adapters**: `cli` (any agent CLI, adding a new one takes no code),
  `openai_compat`, `anthropic`; third parties can register their own
- **Five deliberation protocols**: `ensemble` / `debate` / `council` / `adversarial`, plus
  `reflect` — the no-speaker control baseline, which separates "the effect of debate" from
  "a few more rounds of thinking"
- **The consensus assessment is default-deny**: only a parseable, explicit `agree` resolves a
  cell; "someone objected" is accounted separately from "the engine did not measure it"
- **Six grades of outcome**, with `deadlock` / `exhausted` never written up as consensus;
  `not_measured` is a grade of its own — `reflect` structurally produces no peer assessment,
  and reporting it as "not reached" would label missing data as disagreement
- **Premises are first-class**: `premises` is a field of its own in the stance card, a debate
  round requires attacking the premises before the conclusion, and `RESULT.md` gives them a
  section of their own together with the command that overturns one
- **Code tasks**: a git worktree each, verification executed by the engine itself, a
  cross-test in the final round (A's tests against B's implementation), and every branch kept
- **Copy detection**: when someone throws away their own implementation for a rival's and
  their self-tests go from passing to failing, full consensus is downgraded to consensus with
  reservations, and it is flagged **before** the conclusion
- **An evaluation layer**: `sesa eval` / `judge` / `calibrate`, which declare themselves
  unusable when a metric fails
- `resume --inject` to continue from a stopping point; exit codes 0/3/2/4 for CI to act on
- `sesa watch` follows a deliberation without waiting for it to finish — **many problems appear
  only in the middle** and leave no trace in the outcome
- A **Textual TUI** (`sesa run --tui`): watch everyone write side by side, with four
  interventions (interject / veto a premise / follow one side / wrap up early), all recorded
  as replayable events
- **Background material private to one participant** (`briefing:`, inline or `@file`) — the one
  deliberately created information asymmetry in Sesa. Its cost is built into the product: the
  material goes to disk, an event is emitted, and the top of `RESULT.md` states that "the
  parties' material is asymmetric, so a disagreement here may be an information gap rather
  than a difference in judgement"
- **The interface language is selectable** (`SESA_LANG` / `language:` in the config), English
  by default; the deliberation and the deliverables follow the language of the task, so asking
  in Chinese gets Chinese output even under an English interface

### This version was found out by itself

Before v0.1 was finalised we ran a Sesa deliberation on the topic "the four design bottom
lines in the README — does the code actually do them?" The participants read the source in
their own git worktrees, wrote tests and ran them.

The result was **10 tests, 8 of them failing at the time, all verified mechanically as real**,
including:

- **`sesa run` crashing for certain in a real terminal** — that rendering path runs only under
  a TTY, and the 264 tests of the time and every manual check ran without one, so it was never
  executed
- **rewording a residual made deadlock detection never fire** — a code comment said plainly "a
  self-report is not enough to reset the stall counter", and the function called on the very
  next line called residuals, which are self-reported text, an "objective signal"

On the same day, the external tool open-code-review independently found 6 more real defects.
**All 14 came from code written that day, and repeated self-review had seen none of them.**
See [DESIGN.md 14.20](DESIGN.md).

### Where the defects came from, kept separate

- Found by **a Sesa deliberation** (several parties arguing plus real test runs): about 50
- Found by **an external tool alone** (open-code-review): about 85
- Found by **the author sweeping backwards** (prompted by earlier rounds, looking for the same
  error elsewhere): 2 classes

These are not the same thing. A useful tool does not make the deliberation effective, or the
reverse — this project accounts for the two separately; see [DESIGN.md 14.23](DESIGN.md).

### Known boundaries

- The MCP server is not implemented yet
- CI is manual-trigger only for now (the repository is still private, and Actions consume the
  account's quota)
- Every measurement was made on a limited set of model pairs and tasks, with generally small
  samples; the claims that were overturned and withdrawn are all in section 14 of DESIGN, not
  hidden
