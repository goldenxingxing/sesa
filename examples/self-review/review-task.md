# Task: verify Sesa's design and code from scratch

Your working directory **is Sesa's complete source** (`src/sesa/`, `tests/`, `DESIGN.md`,
`README.md`). You may read any file, change any file and run any command.

Sesa calls itself a "multi-agent deliberation engine", and its four design bottom lines are in
the README:

1. No referee — when consensus is reached the consensus is the deliverable; when it is not, the
   parties' positions are set out side by side
2. Not confirmed to agree ≠ agreed — default-deny, only a parseable explicit agree resolves a
   cell; "someone objected" is accounted separately from "the engine did not measure it"
3. Stuck ≠ united — six grades of outcome, and deadlock / exhausted are never written up as
   consensus
4. A conclusion is delivered together with its premises; an open disagreement comes with a way
   out

## What you have to answer

**Do these bottom lines actually hold in the code?**

Give **specific, verifiable** findings. Every one must contain:

- **Location**: `file:line`
- **Failure scenario**: with what input, in what state, what wrong result comes out
- **How it was verified**: how you prove it is real

## Hard requirements

**No unsupported claims.** You can simply run:

```
PYTHONPATH=src <your python> -m pytest -q          # every existing test
PYTHONPATH=src <your python> -c "..."              # any experiment
```

**Note: running the tests requires `PYTHONPATH=src`.** Without it you import another sesa from
outside the working copy, nothing you changed is visible, and the tests are green forever —
this project has walked into that trap for real once.

The most convincing form of a finding is: **write a new test that fails against the current
code**. If you can get that far, write the test file straight into the working directory.

## What not to do

- Do not give general assessments like "the architecture is clear" or "it is maintainable";
  that is not a finding
- Do not treat "I think it should be changed to X" as a defect unless you can say what is wrong
  with it as it stands
- If you cannot reproduce something, say plainly that it does not hold — **reporting no
  problems is a valid answer**, inventing one is not

---

## Already fixed; do not report again

`tests/test_bottom_lines*.py` is the product of the earlier rounds of self-review and is
currently green. **Read the test names first** — each name is one defect already found and
fixed.

Re-reporting a fixed item is not a finding.

## One thing worth noting

The earlier rounds exposed a pattern: **one error scatters across several outlets**. "Calling
the unmeasured a disagreement" was committed once at each of four places — the RESULT.md prose,
the terminal progress output, the consensus blockers and the REPORT.md minutes — and fixing one
had it emerge from the next.

**Finding this kind of scattered same-source error is worth more than finding an isolated bug.**

## If you find nothing

Say so plainly. **That is a valuable answer** — it means this ground can be signed off.

Do not report marginal issues to look productive. Reporting one that does not survive checking
is worse than reporting none: it destroys the meaning of "have we reached the bottom yet".
