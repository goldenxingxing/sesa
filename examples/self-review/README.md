# Let Sesa review itself (and your project too)

> **English** · [中文](README.zh.md)

This configuration has been run on Sesa itself over several rounds, and every defect it found
is in `tests/test_bottom_lines*.py` — **the test names are the list of findings**, so counting
them tells you how many there are, and no number that could go stale is written here.

Not one of them was found by the author's own review.

It is also this project's **only test that runs a whole deliberation for real**: on every run
the adapters, git worktrees, execution evidence, consensus assessment and delivery rendering
are exercised for real. Three bugs were hit this way in one day, one of which (the TTY
rendering crash) can never be seen under pytest — because the tests run without a terminal.

## Why the topic is phrased the way it is

**Do not ask "is this design any good".** That gets you a reasonable-sounding response nobody
can check. Ask something **mechanically verifiable**:

> The things you promise in the README — does the code actually do them?

And require every finding to give `file:line` + a failure scenario + how it was verified;
**the most convincing form is a test that fails against the current code.**

## Why an external scanner is bolted on

A model reading the code alone finds little. Give it the output of an **external tool** and it
has an attack surface that did not grow out of the author's framework.

**The report goes into the task description through `--file`, where every participant sees
it.** This used to use `briefing` (material private to one participant), on the grounds of
"creating information asymmetry" — a scenario constructed for an experiment. In real use you
have a scan report in hand and **there is no reason to show it to only one of them**; if you
share it, share it with everyone.

> Note: do not use the same model as both a participant and the scanner. The external tool's
> value is precisely that it did not grow out of your framework.

## Running it

```bash
# 1. External scan (any tool that emits file:line plus an explanation will do)
ocr scan --path src/yourpkg --format json > /tmp/scan.json
python3 to_briefing.py /tmp/scan.json > /tmp/findings.md

# 2. The deliberation — the scan goes into --file with the topic, visible to everyone
cat review-task.md /tmp/findings.md > /tmp/task.md
sesa run --repo . \
  --verify "PYTHONPATH=src python -m pytest -q" \
  --file /tmp/task.md \
  "The things promised in the README — does the code actually do them?"

# 3. Collect the tests the participants wrote (a failed round has output too)
git branch --list 'sesa/*'
```

## This configuration changes with the code

**Do not hard-code derivable facts into the topic template.** The first version of this file
wrote "the existing 264 tests" and "22 items over the first two rounds", and two days later
both numbers were wrong — while the participants take a false premise for true.

So:
- the "already fixed" list became **a pointer to `tests/test_bottom_lines*.py`, for the
  participants to read the test names themselves**
- the total number of tests is not written; the participants run them
- `tests/test_self_review_example.py` checks that this document has not gone back to
  hard-coding numbers

The same applies to your project: **anything in the topic that can be read from the repository
should not be copied.**

## Three things to watch out for

**1. Whether `--verify` runs code from somewhere else.** Once the repository has been
`pip install -e`-ed, running pytest inside a worktree imports **the original repository**;
whatever the participants changed is invisible and the tests stay green — the entire evidence
layer silently disabled. So the command has to carry `PYTHONPATH=src`. (The engine detects and
warns, but do not rely on that as a backstop.)

**2. A failed round has output too.** A participant's turn may fail on a quota or a timeout,
but the files it wrote in the worktree have already been committed to the branch. This
project's first round of 8 defects was recovered from a "failed" round.

**3. Verify mechanically, one by one; do not accept the lot.** I reproduced by hand every item
reported across four rounds. External tools produce false positives (participants explicitly
rejected 2 in the third round), and participants will relay a tool's words verbatim.
**A conclusion is verified, not voted on.**
