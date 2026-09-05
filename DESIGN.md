# Sesa — a multi-agent deliberation engine

> **English** · [中文](DESIGN.zh.md)

> **Sesa** — open sesame.
> Let different models speak until their views converge; and when a door will not open, tell
> you honestly where it is stuck.

It brings several **existing agent CLIs** and **bare model APIs** to the same table, has them
argue under a chosen deliberation protocol until consensus converges — and **reports the
disagreements honestly** rather than pretending they are settled.

---

## 1. What it is

### The thing itself

A **headless deliberation engine** plus three front ends (CLI / TUI / SDK). The core has one
responsibility:

> Orchestrate N heterogeneous LLM participants in a structured discussion of one task, measure
> their disagreement, and produce — on convergence or on deadlock — a decision **with its
> sources and its minority opinions**.

### How it differs from AutoGen / CrewAI / ChatArena

| | Those frameworks | sesa |
|---|---|---|
| What is orchestrated | an agent wrapped around a bare model API | **real agent CLIs** (claude code / codex / dsh / gemini-cli), which bring their own tools and file access |
| How discussion ends | a fixed number of rounds, or an agent saying "TERMINATE" | **a computable disagreement matrix** plus stability detection |
| The final output | a paragraph of synthesised text | a decision + the list of disagreements + minority opinions + an outcome label |
| The referee | usually one of the contestants | **no referee**: when consensus is reached the consensus is the deliverable; when it is not, the positions are set out honestly side by side and a person decides |
| Code tasks | rests on a model saying "I tested it" | worktree isolation + results **the engine executed itself**, with cross-testing against "testing only yourself" |

### An unanticipated return

The constraint "the event stream is the only source of truth" was adopted for replay and
review. In use it produced two things nobody had planned for:

1. **The participants can review our code.** In one deliberation, Claude quoted our own
   persisted `consensus.update` event and pointed out that the `min_confidence` aggregate
   dropped unknown participants entirely rather than counting them at their worst — a bug we
   had not found ourselves. It was reading the product's output, not the source.
2. **We can review our own conclusions.** One `sesa eval` table exposed both "all three
   deliberations falsely reported a false consensus" and "six interrupted runs are poisoning
   the means with zeros", neither of which is visible run by run.

> The return on persisting honestly is not only "you can investigate when something goes
> wrong", it is **making the system checkable by itself and by others**. That is the real
> reason to make observability a first-class contract rather than a log.

### Non-goals

- Not a general agent framework (no tool calling, memory or RAG — those are the participants'
  own business)
- Not a model gateway (no API-key rotation, rate limiting or caching)
- Not aiming at fully autonomous unattended operation (quite the opposite: the human in the
  loop is first-class)

---

## 2. The core abstraction

```
Participant = Adapter (how to call it) × Model (which brain) × Role (what stance)
```

One Adapter can be paired with different Models, and one Model with different Roles.
"Combinations of different agents and different LLMs" is the Cartesian product of that triple.

### 2.1 The layering

```
        ┌──────────────────────────────────────────────┐
        │  Engine — drives the deliberation loop        │
        │  (no IO, no rendering)                        │
        │  an async generator that only yields Events   │
        └───────────────────┬──────────────────────────┘
                            │  one event stream
   ┌──────────┬─────────────┼──────────────┬────────────────┐
  CLI        TUI         SDK / library   MCP server     someone's product
(--json)  (watch +      (import)       (an agent calls   (consumes JSONL)
           intervene)                   the council)
```

**The key constraint: the Engine knows nothing about terminals.** The TUI is merely the first
consumer of the event stream, on exactly the same footing as someone embedding it in their own
product. That is the only correct posture for "easy to build on".

### 2.2 The four pluggable layers

| Layer | Interface | Built-in implementations |
|---|---|---|
| **Adapter** | `async stream(prompt, ctx) -> AsyncIterator[Delta]` | `openai_compat` / `anthropic` / `cli` |
| **Protocol** | `plan(state) -> list[Move]` | `ensemble` / `debate` / `council` / `adversarial` / `reflect` |
| **Consensus** | `assess(state) -> ConsensusReport` | `stance_matrix` + `rapporteur` (rotating) |
| **Workspace** | `prepare(participant) -> Path` | `local` / `ephemeral` / `git_worktree` |

---

## 3. The Adapter layer

### 3.1 What the three built-in adapters cover

- **`openai_compat`** — one adapter covers DeepSeek / Kimi / OpenRouter / Ollama / vLLM / Groq /
  Together
- **`anthropic`** — Claude's native API
- **`cli`** — **a configuration-driven general subprocess adapter**

### 3.2 The `cli` adapter: a new agent = 10 lines of YAML

This is the core bet of the project's usability. Wiring up codex / gemini-cli / aider /
cursor-agent **takes no Python at all**:

```yaml
- id: codex
  adapter: cli
  command: ["codex", "exec", "--json"]
  prompt: stdin              # stdin | argv | argv_template
  cwd: "{workspace}"         # variable injection
  parse: jsonl               # raw | jsonl | json_path
  extract: "$.message.text"  # the path to pull out when parse=json*
  timeout: 600
  env: {CODEX_QUIET: "1"}
```

Streaming: in `raw` mode each stdout chunk is emitted as a Delta; in `jsonl` mode lines are
parsed one at a time.
A CLI adapter usually cannot obtain token counts → recorded as `cost: unknown`, with the
budget falling back to the wall clock.

### 3.3 Role (injecting a stance)

Assigning different personas to different models is **the cheapest quality gain there is** —
it avoids homogeneous assent:

```yaml
role: "A pragmatic systems engineer who puts maintainability and running cost first, and is
       conservative about new technology"
```

The Role is spliced into the system prompt, orthogonal to Adapter and Model.

---

## 4. The Protocol layer

A Protocol **decides only "who speaks in which phase, and what they can see"**; everything else
is shared, which is why each protocol is about 50 lines.

```python
class Protocol(Protocol):
    def plan(self, state: DeliberationState) -> list[Phase]: ...

# Move  = (participant, prompt, visible_context, expects_stance)
# Phase = list[Move]   — phases run in sequence, moves within a phase in parallel
```

**Phases in sequence, moves within a phase in parallel** — that one shape accommodates every
protocol (see 4.2).

### 4.1 The four built-in protocols

| Protocol | Structure (how each round divides into phases) | Suits | Cost |
|---|---|---|---|
| `ensemble` | 1 phase: N independent drafts in parallel, with peer-assessed stance cards | picking the best answer quickly, with nobody to persuade | lowest (one round) |
| `debate` | 1 phase: everyone challenges and revises in parallel, with answers in the next round | technical choices that have a right and a wrong | medium |
| `council` | 1 phase: everyone in parallel and **all-see-all** (not pairwise feeding) | open questions balancing several concerns | high |
| `adversarial` | 3 phases: propose → everyone opens fire in parallel → respond / cross-check (see 4.6) | finding holes, security review | medium |

> The prototype's pairwise feeding degrades once there are ≥3 participants (A sees B but not
> C). `council` fixes that.
> In `adversarial`, **an attack that was never successfully refuted** goes into the final
> report's "open items" verbatim, with no referee scoring it.

### 4.2 Why "phases in sequence, parallel within a phase" rather than taking turns

Taking turns (one after another inside one round) has a hard problem: **position bias**.

- whoever speaks first sets the frame of the whole discussion (anchoring)
- whoever speaks last has the most information and is also the most likely to slide into assent
- the wall-clock time is everyone's added together, and the TUI's "watch several models writing
  side by side" is gone

Parallel turns preserve independence and speed. The apparent cost — "they cannot answer each
other inside one round" — does not really exist: **the answering happens in the next round**,
which is what the debate protocol looks like anyway.

What genuinely needs ordering is **phases**, not participants. Take `adversarial`:

```
Phase 1  the proposer produces a proposal      ← 1 person
Phase 2  everyone else opens fire in parallel  ← N-1 at once
Phase 3  the proposer answers point by point   ← 1 person
```

Phases run in sequence (a later phase depends on an earlier one's output), and everything
inside a phase runs in parallel.

**Two iron rules:**

1. **Round 0 is forced parallel** — independent drafts are the only source of diversity, and no
   participant may see anyone else's draft first
2. If sequential really is needed (`turn_taking: sequential`), **the speaking order
   round-robins each round**, cancelling position bias by rotation

```yaml
turn_taking: parallel    # parallel (default) | sequential
```

### 4.3 Any solo role rotates by default

**Wherever a role can only be held by one person, it rotates by default.**
This is the project's uniform tactic against systematic bias, and it shares a root with "no
referee": rather than picking "an impartial one", keep the position itself moving.

| Solo role | Default | What it guards against |
|---|---|---|
| **The rapporteur** (`rapporteur`) | `rotate` | the wording bias a fixed writer brings |
| **Speaking order** | round-robin (only under `turn_taking: sequential`) | position bias / anchoring |
| **The proposer** (`proposer`, `adversarial` only) | `rotate` | "testing one person's proposal" ≠ "picking the best proposal" |

### 4.4 Incremental revision rather than a full rewrite

The prototype had the model output a complete new version each round → tokens grew
exponentially and it drifted further with every edit.
sesa asks for a **structured revision** by default:
`{keep: [...], revise: [{claim, why, new}], drop: [...]}`, with the engine applying it and
maintaining the current version. A full rewrite is demoted to the `--full-rewrite` option.

---

### 4.5 Who the proposer is under `adversarial`

`adversarial` is the only protocol with an **asymmetric role**, and where the proposal comes
from decides its two uses:

```yaml
protocol: adversarial
proposer: rotate     # rotate | <participant_id> | input
```

**`input` — the thing under review is what the person supplied, with no agent proposer.**
The commonest use: `sesa run --file rfc.md "review this RFC"`.
Every participant is an attacker, so Phase 3 is not "the proposer answers" but **the attackers
cross-check**:

```
Phase 1   (none; the proposal is the task input)
Phase 2   everyone opens fire in parallel
Phase 3   everyone cross-checks in parallel whether each other's attacks hold
```

The cross-check cannot be skipped — without it the protocol degrades into "anyone may nitpick
freely" and piles up unverified noise. **Only an attack that was never successfully refuted
reaches the "open items".**

**`rotate` — a different proposer each round**, so over N rounds everyone gets attacked once.
This is the only unbiased choice; the cost is that fairness needs at least as many rounds as
participants.

**`<participant_id>` — a named person.**
Legitimate but asymmetric: this is red-teaming one particular proposal, not picking the best
one. It is labelled honestly at runtime.

> Defaults: with `--file` given it defaults to `input`, otherwise to `rotate`.
> The value actually in force is printed at runtime rather than taking effect quietly.

### 4.6 Layered visibility: the reasoning is not shared by default

What a participant produces has three layers, with different visibility:

| Layer | Content | Visible to the others? | Persisted? |
|---|---|---|---|
| **thinking** | the reasoning draft (extended thinking) | **no** (by default) | yes, for people to read and trace |
| **statement** | the turn and its argument | yes | yes |
| **stance** | the stance card | yes (the input to the disagreement matrix) | yes |

Three reasons not to share thinking:

1. **Premature convergence is the biggest failure mode of multi-agent debate.** The more that
   is shared, the faster they converge — and you end up paying three times over for one
   model's opinion. **Independence is the premise of collective intelligence**, and thinking
   is where independence is most fragile
2. **Half-formed thoughts contaminate.** The thinking is full of paths already rejected, false
   starts and self-doubt. B seeing A's "I was going to use X but it does not seem to work" is
   easily led astray by a half-finished idea, or opens fire on something A has already
   abandoned, wasting a round
3. **Volume and parity** (`measured`). In real deliberations the thinking is **1.4–2.1×** the
   prose, and turning sharing on raised input tokens by **44%** (n=7, the two groups' ranges do
   not overlap); the wall clock showed **no difference** (+1%; the earlier +47% was an n=1
   extrapolation and has been corrected); and **not every adapter can obtain it** (`claude -p`
   does not expose its thinking), so requiring it would break parity between adapters

> **Points 1 and 2 are still unverified.** A controlled experiment (n=7 per group, see 14.3)
> was once read as "point 1 refuted", but the three metrics behind its "no difference" verdict
> were all later overturned or downgraded, and **that refutation has been withdrawn**. The
> current state is unknown, not "ruled out".
>
> `never` remains the default, and the reasons left are **saving 44% of input tokens** and
> **parity between adapters** — neither of which has anything to do with convergence.

**But there is a switch:**

```yaml
share_thinking: never    # never (default) | on_deadlock | always
```

`on_deadlock` — **the reasoning is opened to each other only when stuck**. By the time it is a
deadlock, premature convergence is no longer the risk; what is needed then is precisely to dig
out the two sides' differing premises (the "root cause" of §7.2), and premises usually hide in
the thinking while the prose presents only conclusions.
**Stay independent while independence is what matters, and open up only when digging out root
causes is what matters.**

> **Thinking ≠ traces of work.** A CLI agent's "I read `src/db.py` and found it already uses
> JSONB" is **evidence**, not thinking, and a factual finding should be shared — text tasks ask
> for citations in the prompt, and code tasks go through the Evidence channel (see section 6).

---

## 5. The Consensus layer (the heart of the project)

### 5.1 The stance card

Each round, alongside the prose, every participant emits a structured stance card:

```json
{
  "position": "one sentence summarising my position",
  "confidence": 0.75,
  "key_claims": ["...", "..."],
  "stance_on": {
    "kimi":  {"verdict": "agree",    "reason": ""},
    "gpt5":  {"verdict": "disagree", "reason": "they assume a single-machine deployment, which contradicts the premise"}
  },
  "open_questions": ["..."],
  "changed_from_last_round": true
}
```

**Tolerant extraction**: find the last ```json fence → find the last balanced `{...}` → and if
that still fails, **ask that participant to send the stance card alone again**; failing again,
that participant's position for that round is recorded as `unknown` and listed explicitly in
the report — **no guessing, no writing on their behalf**.

When one participant is `unknown` too often, `doctor` suggests turning stance cards off for it
and using a simpler protocol.

### 5.2 Assessing convergence

**default-deny**: a cell counts as "resolved" if and only if there is a **parseable, explicit
`agree`**. `disagree`, `unknown` and **a `partial` with an empty payload** all count towards
`unresolved` — when the engine does not hold someone's judgement, it must not assume agreement
on their behalf.

```
unresolved   = #{ (i,j) : verdict ∈ {disagree, unknown} }   hard disagreement, blocks convergence
reservations = #{ (i,j) : verdict == partial (with residuals) }   soft, only downgrades
```

- **consensus**: `unresolved == 0`, `reservations == 0`, `min(confidence) >= threshold`
- **consensus_with_reservations**: no hard disagreement, but residuals on record
- **deadlock**: K consecutive rounds (2 by default) with nobody changing position and
  `unresolved` not falling
- **exhausted**: past `max_rounds`, or the budget circuit breaker tripped

> **Consensus is a report label, not a termination condition.** Termination is guaranteed by
> the round count and the budget, so tightening the assessment to default-deny cannot make "the
> debate never finish"; the worst case is a report saying honestly that they did not settle it.
> That distinction was the central output of two real deliberations — we had conflated the two,
> which is why we believed "a strict assessment would degrade the tool".

**A `partial` must carry non-empty residuals.** A "partial agreement" with an empty payload
cannot be checked: it states neither what is agreed nor what is held back, so it is treated as
no position taken.

> **A position may be taken only on someone who was seen.** When a participant is asked to fill
> in `stance_on`, the list contains only those whose turns they **have actually read**. In
> round 0 nobody has seen anybody, and demanding a position there leaves the model nothing to
> do but invent — measured, "kimi" appeared 0 times in DeepSeek's turn while its stance card
> rated kimi. If they all happen to guess `agree`, the engine **declares consensus after a
> round in which nothing was contested**, hollowing out the whole deliberation.
>
> Together with "no position taken blocks consensus", this makes **round 0 structurally unable
> to converge**.

### 5.3 The outcomes, labelled honestly

| State | Meaning | How the report presents it |
|---|---|---|
| `consensus` | they really did settle it | the decision + the agreed conclusion |
| `consensus_with_reservations` | nobody objected, but there are residuals | the conclusion + **the reservations verbatim in the deliverable** |
| `deadlock` | stuck ≠ united | **it says "no consensus was reached"** + the positions side by side + the disagreement matrix, for a person to decide |
| `exhausted` | the budget or the rounds ran out | it says "the discussion is unfinished" + the best so far + the open items |

> This is the project's honesty bottom line: **a deadlock is never dressed up as consensus.**

### 5.4 Why there is no arbiter

The prototype's flaw was "the referee is a player". The usual fix is a third, neutral model as
the judge — but that asks the user for another key, and **the judge can be wrong too**, which
amounts to overwriting the debate's result with an opinion that was never debated.

sesa's choice is **to remove the referee**:

- **when consensus is reached**, the consensus is the deliverable — no third party need
  synthesise it again
- **when it is not**, the positions and the disagreement matrix are set out honestly side by
  side, and **the user decides**

That removes the bias and the biggest first-use friction at once (no extra API key needed).

### 5.5 The rapporteur

Every stance card saying `agree` does not mean the prose really agrees — the wording and the
details may still differ. So once it converges someone has to integrate several texts into one,
and that role is the **rapporteur**.

- chosen **from the participants on rotation** (by round by default; `--rapporteur <id>` names
  one)
- **not a judge**: the job is to integrate the wording and attribute the disagreements, not to
  rule on who is right
- **they write whether or not it converged** — even after a blazing row, whatever was settled
  must be written up as a conclusion, leaving only what genuinely was not in "open
  disagreements" (see section 7). Handing two contradictory essays back to a person is handing
  the work back
- the output carries a `drafted_by` field, and the report says who wrote it

**False-consensus detection (two-way)**:

1. the rapporteur reported a conflict explicitly in `conflicts_found`
2. **the matrix claims agreement while the rapporteur listed open disagreements** — this
   direction matters more: it means the rapporteur, having read the whole thing, caught a
   substantive conflict the structured stance cards failed to reflect

Either one makes it `false_consensus`, recorded honestly in the event stream.

---

## 6. Workspace and Evidence (text tasks ↔ code tasks)

Text tasks and code tasks differ at exactly two seams; the other four layers are shared.

| | `ephemeral` (text topics) | `git_worktree` (code tasks) |
|---|---|---|
| Isolation | a temp directory, touching no repo | `git worktree add` gives each their own, with nobody trampling anybody |

| Participant cwd | the temp directory | their own worktree |
| Evidence | none | the `--verify` command is run, and the exit code / output enters as objective evidence |
| The decided artefact | a markdown conclusion | the chosen or merged branch + the reasoning |

### 6.1 The Evidence hook

```bash
sesa run --repo . --verify "pytest -q" "fix issue #123"
```

Each round every worktree runs verify once, and the results are injected into the next round's
context:

```
[EVIDENCE] claude/worktree  → pytest: 12 passed
[EVIDENCE] kimi/worktree    → pytest: 10 passed, 2 failed (test_tz_boundary)
```

An execution result is far harder than rhetoric. But —

### 6.2 Evidence can be wrong too

**"An execution result is an objective referee" is written too confidently.** Evidence has at
least six ways of being wrong, and the first of them is all but default behaviour for agents:

| How it goes wrong | Example |
|---|---|
| **Whoever writes the code also writes the tests** | a green light proves nothing: it may be `assert True`, or assertions that happen to match their own bug |
| **Fabricated citations** | "I read `src/db.py` and found it already uses JSONB" — when they did not read it, or read it wrong |
| **The tests miss the point** | they pass, and never touch the contested point at all |
| **Environment differences** | two worktrees with different dependencies or caches give different results for the same command, and neither is wrong |
| **Stale evidence** | round 1's result quoted in round 3, long after the code changed |
| **Tampering** | editing the test file to make it green |

So Sesa handles evidence under four rules.

#### Rule one: the engine never takes a participant's account of an execution result

Evidence is graded by source, and this is the structural first gate:

| Source | Meaning | Standing |
|---|---|---|
| `engine` | obtained by **the engine itself** executing in a controlled workspace | this is what counts as evidence |
| `claimed` | a participant's "I ran it, the result was …" | only **a claim awaiting verification**, on a par with any other assertion |

Everything a participant says about command output is, by default, a claim to be verified and
not a fact.

#### Rule two: citations are mechanically checkable

When a participant cites a file and a line number on a text task, the engine goes and looks:
does the file exist? Is the quoted passage really in it? The check is cheap and catches the
vast majority of hallucinated citations. A citation that fails is marked in the report and may
not serve as grounds for consensus.

#### Rule three: cross-testing — the real answer to "testing only yourself"

Since a self-test does not count, **run A's tests against B's implementation**:

```
                claude's impl   kimi's impl
claude's tests       ✅              ❌
kimi's tests         ✅              ✅
```

That table carries a great deal: kimi's implementation fails claude's tests, while claude's
implementation passes both. If someone's tests pass **only for themselves**, either the tests
encode their private assumptions or the others really are wrong — and either way, **the
disagreement has been located on one specific test**, which beats ten rounds of arguing.

> The stance matrix is what they say. **The cross-test matrix is what they did.** The two are
> symmetric.

Beyond that, the baseline tests the user supplied **may not be modified by anyone**: the engine
checks the protected paths with `git diff`, and touching them is a serious violation, recorded
honestly in the report.

#### Rule four: evidence can be rebutted

Evidence enters the debate as **a strong but rebuttable claim**, not as a judgement. A
participant may argue "this test is itself wrong, because …", and that rebuttal is equally
checkable.

So it is **not "a position contradicting an execution result is automatically void" but "it
must be answered explicitly"** — a contradiction left unanswered goes straight into the final
report's "open items".

#### The metadata on a piece of evidence

Every piece carries its source, the party it tests and the workspace's revision fingerprint,
so that a code change invalidates old evidence and it cannot be reused across rounds:

```python
EvidenceRecord(
    participant="claude",   # who produced it / who wrote the test
    against="kimi",         # the party tested; equal to participant means "testing yourself"
    source="engine",        # engine | claimed
    revision="a1b2c3d",     # the workspace revision; a code change invalidates this record
    cmd="pytest -q", exit_code=1, summary="...",
)
```

---

## 7. The shape of the output

### 7.1 Agreement and disagreement are not two different outputs

**The rapporteur always writes**; only the scope differs. Even with nothing settled at all,
whatever *was* settled should be written up as a conclusion, leaving out only the small part
that genuinely was not.

`RESULT.md`'s skeleton is **constant**:


```markdown
# <the task>

## Conclusion            ← the agreed part, ready to use (labelled with drafted_by)
## Grounds               ← the key arguments behind it, saying who agreed or changed position, and when
## Open disagreements    ← see 7.2
## Minority opinions     ← voted down but still held, kept verbatim
```

| Outcome | The shape of the document |
|---|---|
| Full agreement | "open disagreements" is empty, and this is a clean answer |
| Partial agreement | **the commonest and the most valuable**: the conclusion holds the agreed part and the open section holds the rest |
| No agreement at all | the conclusion says "no consensus" and the open section becomes the body of the document |

The reader's way of reading it never changes, and the document does not switch format according
to whether they settled it.

### 7.2 How an open disagreement is written

Simply listing each side's position is not enough — that hands the work of making sense of it
back to the reader. Every open disagreement must contain three things:

```markdown
### Disagreement 1: the assumed deployment scale
| Participant | Position | Reason |
|---|---|---|
| claude | a single Postgres is enough | lower running cost at the current volume |
| gpt5   | sharding is required | it hits a wall on the three-year growth curve |

**Root cause**: the two assume different peak QPS (claude assumes ~500, gpt5 assumes ~50000)
**What is needed to decide**: what is your actual peak QPS?
**Next step**: sesa resume <run_id> --inject "peak QPS is about 3000"
```

1. **Attributing the root cause** — the large majority of disagreements come from **differing
   premises** rather than a wrong conclusion. Lay the assumptions out and a person can usually
   decide at a glance
2. **The one piece of information needed to decide** — "just tell me X and this disagreement
   dissolves"
3. **An actionable next step** — `resume` carries on from where it stopped, with nothing re-run
   from the start

> `resume` turns a deadlock from "a failure" into an ordinary intermediate state in the loop:
> they cannot agree → they tell you what is missing → you supply it → they carry on.

### 7.3 The corresponding output for a code task

| Text task | Code task |
|---|---|
| the conclusion (markdown) | the merged branch `sesa/<run_id>/result` |
| the grounds | the list of tests everyone passes |
| open disagreements | the contested files and implementation choices + **every party's branch kept and checkout-able** |
| minority opinions | an implementation that was not adopted but kept (the branch is not deleted) |

```
sesa/<run_id>/claude    ← kept, git diff-able
sesa/<run_id>/kimi      ← kept
sesa/<run_id>/result    ← the rapporteur's merge
```

**No participant's branch is deleted.** A person can diff two implementations and decide for
themselves.

### 7.4 What lands on disk

```
.sesa/runs/<run_id>/
├── RESULT.md      # the main deliverable, ready to use
├── RESULT.json    # the same thing structured, for MCP and third-party products
├── REPORT.md      # the minutes: how the argument went, how the matrix evolved, what it cost
├── events.jsonl   # the raw event stream, replayable and evaluable
└── turns/         # everyone's raw text per round, for tracing back
```

`RESULT.md` is a rendering of `RESULT.json`, and the two always share one source.

---

## 8. The event stream (the outward contract)

The Engine's only output. The CLI, the TUI, the SDK, MCP and third-party products all consume
the same one.

```jsonc
{"t":"run.start",       "run_id":"...", "task":"...", "participants":[...], "protocol":"debate"}
{"t":"round.start",     "round":2}
{"t":"turn.start",      "round":2, "participant":"kimi"}
{"t":"turn.thinking",   "participant":"kimi", "text":"..."}   // persisted only, never into others' context
{"t":"turn.delta",      "participant":"kimi", "text":"..."}          // streamed
{"t":"turn.end",        "participant":"kimi", "tokens":{...}, "usd":0.012}
{"t":"stance.emit",     "participant":"kimi", "stance":{...}, "degraded":false}
{"t":"evidence",        "participant":"kimi", "cmd":"pytest -q", "exit":1, "summary":"..."}
{"t":"consensus.update","round":2, "unresolved":1, "matrix":[...], "state":"open"}
{"t":"false_consensus", "round":3, "detected_by":"kimi", "conflicts":[...]}   // back for another round
{"t":"human.inject",    "text":"ignore cost; look only at maintainability"}   // human in the loop
{"t":"run.resume",      "from_run":"...", "inject":"peak QPS is about 3000"}  // resuming
{"t":"budget.warn",     "spent_usd":1.6, "limit_usd":2.0}
{"t":"verdict.final",   "outcome":"consensus", "answer":"...", "drafted_by":"kimi",
                        "dissent":[...], "unresolved":[...]}
```

All of it is persisted to `.sesa/runs/<run_id>/events.jsonl`, which supports:
- `sesa replay <run_id>` — replay in the TUI
- `sesa report <run_id>` — render markdown
- evaluation later: which "agent × model × protocol" combination does better on what kind of
  task

---

## 9. The human in the loop (what the TUI is really for)

A multi-agent debate is inherently **watchable, and ought to be interruptible**. The prototype's
tmux split panes were a manual simulation of this.

The Textual TUI provides:

```
┌─ sesa ─ debate ─ round 2/4 ─ $0.41 ────────────────────────────┐
│ ┌ claude ────────┐ ┌ kimi ──────────┐ ┌ gpt5 ─────────────┐    │
│ │ I still think… ▊│ │ agreeing with… │ │ both of you miss ▊│    │
│ └────────────────┘ └────────────────┘ └───────────────────┘    │
│ ┌ disagreement matrix ───────────────────────────────────────┐ │
│ │          claude   kimi    gpt5                             │ │
│ │ claude      -     agree   ✗ premise: deployment scale      │ │
│ │ kimi      agree     -     ✗ premise: deployment scale      │ │
│ │ gpt5        ✗       ✗       -                              │ │
│ │ 1 open disagreement · state open                           │ │
│ └────────────────────────────────────────────────────────────┘ │
│ [i] interject [v] veto a premise [f] follow one [s] wrap up [q] │
└────────────────────────────────────────────────────────────────┘
```

The interventions (all recorded as events, all replayable):
- **Interject** — append a constraint into the next round's context
- **Veto a premise** — declare a claim invalid, and the participants must work around it
- **Follow one side** — "carry on along gpt5's lines"
- **Wrap up early** — have the rapporteur write it up from the current state

> `textual serve` turns the same TUI into a web interface in one command, with no second
> implementation.

---

## 10. Budget and safety

**A thinking model's hidden cost** (`measured`): reasoning tokens count as output.
Measured, **76%** of Kimi's output was reasoning (the prose only 24%), and up to 87% under some
configurations. And the deliberation **does not consume** the reasoning — by default it never
enters the other participants' context.
Cost-sensitive settings should turn it off explicitly:
`extra_body: { thinking: { type: disabled } }`.

The differences between billing models are large enough to weigh when choosing participants:

| Route | Billing | Measured output/input |
|---|---|---|
| an agent CLI (subscription, e.g. `claude -p`) | not per token | usage unavailable |
| DeepSeek API | per token | 0.76 |
| Kimi API (thinking on) | per token | **2.2** |

**The budget circuit breaker**: `max_usd` / `max_tokens` / `max_wall_seconds`; hitting any one
stops the run and produces an interim decision. API adapters read real usage; CLI adapters
usually cannot → recorded as `unknown`, with the wall clock as the backstop.

**Safety** (a code task involves switches like `--dangerously-skip-permissions` by default):
- the `ephemeral` workspace is a default that **touches no user repository**
- a code task requires an explicit `--repo`, and a clean git repository (uncommitted changes are
  refused)
- worktree isolation, so participants cannot write to each other or to the main branch
- dangerous switches like `--yolo` must be enabled explicitly per participant in the config;
  there is no global default that turns them on

---

## 11. Configuration and credentials

Participant configuration is **filled in once and reused indefinitely** — the key to the
first-use experience.

### 11.1 Two levels of configuration

```
~/.config/sesa/config.yaml   # the global participant library, shared across projects
./sesa.yaml                  # per project: which of them, which protocol, what budget
                             # (overrides the global one)
```

**How many participants is the user's choice: at least 2, with no upper bound** (only the
budget). With 2 the disagreement matrix has one cell — which is not a defect, because a person
decides anyway; majority and minority opinions only mean something with more.

### 11.2 The `sesa init` wizard

1. **Detect the agent CLIs installed on this machine** (claude / codex / dsh / gemini / aider /
   cursor-agent) and offer them as checkboxes
2. **Add API models**: pick a preset (DeepSeek / Kimi / OpenRouter / Ollama / a custom
   base_url)
3. **Write a role for each participant** (skippable; a default persona is used)
4. **Handle the credentials** (see 11.3)
5. Write the global configuration, after which any project can simply `sesa run`

### 11.3 Credentials

**By default a key is never written into a config file in plaintext.**

| Method | What the config records | Notes |
|---|---|---|
| The system keyring (default) | `api_key: keyring` | macOS Keychain / Windows Credential Manager / Secret Service |
| An environment variable | `api_key_env: KIMI_API_KEY` | suits CI |
| Plaintext (not recommended) | `api_key: sk-...` | allowed only after the wizard warns explicitly, with file permissions forced to 600 |

### 11.4 Managing participants

```bash
sesa participants list
sesa participants add           # through the wizard
sesa participants test kimi     # send one message, checking availability and latency
sesa participants remove kimi
```

### 11.5 A sample configuration

```yaml
version: 1

participants:
  - id: claude
    adapter: cli
    command: ["claude", "-p", "--effort", "high"]
    prompt: stdin
    cwd: "{workspace}"
    role: "A pragmatic systems engineer who puts maintainability and running cost first"

  - id: kimi
    adapter: openai_compat
    base_url: https://api.kimi.com/coding/v1
    model: kimi-for-coding
    api_key: keyring                  # the real key lives in the system keyring
    role: "A bold innovator, willing to challenge received assumptions"

  - id: gpt5
    adapter: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: openai/gpt-5
    api_key_env: OPENROUTER_API_KEY
    role: "A rigorous sceptic who hunts for holes in the argument"

# No arbiter. Once it converges a participant writes it up on rotation; if it does not, the
# positions are set out side by side and a person decides.
# Any solo role rotates by default (see 4.3).
rapporteur: rotate                    # rotate | <participant_id>
proposer: rotate                      # adversarial only: rotate | <id> | input
turn_taking: parallel                 # parallel (default) | sequential
share_thinking: never                 # never (default) | on_deadlock | always

protocol: debate
rounds:    {max: 4, stability_window: 2}
consensus: {confidence_threshold: 0.6}
budget:    {max_usd: 2.0}
```

---

## 12. The CLI

```bash
sesa init      # the first-run wizard: detect installed agent CLIs + configure API models + store credentials
sesa doctor    # check each one for callability and authentication
sesa participants list|add|test|remove

# A text topic
sesa run "Postgres or SQLite?" -p claude -p kimi -p gpt5
sesa run --file rfc.md "review this RFC" --protocol adversarial

# A code task
sesa run --repo . --verify "pytest -q" "fix issue #123"

sesa resume <run_id> --inject "peak QPS is about 3000"   # add information and carry on
sesa replay <run_id>         # replay in the TUI
sesa report <run_id>         # render markdown
```

Defaults: **a tty opens the TUI; a pipe emits JSONL**. `--tui` / `--json` force either.

---

## 13. Roadmap

| Version | Contents |
|---|---|
| **v0.1** | the streaming core + the event stream + three Adapters + the `init` wizard and persisted configuration + `ensemble`/`debate` + stance-matrix consensus + a rotating rapporteur + `RESULT.md`/`RESULT.json` + the `ephemeral` workspace + the CLI + JSONL persistence |
| **v0.2** | ~~the Textual TUI (watching + the four interventions)~~ shipped + `replay` + `resume --inject` |
| **v0.3** | the `git_worktree` workspace + the Evidence hook (engine execution + the cross-test matrix + citation checking + protection of the baseline tests) + `council`/`adversarial` |
| **v0.4** | the MCP server (letting an agent like Claude Code "convene a council" of its own) |
| **v0.5** | evaluation, extended: a benchmark set + controlled comparisons across configurations (`sesa eval`'s metric layer shipped in v0.1) |

---

## 14. Measured findings

Claims in this document carry one of three labels: **`measured`** (an experiment was run, or
there is production data), **`inferred`** (there is a mechanism but it was not verified), and
**`unverified`** (it holds only on plausibility).
This applies the project's own values to itself — we require the participants to separate fact
from judgement, and the documentation should not be exempt.

### 14.1 Claims measurement has overturned

**`agent CLIs comply markedly worse with structured output`** — **false**.

| Component | JSON stance card | line table | time taken |
|---|---|---|---|
| claude CLI | 3/3 | 3/3 | 41–61s vs **7–8s** |
| kimi CLI | 3/3 | 3/3 | 51–118s vs **15–16s** |

Production data agrees: the stance-card degradation rate across three real deliberations was
0.00 / 0.17 / 0.00, and that 0.17 turned out to be **a defect in our own extractor** (it
returned early when a participant quoted some other JSON and never reached the balanced-bracket
fallback), not a participant breaking the format.

> This premise had been used to justify the T2 degradation ladder, the `cli` adapter's
> artefact-file channel and several other designs. Those designs are still worth keeping, but
> **the reason has to change**: not "JSON will fail" but "the degraded path is cheap, 4–7×
> faster, and when it does fail it costs one cell".

### 14.2 A withdrawn claim: `several rounds of debate really do change conclusions`

This was the project's existence premise and was once labelled "supported by measurement"
(position drift 0.62–0.92).
**That conclusion has been withdrawn** — not refuted, but **the metric supporting it failed
calibration**.

**Calibrating the metric** (hand-constructed known cases, at no cost):

| Case | Surface-form drift |
|---|---|
| identical | 0.000 |
| **opposite conclusions** | **0.394** |
| **same conclusion, reworded** | **0.733** |
| same conclusion + a long added condition | 0.881 |
| entirely unrelated topics | 1.000 |

**Across the interval that matters it is inverted**: "said differently" scores higher than
"opposite conclusion". And real deliberations land at 0.62–0.92, right in the "same conclusion,
reworded" band — this metric was very likely measuring **change of wording** all along, not
change of position.

**Switching to a categorical metric that does not depend on wording** (the cross-round change
in agree/partial/disagree, excluding transitions that are not changes of mind, such as "round 0
unknown → round 1 first position"):

```
cells that genuinely changed judgement after the first position, across 33 deliberations:  3
times participants self-reported "I changed my position":                                 33
```

**But the categorical metric is not conclusive either**: measured, the vast majority of cells
sit at `partial` for a long time, so the category space effectively has one value and it cannot
detect movement *inside* `partial` — and the movement very likely happens in the **residuals**
("which points I have not yet accepted") appearing and disappearing.

> **The honest current state: we have no effective way to measure "does debate change
> positions".** The surface-form metric measures wording, and the categorical metric lacks
> resolution. The existence claim's status is **"could not be measured effectively"**, neither
> established nor refuted.
>
> What has been done about it: the residuals **had not been entering the event stream at all**
> (`stance.emit` carried only the verdict); they now do, and a `residual_churn` metric was
> added. To be reassessed once new data accumulates.

**The lesson matters more than the conclusion**: we ran twenty-odd experiments on an
uncalibrated metric and wrote it into the README, and only went to calibrate it when the number
struck us as barely moving under any intervention.
**Calibrate the metric before drawing conclusions from it.**

**`the two-way false-consensus check does real work`** — **true**; all three cases were real
conflicts.
But the test was once too broad (it applied to `consensus_with_reservations` too), producing
3/3 false alarms — that grade is by definition "there are reservations", and the rapporteur
writing them up as open disagreements is expected behaviour. It is now narrowed to full
consensus only.

### 14.3 A withdrawn refutation: sharing the reasoning and convergence

**`sharing the reasoning causes premature convergence`** — **false** (a controlled experiment,
7 valid runs per group).

The same topic, the same two participants (Kimi + DeepSeek), the same three rounds, with only
`share_thinking` changed:

| Metric | `never` | `always` | Verdict |
|---|---|---|---|
| position drift | 0.623 ±0.090 | 0.661 ±0.048 | +6%, inside a standard deviation, no difference |
| divergence between participants | 0.685 ±0.076 | 0.733 ±0.046 | +7%, inside a standard deviation, no difference |
| admitted changing position | 0.86 ±0.69 | 0.86 ±0.69 | **identical** |
| rounds | 3.00 | 3.00 | both ran to the limit |
| wall clock | 282s ±40 | 283s ±66 | +1%, no difference |
| **input tokens** | **8477 ±305** | **12205 ±1248** | **+44%, the two ranges do not overlap at all** |

If a convergence effect existed, the `always` group's drift and divergence should be markedly
lower — they are not. More directly, "admitted changing position" is **identical** in both
groups.

> **How the conclusion changed as the sample grew** is itself worth recording:
>
> | Metric | n=1 | n=4 | n=7 |
> |---|---|---|---|
> | changed position | +100% | +100% | **0%** |
> | wall clock | +47% | −3% | **+1%** |
> | input tokens | +54% | +42% | **+44%** |
>
> The first two look like strong signals at n=1 and go to zero as the sample grows; only the
> token increase, which has a mechanism behind it, holds steady throughout.

> **⚠️ This section's refutation has been withdrawn (2026-08-30).**
>
> The three metrics behind the "no difference" verdict above were all later overturned or
> downgraded:
>
> | Metric | Its later status |
> |---|---|
> | position drift | ❌ failed calibration — it measures **wording**, scoring "same conclusion, reworded" above "opposite conclusions" |
> | divergence between participants | ❌ retired, same root cause |
> | admitted changing position | ⚠️ a self-report, measured to over-report by about 11× |
>
> Two and a half of the three pillars are gone, and **"sharing the reasoning does not accelerate
> convergence" no longer rests on anything**. The current state is not "refuted" but
> **unknown**.
>
> What still stands is two objective quantities: no difference in rounds or wall clock, and
> input tokens +44% (the ranges do not overlap, and there is a mechanism). Neither answers the
> convergence question.
>
> More important is **the withdrawal itself**: when a pillar of a conclusion is knocked out, the
> conclusion does not fall with it automatically — not unless someone goes back to look. This
> section stood as "refuted" for a long time after two of its pillars had been removed.
> **An evidence ledger has to support a reverse index: when a metric fails, you must be able to
> ask immediately "which conclusions were standing on it".**
>
> This reverse index is now a test (`tests/test_evidence_ledger.py`): wherever the
> documentation mentions an invalidated metric, **the invalidation note must appear in the same
> paragraph**, or CI goes red. Its first run caught three that had slipped through — 14.4's
> primary metric, a statement in §4.6, and the sentence in the literature comparison that read
> drift as a positive signal.
> **A principle written down does not enforce itself; it has to be made executable.**

**So `never` stays the default, but only two reasons are left, and neither has anything to do
with convergence:**

1. **saving 44% of input tokens** (measured; the ranges do not overlap and there is a
   mechanism)
2. **parity between adapters**: `claude -p` does not expose its thinking, and forcing sharing
   would make different adapters incomparable

> The experiment's limits: 2 participants, one topic, two particular models, 7 runs per group.
> That is not enough to support "sharing the reasoning is harmless in any setting", only enough
> to refute "sharing markedly accelerates convergence in this setting".

**Two earlier claims that need correcting** (both extrapolated from n=1):

| The earlier claim | Its basis | Measured at n=4 |
|---|---|---|
| the thinking is 41× the volume of the prose | one small numeric-comparison task | **1.4–2.1×** under a real deliberation load |
| sharing the reasoning costs +47% wall clock | a single run | **+1%** (n=7), pure noise |

> Two extrapolations from a single sample in one round — exactly the error this project keeps
> guarding against. **n=1 can raise a hypothesis, never support a claim.**

### 14.4 A downgraded claim: role and homogenisation

**`a stance (role) avoids homogeneous assent`** — **the main prediction did not hold**
(heterogeneous models, n=8 per group: Kimi + DeepSeek, one group with opposing roles, one with
none).

| Metric | with-role | no-role | Verdict |
|---|---|---|---|
| **divergence between participants** (primary) | 0.857 ±0.049 | 0.839 ±0.073 | +2%, the ranges overlap heavily → **no effect** |
| first-round divergence | 0.850 ±0.091 | 0.813 ±0.097 | +5% (it was +10% at n=4, decaying with the sample) → noise |
| position drift | 0.774 ±0.069 | 0.779 ±0.085 | −1% |
| input tokens | 9028 ±358 | 9102 ±438 | −1%, cost-neutral |
| **times position changed** | **0.62 ±0.52** | **1.38 ±0.74** | **−55%** |
| **false consensus occurred** | **3 / 8** | **0 / 8** | |

> **⚠️ The primary metric has failed (noted 2026-08-30).**
>
> The table's primary metric, "divergence between participants", and "position drift" were both
> later overturned — they measure **wording** rather than position (calibration shows "same
> conclusion, reworded" scoring above "opposite conclusions").
> So **"role did not raise divergence" no longer rests on anything**, and this section's main
> prediction should be read as **not measured** rather than "disproved".
>
> The reverse signal in the second half ("times position changed −55%") **must be discounted
> too**: it is a self-report, measured to over-report by about 11×. The separation of the
> distributions (with-role changed at most once, no-role changed twice on four occasions) is
> still a lead worth chasing, but it is not a conclusion.
>
> What stands is two objective quantities independent of wording: **input tokens are
> cost-neutral**, and **false consensus 3/8 vs 0/8**. The latter is judged by the engine's own
> two-way check and depends on no semantic metric — **it is this section's only surviving
> finding, and it happens not to be what was being measured**.

Role did not raise the divergence between participants — the one benefit claimed for it when it
was introduced.

**But the last two rows are a signal in the other direction.** Unlike the first-round divergence
lead, which decayed with the sample, "times position changed" went from −50% at n=4 to −55% at
n=8, **firming up**, with a real separation in the distributions: `with-role` changed at most
once across eight runs, while `no-role` changed twice on four of them.

The mechanism makes sense: telling a participant "you believe the fewer moving parts the
better" **is itself an instruction to hold a position**. For an engine whose value proposition
is "let the debate change the conclusion", that direction is wrong.

> **No default is being changed on this.** This experiment measured "does role add anything once
> the models already differ", while what role is really meant to rescue is **one model playing
> different characters** — where model differences no longer supply diversity and role is the
> only means of differentiation. That comparison is still to be run.

### 14.5 A recurring methodological error

So far I have built three metrics, and **not one of them measures what its name says**; a
fourth followed, of a slightly different shape and the same origin (14.17):

| Metric | What I thought it measured | What it actually measured |
|---|---|---|
| `position_drift` | change of position | **change of wording** ("same conclusion, reworded" scores above "opposite conclusions") |
| `residual_churn` | progress on the reservations | **1 bit** (whether the sets are equal; a paraphrase scores full marks) |
| `laundering_index` | relisting in new wording | **turnover**, unable to separate "reworded" from "moved on to a new question"; retired, see 14.8 |
| (weak models' code scores) | the models' implementation judgement | **the result of guessing with no specification** — see 14.17 |
| (the debate dynamics of four self-review rounds) | how debate works | **what happened inside a configuration I constructed** — see 14.22 |

The shared shape of the error: **build a cheap counting proxy, name it after the concept you
wish you were measuring, and then treat the number as though that concept had been verified.**
Once the name is attached, every later reading assumes it holds — and calibration is something
one remembers to do much later.

The last one's inversion is worth recording: turnover was 0.89–1.0 across six deliberations, and
on that basis I added a warning to `sesa eval` that "the reservations are being relisted in new
wording".
**Reading the source text item by item showed exactly the opposite** — round 2's reservations
had taken up round 1's answers and moved on to new points. Same number, opposite meaning.

> **Counting cannot separate "laundering" from "going deeper".** Separating them requires a
> semantic comparison (embeddings or a judge), and that cost cannot be avoided. Before paying
> it, only one thing can be said reliably: **the total number of open reservations does not fall
> with the rounds.**

### 14.6 Semantic comparison: the long-open question finally has an answer

Counting cannot answer "is the debate laundering the residuals or advancing them", so local
embeddings were brought in for a semantic comparison.
**This time it was calibrated before use** — three candidate models over the same known cases:

| Model | min for a rewording | max for a new question | gap |
|---|---|---|---|
| paraphrase-multilingual-MiniLM-L12-v2 | 0.620 | 0.700 | **−0.081** ❌ |
| BAAI/bge-small-zh-v1.5 | 0.756 | 0.849 | **−0.093** ❌ |
| **BAAI/bge-base-zh-v1.5** | 0.717 | 0.602 | **+0.115** ✅ |

The first two are **inverted** across the interval that matters: they score "same topic,
different claim" above "the same thing reworded" — they capture the topic, not the claim. The
smaller the model the worse it gets (bge-small gave 0.849).
**Installed and used out of old habit, either would have produced another metric that looked
reasonable and ran backwards.**

Applied to real data once it passed calibration:

| Run | turnover | semantic rewording rate | Meaning |
|---|---|---|---|
| claude ×2, first run | 0.89 | **1.00** | all of it reworded |
| claude ×2, second run | 1.00 | **0.40** | half and half |
| DeepSeek ×2 (feedback on) ×2 | 0.92 / 1.00 | **0.00** | all new questions |
| DeepSeek ×2 (feedback off) ×2 | 0.91 / 0.91 | **0.00** | all new questions |

**Turnover sits at 0.89–1.00 throughout, nearly identical, while the semantic rewording rate
spans the whole 0.00–1.00 range.** Turnover has no resolution over the real difference at all.

> **Correcting one of my own over-generalisations while we are here.** Earlier, from reading the
> residual text by hand, I asserted "this is moving on to new questions, not laundering". The
> semantic comparison shows: what I happened to read was the DeepSeek run (rewording rate 0.00,
> conclusion correct), while the two claude runs were 1.00 and 0.40. **The conclusion was right
> and the generalisation was wrong.** Reading one run by hand cannot support a cross-run
> conclusion — the same shape as the n=1 lesson.

### 14.7 Examining the evaluation itself

A metric passing calibration does not make a conclusion hold. A validity review of the semantic
comparison **overturned the strong version of 14.6's conclusion**:

**Threat one: the threshold lands in the middle of the real data.** The similarity of 51 newly
added residuals is distributed over 0.441–0.813 with a median of 0.634; the threshold of 0.66
sits at the **65th percentile** — the least stable place a classification boundary can be.

**Threat two: the conclusion is extremely sensitive to the threshold.**

| Threshold | overall rewording rate | claude runs | DeepSeek runs |
|---|---|---|---|
| 0.50 | 0.94 | 1.00 | 0.88 |
| 0.60 | 0.57 | 0.93 | 0.17 |
| 0.66 | 0.35 | 0.67 | 0.00 |
| 0.80 | 0.02 | 0.04 | 0.00 |

**14.6's report of "claude all reworded (1.00), DeepSeek all new questions (0.00)" is an
artefact of the threshold cutting between the two distributions**, not a dichotomy in the
phenomenon. Move the threshold by 0.05 and the conclusion flips.

**Threat three: the calibration set is small and written by me.** Of the original 5 cases,
"identical" is a free point, and only 1 actually set the "minimum score for a rewording". Three
models were selected over the same set of 5, which is close to picking a winner out of noise.
Two samples taken from real residual text have since been added (0.821 / 0.395, judged
correctly, the gap unchanged).

**Threat four: the residuals are self-reported.** `residuals_discharged` treats "no longer
listed" as "no longer held", when it may only mean "could not be bothered to repeat it" — the
same unreliable source as "self-reported change of position over-reports 11×". A participant
proposed a fix (an explicit `withdrawn` marker); it is not implemented.

**Threat five: six deliberations, two model configurations, one genre of topic** (always
metrics or mechanism design).

#### What survives the review

| Conclusion | Status |
|---|---|
| the claude runs' new residuals resemble the previous round more (0.741 / 0.652)<br>the DeepSeek runs' resemble it less (0.606 / 0.585 / 0.541 / 0.524) | ✅ **stable**; the ordering holds at every threshold from 0.50 to 0.80 |
| "one group all reworded, the other all new questions" | ❌ **withdrawn**, a threshold artefact |

**So the preferred metric became the continuous `residual_similarity` (the median
similarity)** — which needs no threshold. The binary `restatement_rate` is kept but labelled
threshold-sensitive, with `restatement_sensitivity` provided so anyone can see for themselves
whether a conclusion is stable.

> This section is itself an example: **a metric passing calibration only means it can separate
> the cases in the calibration set; it says nothing about resolution over the distribution of
> real data.** A distribution check and a sensitivity analysis sit between the two.

### 14.8 How the residual score is computed, and what confounds it

**Being able to explain where a number came from is the precondition for using it as
evidence.** Laying the chain out:

```
① the model emits text
② parse stance_on[target].residuals            ← item granularity is the model's own choice
③ write stance.emit (the raw output is archived to turns/ at the same time)
④ evaluate reads it as {source → target: [items]}
⑤ residual_flow: exact string comparison gives no-longer-listed / added / balance
⑥ each added residual is cosine-compared to the previous round's residuals of the same
   direction, taking the max                    ← more candidates raise the max
⑦ the median of all those maxima = residual_similarity
```

Steps ② and ⑥ each carry a confound, **both pointing the same way**, with serious measured
consequences:

| Confound | Mechanism | Measured |
|---|---|---|
| item granularity | the model chooses how many items and how long each is; longer text shares more words and raises the cosine | correlates with similarity at **r=+0.858** |
| number of candidates | more candidates make it easier for `max()` to hit a high score | correlates at **r=+0.830**; on one residual pair it lifted the value from 0.605 to 0.763 |

The real data is entirely explained by it:

| Run | items per round | mean item length | similarity |
|---|---|---|---|
| claude ×2 | 13.5 / 15.0 | 148 / 154 | 0.741 / 0.652 |
| DeepSeek ×2 | 5.5–6.5 | 54–65 | 0.524–0.606 |

> **So even 14.7's "surviving" conclusion is withdrawn.** "The claude runs look more like
> relisting than the DeepSeek runs" is not a difference in debating behaviour, it is a
> **difference in writing style**: claude writes 150 characters an item and DeepSeek 57.
> With that, **every cross-configuration conclusion drawn from the semantic comparison is
> withdrawn**.

#### Step ⑤: exact string comparison degenerates three metrics into constants

Items are compared by **exact string equality**. And an LLM almost never repeats itself
verbatim — **measured, 0 of 51 residuals were identical to the previous round's**.

The consequence is that three metrics were not measuring anything:

| Metric | What it actually means |
|---|---|
| `residuals_dropped` (no longer listed) | always equals **the previous round's entire count** |
| added | always equals **this round's entire count** |
| `residual_turnover` | reflects only the change in count; 0.89–1.00 across all six runs |

**Turnover never measured anything about content at all**, and it had been used to
raise a warning in `sesa eval`, was written into this document, and served as the basis for
analysis. All three are removed; `residual_flow` now reports counts only, and `residual_trend`
(the net change in count) was added.

> The character layer only picks out candidates; whether the content advanced was always a
> question for the semantic layer. Taking the character layer's output for a conclusion about
> content was the most insidious error along this road — because the number kept moving and
> looked as though it were measuring something.

#### What is left that is usable

Granularity is far more stable **within** a run round to round (the median coefficient of
variation of length is 0.104, against about 0.48 across runs), so **a trend across rounds within
one run is still measurable** — but that run's own coefficient of variation has to be checked.

So `residual_similarity` is constrained to:

- be **presented side by side** with `residual_granularity` (count × mean length × coefficient
  of variation)
- pass `comparable_with()` (granularity within 25%) before any cross-run comparison
- have `sesa eval` **actively refuse a cross-run conclusion** when the granularities do not
  match, and star a warning when the within-run coefficient of variation exceeds 0.25

> Make the confound something the tool forces into the open, rather than leaving it in a
> comment — because the person writing the comment and the person reading the number are usually
> the same, and they will forget.

### 14.9 A reasoning error: "no referee" should not have been used to avoid an evaluation judge

To avoid "introducing a referee", this project built **6 counting and embedding proxy metrics**,
all of which failed (§14.5, §14.8). But that principle was applied in the wrong place:

| | A referee inside the deliberation | A judge in the evaluation |
|---|---|---|
| What it reads | the discussion in progress | a transcript of something **already over** |
| Does it affect the conclusion | **yes** — it decides who is right | **no** — the deliberation is long finished |
| This project's position | do not have one | there should be one |

**Applying the principle for the former to the latter was a reasoning error.** And the
counterexample was in plain sight all along: in one deliberation Claude read our persisted event
stream and pointed out the flaw in `min_confidence` — precisely a semantic understanding of the
deliberation's content, and an effective one.

#### The judge's own failure modes, each guarded

| Failure | Guard | Implementation |
|---|---|---|
| **Hallucinated citations** | every verdict must carry a verbatim quotation, **mechanically checked** against the transcript; failing it voids the verdict | `verify_quote`, ignoring whitespace and punctuation, not accepting anything under 8 characters |
| **Self-preference** | the judge may not be a participant in that run | `assert_not_participant` |
| **Instability** | `--repeat` judges several times and reports the agreement rate, warning below 0.7 | `agreement` |
| **Over-attribution** | the prompt offers "elaborated only" explicitly and declares it the default | a three-way verdict |

#### The first real judgement

The same deliberation (DeepSeek ×2, judged by Claude):

```
ds-conservative  elaborated only  across three rounds the conclusion stayed "reject A/C, D as
                                  the backbone, B as support"; neither the ranking nor the
                                  trade-off moved
ds-radical       changed          in round 0 it made B the backbone with D merely supporting;
                                  by r01 it had switched to "adopt D as primary, and reject B"
```

**One really did change and the other was only elaborating** — a distinction that six metrics of
character comparison, embeddings and turnover could not make, and the judge produced in one go,
with the quotations verified as genuine.

> The lesson is not "we should have used a judge sooner" but: **when a principle sends you the
> long way round, first confirm that it is aimed at the thing in front of you.**

### 14.10 The judge layer's first cross-judging, and a flaw in the experimental design

Six deliberations were cross-judged (claude's runs judged by DeepSeek, DeepSeek's by claude), 14
verdicts:

| Verdict | Passed | Voided | Pass rate |
|---|---|---|---|
| elaborated only | 3 | 1 | 75% |
| changed | 6 | 4 | 60% |

It looks as though "changed" is more easily voided — but split by judge, the type of verdict is
not the main cause:

| Judge | Pass rate | Verdicts of "changed" |
|---|---|---|
| claude | **88%** | 6/7 passed |
| ds | **33%** | **0/3 passed** |

**The difference in the judges' credibility (88% vs 33%) is far larger than the difference
between verdict types.** Without the mechanical gate of quotation checking, the two judges'
output would look equally credible.

#### The design flaw: judge and judged run are perfectly confounded

claude judged only DeepSeek's runs and ds judged only claude's. So "claude is the better judge"
cannot be told from "DeepSeek's runs are easier to judge".

More importantly, **the distribution of verdicts is contaminated too**: all 6 "changed" verdicts
come from claude judging ds's runs, and most of the 3 "elaborated only" from ds judging claude's.
So it **cannot** be said on this basis that "several rounds of debate produce substantive change
in 2/3 of cases" — that number is an artefact of the confound.

> This is **the same class of error as the six metrics, at another level**: there the metric did
> not measure what its name said, here the experimental design made two variables inseparable.
> What they share is that **the numbers look perfectly normal.**

The fix is to bring in a neutral third-party judge for every run, holding the judge variable
constant.

### 14.11 Against the literature: three findings that contradict what this project was doing

After surveying the 2024–2026 work on multi-agent debate, three findings point **directly at this
project's current methods**.

#### Contradiction one: repeating one judge is barely evidence of reliability

[When the Judge Changes, So Does the Measurement (2026-07)](https://arxiv.org/html/2607.08535v1)
measures **error correlation in a homogeneous jury at ρ = 0.944–0.972**, so "jury size matters
far less than error dependence" — adding more of the same judge buys almost nothing.

This project had used `--repeat 3` to get "100% agreement" and treated it as evidence the judge
was reliable. **That only measured certainty**: being wrong together also means "agreeing"
together.
Changed: `--repeat` is now explicitly labelled a check on certainty, and `cross_agreement`
(cross-model judging) was added as the evidence of reliability.
The subsequent cross-model judging came out **5/5 in full agreement**, and that is valid
evidence.

#### Contradiction two: no "no-speaker" control group — the most fundamental gap

Two independent pieces of work reach the same conclusion:

- [Not All Flips Are Conformity (2026-06)](https://arxiv.org/abs/2606.00820): under the main
  MMLU-Pro setting, **37% of observations change on self-reflection alone**.
- [Most LLM Conformity Needs No Speaker (2026-07)](https://arxiv.org/pdf/2607.05545): once a
  speaker-free floor control is added, most of the measured "conformity" is still there —
  existing benchmarks **systematically over-attribute the change to social influence**.

**So the sentence "the debate changed X% of the participants' positions" does not hold without a
control group.** The substantive changes this project measured with `sesa judge` could not, until
now, rule out "they would have changed anyway".

The `reflect` protocol was added as that control baseline: the same participants, the same number
of rounds, but each sees only their own last round and nobody sees anybody.
**Only change beyond that baseline can be attributed to the debate.**

> This has the same shape as the six metrics' error, and it is more fundamental: those measured
> something other than what their names said; this one measured the right thing and attributed it
> wrongly.

#### Contradiction three: more change does not mean the debate was worth it — it may be the reverse

From the same 2026-06 work: of the flips caused by strict conformity, **57–77% go from right to
wrong**; and **empty-sounding reasoning induces 20–39% wrong adoptions even in "resistant
agents"** — sounding like an argument is itself persuasive. The paper states plainly: **without
ground truth, a helpful influence and a harmful one cannot be told apart.**

This project had implicitly treated "large position drift" as a positive signal. **Two errors at
once**: that metric was later overturned by calibration (it measures wording, not position, and
has been retired), and even if it were valid, "changed a lot" is not "changed for the better".
`sesa judge`'s output now states permanently: **it answers only "did it change", never "did it
change for the better".**

#### Where the literature supports this project

| What this project does | Support in the literature |
|---|---|
| consensus cannot be a termination condition | [Wald-SPRT (2026-05)](https://arxiv.org/html/2605.19193v1): consensus-based early stopping is "fundamentally unsafe"; measured, 87.7–97.8% of samples terminate before the final round while their accuracy swings between 67.9% and 94.9% — consensus **offers no coverage guarantee** |
| check quotations mechanically rather than trusting the judge's own account | LLM self-checking of citations is about 38% accurate, below chance ([2606.21155](https://arxiv.org/html/2606.21155v2)) |
| surface-form and embedding proxies cannot measure semantic change of position | work on position dynamics explicitly avoids lexical similarity ([DEBATE 2025-10](https://arxiv.org/abs/2510.25110)) |
| the judge may not be a participant | self-preference is a recognised systematic error in judges |
| positioning it apart from task-dispatch orchestrators | [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator), Conductor and the rest are **all supervisor→worker task dispatch, and not one does debate, consensus or disagreement reporting** |

#### Two of our own observations with no external corroboration

1. **The judge's void rate is higher on "changed" verdicts than on "elaborated only"** — no study
   aimed at that pair of labels was found. What is known is only that judges generally carry a
   conservative bias.
2. **A complete record of measuring semantic change of position with surface-form and embedding
   proxies and coming off the rails systematically** — the published literature has no equivalent.
   The six failures in §14.5 and §14.8 may be original negative experience.

> Neither has external corroboration, so **each needs proving on its own** and must not be quoted
> as an established conclusion.

### 14.12 debate vs reflect: the certain conclusion is on the cost side

The same topic, the same two participants, the same roles, the same 3 rounds, with **`protocol`
the only variable** (n=3 per group, DeepSeek ×2):

| Metric | debate | reflect | Difference |
|---|---|---|---|
| characters per turn | 1841 | 2029 | −9% (within noise) |
| **input tokens** | **9994** | **8311** | **+20%** |
| **output tokens** | **9039** | **7004** | **+29%** |
| wall clock | 68s | 60s | +13% |
| outcome | 2× consensus with reservations, 1× false consensus | 3× exhausted (inevitable without stance cards) |

**A debate costs about 20% more input and 29% more output tokens than "each thinks it over
again"**, while turn length is nearly identical — the extra cost comes from feeding others' turns
into the context, not from writing more.

None of this depends on a judge; it is mechanically confirmable. **The only question left is what
that extra buys.** Answering it needs a judge to assess both groups' rates of change of position,
and **only the part beyond the reflect baseline can be attributed to the debate** — the
literature shows about 37% change from reflection alone.

> Note that the `reflect` group is always `exhausted`: with no speakers there is nobody to take a
> position on, the stance cards are always empty, and the consensus assessment structurally cannot
> converge. That is the control group behaving normally, not failing — its value is in providing
> the "how much would change without a debate" baseline, not in reaching consensus itself.

### 14.13 On the judge side: the first existence evidence with a control

One judge (Claude, not a participant in either group) assessed both:

| Group | Verdicts | Quotations verified | Changed | Elaborated only | **Rate of change** |
|---|---|---|---|---|---|
| debate | 6 | 6/6 | **2** | 4 | **33%** |
| reflect | 6 | 5/6 | **0** | 5 | **0%** |

**The debate group produced substantive changes of position and the self-review group produced
none.**
This is the first time this project obtained **controlled** evidence for "several rounds of debate
change positions" — every earlier claim was unable to rule out "they would have changed anyway".

One detail worth noting: **in both groups the one that changed was `ds-radical`, and
`ds-conservative` was "elaborated only" all six times.**
The conservative persona never backs down; the radical one changes only **when it can see the
other side**.
That agrees with §14.4's observation — role may make a participant less willing to concede — but
there it was a weak signal at n=8 across models, and here it is a direct observation under a
same-model control.

#### The limits of this result, which have to be written down with it

1. **n=3 per group, 6 verdicts.** The gap between 33% and 0% looks clean, but in absolute terms it
   is 2 against 0. A few more runs could easily change it.
2. **One topic, one model pair (DeepSeek ×2), one judge.** The self-reflection baseline in the
   literature §14.11 cites is about 37% and we measured 0% — a gap that large more likely says
   **our topic or participant setup is unusual** than that the literature is wrong.
3. **"Changed" is not "improved".** See §14.11's third contradiction: without ground truth, a
   helpful influence and a harmful one cannot be told apart. This table **only shows that the
   debate produced change that self-review did not; it does not show that the change was an
   improvement**.
4. One reflect verdict was voided because its quotation could not be verified, shrinking the
   sample further.

> The conclusion should be stated as: **in this setting, the debate produced change of position
> beyond the self-review baseline.** It must not be stated as "several rounds of debate work" —
> that would need more topics, more model pairs, and a means of judging whether the change was for
> the better or the worse.

### 14.14 A code task: the first control with ground truth, and it is a null result

Every earlier conclusion in this project stalled on the same sentence — **"changed ≠ improved,
and without ground truth the two cannot be told apart"**. A code task brings its own ground
truth, so debate vs reflect was run again.

**The design**: a `parse_duration` parsing task whose SPEC lists 6 error cases that must raise
`ValueError`. The participants receive the spec and an empty implementation and write both the
implementation and the tests themselves; **scoring uses a held-out test suite they never see**
(11 tests, one per SPEC item). The only variable between groups is `protocol`, and both
participants are the `claude` CLI ×2 (same model, opposing roles).

| | debate | reflect |
|---|---|---|
| **held-out tests** | alice 11/11 · bob 11/11 | alice 11/11 · bob 11/11 |
| characters written | 11264 | 9610 (+17%) |
| time taken | 679s | 592s (+15%) |
| cross-test | failing both ways | failing one way |

**On this task the debate brought no gain in correctness at all, and cost 17% more characters
and 15% more time.**

#### This is a problem with the task, not a conclusion

The task is too easy — **a single agent scores full marks in one round, and under a ceiling
effect no method can measure a difference**. This null result cannot support "debate is useless";
it supports one more useful product recommendation:

> **A task everyone can score full marks on is not worth convening a council for.**

That is now built into the tool: when `sesa eval` detects that everyone's **self-tests** are
green, it suggests considering `ensemble` or setting a harder task. Note it looks only at
self-tests — **a cross-test failure is precisely a valuable signal** (it means the parties'
implementations or tests really do disagree), and counting it towards "too easy" would be wrong.

#### The tension with the previous section is exactly what the literature predicts

§14.13 measured "debate: substantive change 2/6, reflect 0/6" on a text topic, which looks like
debate working. Only with ground truth does this become visible: **the debate really did change
positions, and correctness after the change is the same.**

That is the empirical form of the literature's sentence — **without ground truth, a helpful
influence and a harmful one cannot be told apart** (§14.11, third contradiction). With ground
truth we know: this change was neither an improvement nor a regression.

#### One piece of corroborating evidence

Both agents dealt on their own initiative with a trap the SPEC **never mentioned**: `\d` in
Python is Unicode-aware and matches the full-width `１`, which `int()` accepts, so `１h` silently
parses as 3600. Both switched to `[0-9]` to pin the grammar to ASCII — a correctness judgement
beyond the spec, and one the held-out tests do not examine. **The task's real difficulty may be
higher than the held-out tests measure; both simply cleared it easily.**

### 14.17 An experiment voided: missing context masquerading as the model's judgement

The first run of the weak-model comparison (DeepSeek ×2 on the semver task) came out at debate
alice 28/34, bob 21/34, which looked at last like breaking the ceiling effect.
**Both numbers are void.**

DeepSeek goes through the API and cannot write files — for which the engine had grown "extract
code blocks from the text and write them to disk".
But **I had missed the other half, "cannot read files either"**: what the participants received
was only `task.md` (one sentence saying "implement what SPEC.md in the repository says"), and the
SPEC itself never entered the context.

A participant said so outright, and I did not read it at the time:

> "I notice a key problem: **neither bob nor I have seen the actual contents of SPEC.md**."
> — alice, round 1

On that basis bob wrote `# NOTE: This parser intentionally does NOT support ^, ~, x, or hyphen
ranges.` I briefly read that as "the model knew the spec and refused to implement it" and was
about to report it as evidence that "the debate failed to correct an open violation".
In fact it **did not know what the spec required**.

This is the fourth instance of 14.5's error in a different shape and with the same origin: the
first three were **an empty metric under a satisfying name**, and this one is **a missing input
under the name of the model's judgement**. What they share is still that I read it by its name
first, without asking "what is the input to this number".

The fix: for a participant that cannot write files, the contents of the working directory are
injected with the prompt (`patch.render_workspace`, with a regression test).
After the fix, running the same task again, both parties began discussing the spec's fine print
clause by clause.

> This is also **one more reason to check mechanically what a participant said**. alice's
> sentence was right there in the transcript, in the first paragraph of round 1, and I read that
> transcript without seeing it — because I was looking for "whose implementation is better", not
> for "was the input right".

### 14.18 The debate's first visible mechanism: copying a rival's code wholesale

After fixing 14.17's two defects, it was run again: DeepSeek ×2 × semver × **24 runs** (three
batches).

#### The scores: the debate did not make the code more correct

| | scorable cells | improved | worsened | net |
|---|---|---|---|---|
| debate | 16 | 4 | 4 | **−3** |
| reflect | 16 | 1 | 0 | **+1** |

Between groups, Fisher **p=0.101, not significant**. This n cannot resolve a protocol effect, and
the only thing that can be said is: **the debate showed no advantage.**

#### The mechanism: all three crossings of the 0.5 similarity line lost points

All three regressions carry the same signature — the changed code no longer resembles its own
draft and closely resembles the rival's:

| | similarity to own r0 | similarity to rival's r0 | Δ |
|---|---|---|---|
| debate-7/bob | 0.10 | 0.75 | −1 |
| debate-8/alice | 0.08 | **0.97** | −11 |
| debate-12/alice | 0.17 | **0.95** | −11 |
| the 13 cells that did not cross | 0.53–0.97 | 0.00–0.43 | only 1 regression |

`debate-8/alice` is the cleanest case: it wrote a correct 34/34 implementation in round 0 against
bob's 23/34. After one round of debate, the code alice handed in had similarity **0.97** to bob's
draft and 0.08 to its own, inheriting even the error message `ValueError: 非法版本号: '='`.

`debate-12` is closer to a natural control — the two swapped places within one run:

```
alice  34 → 23   own 0.17  rival 0.95   copied the rival, and broke
bob    23 → 34   own 0.84  rival 0.14   held its own and fixed it, and got there
```

**In all three copies the party copied from scored no higher than the copier.** Not once was it a
move towards the better side — the same direction as the literature's "57–77% of strict-conformity
flips go from right to wrong".

#### How strong this conclusion is, stated precisely

- within-group Fisher p=0.007, but that **includes the two batches in which it was found after
  the fact**, so it is not confirmatory evidence
- the third batch **was run after pre-registration** (the test, the threshold and the falsifying
  condition written down first), and the main prediction's direction held, but the copying group
  is **n=1** — an extremely weak confirmation
- the reflect group's zero regressions **are not independent evidence**: nobody sees anybody, so
  copying is structurally impossible (that group's "similarity to rival" peaks at 0.16); it is
  merely the absence of this mechanism
- a great many cells sit at the 34/34 ceiling, depressing the power further

#### How the threshold of 0.5 was set

In the reflect group nobody sees anybody, and the similarity between two independent drafts
measured 0.03–0.16 — the natural overlap of one model under one prompt. 0.5 sits far above that
baseline.
**It was calibrated in exactly one setting, DeepSeek × semver**; a different model or task needs
recalibrating.

#### What was built into the tool

`sesa eval` now reports copying events (`evaluate.code_adoption`). Three disciplines:

1. **report the fact, do not judge it good or bad** — that depends on whether the execution
   evidence improved, not on this number
2. **"could not measure" is kept apart from "measured and found nothing"**
   (`AdoptionReport.measurable`) — an agent CLI writes its own files and the code never enters
   the turn, so such runs cannot be measured at all
3. **"had nothing of your own and took the other's" is not copying** — measured, the former
   happened 4 times and the latter 3, and only the latter came with a drop in score; conflating
   them takes the detection count from 3 to 7, and the extra 4 carry no signal

> What this means for Sesa is direct: **the risk in a debate is not failing to converge, it is
> converging on the wrong side.**

#### Wired into the engine (no longer only a retrospective metric)

During a deliberation, after each round's evidence has run, the engine performs a copy check,
reading **the working copy itself** rather than the code blocks in the prose — so an agent CLI
that writes its own files can be measured too. A detected event carries the self-test exit codes
from before and after the copy, so for the first time "converged on the other party" can be told
from "converged on the right answer":

| Case | What the engine does |
|---|---|
| copied + self-tests went from passing to failing | the outcome is **downgraded from `consensus` to `consensus_with_reservations`** (exit code 0→3), `RESULT.md` puts a warning **before** the conclusion, and says which branch still holds the abandoned implementation |
| copied, but the evidence did not get worse | state the fact only, judging neither way — they may genuinely have been convinced |
| copied, but the run has no execution evidence | **no downgrade**. Similarity does not answer good or bad, and without evidence no judgement is made |
| they did not have that file last round | not copying. Having nothing of your own and taking the other's is different in kind from throwing your own work away |

The downgrade happens only when there is execution evidence, and that is deliberate: this project
has been caught four times by an empty value masquerading as data, and a similarity number must
not be allowed to pass for a judgement of correctness.

### 14.19 Heterogeneous debate: the product's core premise, measured for the first time, unsupported

Every earlier correctness experiment with ground truth had **two copies of the same model as the
participants** — while this project's core premise is "combinations of **different** agents and
**different** LLMs".
Which is to say: those negative conclusions were not measuring the product's premise at all.

The configuration: `claude` (the cli adapter, writing its own files) + `deepseek`
(openai_compat, with the engine writing for it), **heterogeneous on both the adapter and the
model**, with the same role for both.
The task was the full node-semver grammar plus range set operations, 118 held-out tests. 4 runs
per group.

#### Setting the task was itself a lesson

| Task | Held-out tests | claude alone |
|---|---|---|
| the old semver | 34 | 4×34/34 |
| the full semver grammar | 85 | 85/85 |
| + range intersection/containment | 118 | **118/118 × 8 runs** |

Three independent increases in difficulty, three sets of full marks. **Going on raising the
difficulty is p-hacking at the task level** — changing the task until the desired result appears.
So it stops here:

> **A strong model + a well-specified task = no room for improvement.**
> A debate cannot add anything here, because there is nothing to add.

On the second task claude also caught **an error by the person setting it**:
`subset('~1.2.3', '1.2.3 - 1.2.99')`, which my exhaustive oracle judged True and it judged False.
The counterexample is `1.2.100` — both of my validating universe patches only went to 6 and 9,
**missing the same counterexample in the same way**, which made the "compare two universes"
self-check worthless. It now derives the universe from the literals in the expression.
(This is the second time in this round that the party under test found an error by the person
setting it; the previous one is in 14.17.)

#### The results

| | n | mean r0 | Δ per run | mean Δ |
|---|---|---|---|---|
| debate | 4 | 81.0 | +5, 0, +24, +19 | **+12.0** |
| reflect | 3 | 70.3 | +19, +44, +6 | **+23.0** |

- **The main prediction (the weaker one gains more under debate) did not hold, and the direction
  is reversed.** But n=4 against 3, Δ spread from 0 to +44, and reflect starting 10.7 points
  lower (regression to the mean favours it) — **enough only to say the main prediction was
  unsupported, not that the reverse holds**
- **The secondary prediction (the stronger one does not regress) holds and carries no
  information**: claude scored 118/118 in all 8 runs, was already at the ceiling, and the
  round-by-round code was not being persisted at the time, so intermediate ups and downs cannot
  be ruled out
- **The mechanism prediction could not be tested**: copying occurred 0 times (it occurred only 3
  times in the homogeneous group's 32, so it is rare to begin with)

#### Four of our own defects this round exposed

| Defect | Consequence |
|---|---|
| the round-by-round working copy was computed and discarded | an agent CLI that writes its own files leaves no intermediate work, so the secondary prediction could only look at the end state |
| the truncation fix overcorrected | it swung from "accept silently" to "discard the whole thing", throwing away usable code from 4 turns, one of which had already written 109/118 |
| a subprocess failure reported only stderr | claude's `You've hit your session limit` is written only to stdout, so 8 runs were wasted with no visible reason |
| `reflect` could not see its own execution evidence | the control group ended up measuring "nothing at all" rather than "no peers" |

The fourth is worth noting for its direction: before the fix the debate group's information was
**strictly greater** than the self-review group's (it had the other's claims, both exit codes,
and its own test results), and **it still did not win with more information**.
Strictly, though, the post-fix comparison has to be re-run to give numbers; the old ones cannot
stand in for a result under the new control.

### 14.20 Using Sesa on Sesa: one closed loop, eight real defects

The topic: **the four design bottom lines in the README — does the code actually do them?** The
participants received this repository's full source (a git worktree each, readable, writable and
testable), with every finding required to give `file:line` + a failure scenario + how it was
verified, and with it stated plainly that **"reporting no problems is a valid answer, inventing
one is not"**.

The result: claude wrote 10 tests, **8 of which failed against the code at the time**, and
verified mechanically one by one, **all held**.

| Bottom line | Defect |
|---|---|
| — | **`sesa run` crashes for certain in a real terminal**: `ConsensusReport(unresolved=…)` passes a property as a field |
| 2 | zero agree cells still judged `partial_coverage_consensus` — a "consensus" with partial coverage still needs a consensus first |
| 2 | an empty matrix (one participant, or nobody taking a position) has no blockers ⇒ `converged=True` |
| 2 | the outcome says "partial coverage" while the delivered JSON says `coverage=1.0` |
| 2 | `reconcile` describes `unresolved` (opposition + not measured) wholesale as "explicit opposition" |
| 3 | **rewording a residual makes deadlock detection never fire** |
| delivery | `partial_coverage_consensus` / `not_measured` have no banner and degrade to a bare enum value |
| delivery | `not_measured` written up as "this deliberation did not reach consensus" |

#### The two most embarrassing

**One, `sesa run` crashes for certain in a terminal.** That path runs only under a TTY; a pipe
and `--json` both bypass it. And every manual check of mine had redirected output to a file, and
all 264 tests ran without a TTY — **so the path a new user hits at step one had never once been
executed**. In the message just before this I had reported "installs cleanly, runs, end-to-end
verification passed".

**Two, rewording a residual defers a deadlock indefinitely.** `_stalled_rounds`'s comment says
plainly that "a self-reported 'I changed my position' is **not enough** to reset the stall
counter … otherwise a participant fond of saying they changed could defer deadlock detection
indefinitely", while the `_residuals_changed` it calls on the very next line has a docstring
saying "**an objective signal that does not depend on self-report**" — **and the residuals are
themselves self-reported text**. The front door was shut against self-report and the side door
let it back in, under the name "objective".
This is another instance of 14.5's naming error: **calling something "objective" does not make it
objective.**

A measured comparison (both positions identical every round, the wording the only variable):
without rewording → `deadlock` at round 2; rewording every round → `stalled` stayed 0 for five
consecutive rounds.
It now registers only **a change in the count**, and the docstring states that this can still be
gamed by splitting sentences, merely at a higher cost, rather than pretending it is airtight.

#### About this loop itself

- **claude's turn was ultimately marked failed** (it hit its session quota), but its working copy
  had already been committed to the branch — **the output survived, the turn did not**. These
  tests were recovered from the failed round.
- So this **was not really a two-sided debate**; it was "claude reviewing alone + deepseek
  reviewing alone". The protocol layer's value went unmeasured this time.
- deepseek's side received a `briefing` (18 unfiltered scan findings from the external tool
  open-code-review, with an explicit note that "it may contain false positives, and your job is
  to verify item by item").
  **⚠️ That configuration is not realistic**: in real use the scan report goes to everyone (see
  14.22). Every observation in this section about "debate dynamics" is therefore doubtful;
  **the defects themselves are unaffected**, having been verified against the code one by one.
- In an earlier round the same external tool independently found 6 more real defects (the version
  number drifting apart, the outcome being settled 70 lines after the drafting, two escaping
  omissions, and so on), and all were likewise verified as real.

> **Self-review has systematic blind spots, and they do not go away with "being more careful".**
> All 14 real defects this round came from code I wrote that day and had reviewed several times
> myself, and I saw none of them. Another pair of eyes — even a weaker model plus an external
> tool — found them.

### 14.21 Four loops: the output curve did not converge, and why

Using Sesa on Sesa, with the topic fixed as "the four bottom lines in the README — does the code
actually do them?" and every finding required to give `file:line` + a failure scenario + how it
was verified. Real defects per round:

| Round | What was scanned | Real defects |
|---|---|---|
| 1 | the engine and the consensus core | 8 (plus 6 found independently by an external tool) |
| 2 | the delivery layer | 14 |
| 3 | types / config / adapters / workspace | 9 |
| 4 | **rescanning round 1's modules** | **18** |

Round 4 was designed to answer "have we reached the bottom". **The answer is no, and the output
went up.**

#### But that curve has a confound, which has to be stated

The first three rounds scanned different modules, measuring "how many mines are left in new
ground", not "has the same ground got cleaner". Round 4 did rescan, but those modules had been
heavily changed over the first three rounds — so it is not purely measuring what remained;
**a fair proportion was newly introduced while fixing things**.

The latter deserves more alarm. At least two of round 4's findings were created by that day's
fixes:

> To fix a monotonicity inversion ("a weaker agreement buying a better outcome"), I added
> `if report.min_confidence and ...` to the confidence bar. **And 0.0 is falsy** — so a
> confidence of 0.00 was judged "consensus with reservations" and 0.01 "unfinished".
> **The same function, within an hour, a second inversion of the same shape.**

The root cause is `0.0` doubling as the sentinel for "not reported" and as the legitimate value
"I am very unsure". Another instance of 14.5's error: **one slot carrying two meanings will go
wrong eventually.**

#### One root cause, four symptoms

`find_json_blocks`'s docstring says "code blocks first, bare objects after", while `parse_stance`
locates the final stance card by "take the first one scanning backwards" — two contradictory
assumptions. The consequences: when a turn quotes someone else's stance card (very common when
discussing disagreements), the quoted one overrides the author's own; `strip` and `parse` select
different blocks; and the judge's overall verdict is displaced by JSON it quoted.

**Finding scattered same-source errors is worth far more than finding an isolated bug.** Another
of the same kind is "calling the unmeasured a disagreement", committed once at each of four
outlets, emerging from the next one every time it was fixed.

#### What all this shows

1. **Self-review has systematic blind spots, and they do not go away with "being more careful".**
   Not one of the 49 defects was found by the author's own review — and every line had been
   reviewed several times.
2. **Fixes are the main source of new defects.** This is not "the code is bad", it is the normal
   state of "a change is a risk"; the only difference is whether there is a pair of eyes that can
   see it on the spot.
3. **Do not accept the lot.** One round-4 item asked to cut out the stance card a participant had
   quoted from someone else — which would delete their evidence; it was rejected with the reason
   written down. **A conclusion is verified, not voted on.**

#### With a different approach, the curve finally came down

The first four rounds scanned whole modules. The fifth changed to **reviewing the diff just
written** — because the curve had already shown that new defects come mostly from the fixes. The
same diff was reviewed three times over:

| Pass | Reported | Real | Highest severity |
|---|---|---|---|
| 1 | 12 | 11 | high |
| 2 | 4 | 4 | medium |
| 3 | 1 | 1 | medium |

**Reviewing "what was just changed" is far better value than reviewing "the whole module"**: the
hit rate went from about half to nearly everything, and real convergence became visible. The
non-decreasing curve of the first four rounds was largely because the ground changed each time.

#### Three "the fix introduced a new defect", identical in shape

| What I fixed | What I created |
|---|---|
| the monotonicity inversion (a weaker agreement buying a better outcome) | testing 0.0 with `if x and`, creating a second inversion |
| "could not check" and "nothing wrong" must be separated | both cases `return []`, while the docstring claimed they were distinguished |
| the two extraction paths judged differently | made one of them judge "the reason for agreeing" as "a reservation", manufacturing disagreement out of nothing |

**Every one is the error I was guarding against, committed again in another direction**, and every
one was caught on the spot by the next loop.
The third is especially worth recording: the original defect was a false negative, and my fix
turned it into a false positive — **a false positive is worse**, because a false negative still
has default-deny's other gates behind it while a false positive conjures something from nothing.

> This method is packaged as `examples/self-review/` and any project can use it directly.
> It is also this project's **only test that runs a whole deliberation for real** — a defect like
> the TTY rendering crash can never be seen under pytest, because the tests run without a
> terminal.

### 14.22 The fourth methodological discipline: the configuration must be one the product produces naturally

The first three disciplines (14.5, 14.7, 14.11) all guard against "the measurement method was
wrong". This one is more basic and guards against "**what was measured is not a thing that
happens**":

> A user starting a deliberation gives **one** task, and every participant sees it.
> With a scan report, an RFC or a log export in hand, **there is no reason whatsoever to show it
> to only one participant** — if you share it, share it with everyone.

Across those four self-review rounds I hung a `briefing` (material private to one participant) on
deepseek and not on claude, on the grounds of "creating information asymmetry so the disagreement
carries information". **That was a scenario constructed for an experiment.**
Worse, I wrote the same configuration into `examples/self-review` — and an example that
demonstrates a usage the user will never have teaches them the wrong thing.

#### The boundary of what is affected, stated clearly

| Conclusion | Affected? |
|---|---|
| the defects themselves | **unaffected**. Each was written up as a failing test, run, and seen to go red — which has nothing to do with who saw what material |
| "what the debate produces is a new allocation of attention" (14.20) | **affected, downgraded to doubtful**. That chain (deepseek relaying the tool's finding → claude digging along it) **depends entirely on only one side seeing the scan results**. Under a realistic configuration both see them and that transfer never happens |
| the 24 semver-weak runs and the 8 heterogeneous runs | **unaffected**; the material was symmetric in both. Injecting the working directory for deepseek in the heterogeneous group was because it cannot read files — that **levels the two sides**, in the opposite direction |

#### What was done about it

- `examples/self-review` switched to `--file`: the scan results are merged with the topic and
  visible to everyone
- the test changed from "briefing goes to only one" to "**the example must not demonstrate
  private material**"
- `briefing` is kept but demoted to an exceptional channel, with the test written into its
  docstring: **why can this material not be shown to the others? If you cannot answer, use
  `--file`.**

> This discipline is the hardest to keep because **the experiment runs beautifully when it is
> broken**: the data is clean, the conclusions are self-consistent, the metrics are fine. Only
> asking "would a user really do this?" afterwards shows the whole thing hanging in mid-air.

#### It happened again the same day, in the shape of "unequal ground"

Running "can two weak models stand in for one strong one", I gave the two groups **search scopes
differing by 9×**:

| Group | Code scanned |
|---|---|
| claude, four rounds | 6858 lines (the engine, consensus, delivery — the core modules) |
| kimi + deepseek, one round | **761 lines** (the wizard, semantic, two small protocols, two adapters) |

I then compared "1 finding in a round" against "8–18 in a round" and wrote "the gap is an order
of magnitude". **That sentence has been withdrawn.** Putting someone in a small room to look for
something and then saying they found less than the person searching a whole building is not
comparing ability, it is comparing room sizes.

The task brief said "review this project"; I narrowed the scope to a small patch while
implementing it.
**The discipline has to check not only "would a user configure it this way" but "did the two
groups get equal conditions".**

### 14.23 Where a finding came from has to be kept straight: the tool, the deliberation, or the author

A day's work accumulates a great many defects, and it is easy to describe them collectively as
"found by multi-party review". **That is inaccurate**, and vagueness of exactly this kind is what
this project should most guard against:

| Source | Form | Real defects |
|---|---|---|
| Claude self-review, 4 rounds | **a Sesa deliberation** (claude + deepseek arguing) | 8 / 14 / 9 / 18 |
| ocr reviewing the diff × 3 passes | **the tool alone** (driven by DeepSeek) | 11 / 4 / 1 |
| kimi + deepseek | **a Sesa deliberation** | **1** |
| ocr scanning all of `src/` | **the tool alone** (driven by DeepSeek) | 70 reported, most verified as real |
| the author sweeping backwards | by hand (prompted by earlier rounds) | 2 classes of scattered error |

Those last 70 **involve no debate at all**: `ocr scan` run alone, with no participants and no
deliberation record, and kimi took no part whatsoever. Counting them as "what kimi + deepseek
discussed" would badly overstate what the deliberation produced.

#### It also means one comparison still has no answer

"Can two weaker models plus an external tool stand in for one strong model?" —

- weak models **deliberating**: 1 (over 761 lines of ground)
- Claude **deliberating**: 49 (over 6858 lines)
- a **tool** driven by a weak model: 70 (over all 8642 lines)

Three numbers from three different things, and **not one pair is comparable**. The third line
proves the tool is useful and says nothing about weak models' ability to deliberate. Answering it
properly needs both groups to hold a deliberation over the same batch of code **that neither has
reviewed** — and those 70 have already been fixed, so comparing over the same code is no longer
fair.

### 14.24 A fixed shape of attribution error: whoever you read first gets the credit

Committed three times in one day, identical in shape.

| What I said | The fact |
|---|---|
| "the 70 items kimi + deepseek discussed" | `ocr scan` run alone, with no participants and no deliberation record |
| "deepseek raised the minority-opinion truncation" | it was in the scan material given to it, almost word for word |
| "deepseek pointed out the R3 regression" | **claude wrote `### R3.` in round 0**, and the label is theirs; deepseek quoted it in round 1 |

The mechanism is the same all three times: **whichever material I read first decided who I gave
the credit to.**
The third is the clearest — deepseek handed in first (23s against 988s), briefly, with a striking
conclusion, and on seeing "R3 is the most severe regression" I went straight to verifying it,
**never opening claude's 9,673 characters**.

#### How to guard against it

Ranking information ("this one is the most severe") and provenance information ("this one was
found by X") have to be booked separately. To judge where a finding came from, look at three
things:

1. **the timestamp** — who handed in first, in the event stream
2. **the labels and the wording** — quoting someone else's label (`R3`) means agreeing, not
   originating
3. **the depth** — the originator usually gives a root cause; the one agreeing can only give an
   assessment

> All three are sitting ready in `events.jsonl`. **Attribution should not rest on impression, and
> this project's entire methodology is "do not rest on impression"** — and yet it did exactly that
> three times over its own bookkeeping.

#### But the ranking has value; just do not call it the wrong thing

deepseek ranking R3 "most severe" really did put it in front of the author first — claude reported
ten items, and reading them in order would have spent time on R1 and R2 first.
That is **an allocation of attention**, of the same kind as 14.20's observation, and not "the weak
model outperformed the strong one".

### 14.15 The only reliable observations so far

Across six deliberations (two with claude ×2, four with DeepSeek ×2, including a residual-feedback
on/off comparison):

| Observation | Value | Reliability |
|---|---|---|
| the total of open reservations does not fall with the rounds | balance 6→6 / 6→7 / 5→6 / 15→12 / 15→15 | **reliable**, pure counting |
| categorical judgement barely moves after the first position | 3 times across 33 runs | **reliable**, categorical |
| participants self-reporting "I changed my position" | 33 times | **over-reports by about 11×** |
| whether feeding residuals back suppresses turnover | 0.962 vs 0.909 (n=2) | no difference, sample far too small |

These **are not enough to judge whether several rounds of debate are worth it** — "the total does
not fall" may mean marking time or may mean the discussion is going deeper. Judging that needs a
semantic method, and that is the next step.

### 14.16 Load-bearing claims still unverified

The following claims have **no experimental support** and hold only on the plausibility of their
mechanism. Each has produced a concrete design decision, so each deserves a controlled experiment:

| Claim | The design it produced | How to verify it |
|---|---|---|
| a stance is the cheapest quality gain | Role injection | with-role / no-role comparison |
| taking turns carries position bias | parallel within a phase | a `turn_taking: sequential` comparison, checking whether the first speaker's position is over-adopted |
| more participants make the disagreement more informative | recommending 3 or more | a 2/3/4-participant comparison |

> An observation with no explanation yet: **not one `agree` appeared in the final round of three
> deliberations; they were all `partial`.** Which means the `CONSENSUS` grade may be nearly
> unreachable in practice. If later data bears that out, the exit codes and the grades of outcome
> need redesigning.

---

## 15. Settled decisions

| Decision | Conclusion | Reason |
|---|---|---|
| Language / distribution | Python + uv (`uvx sesa`) | the prototype can evolve into it; Textual is the strongest TUI framework and `textual serve` turns it into a web app; cross-language embedding goes through JSONL/MCP |
| Product shape | a headless core + three front ends | if the TUI were the body, anyone embedding it would have to strip the rendering out; the event stream is the reusable contract |
| Scope of tasks | both text topics and code tasks | they differ at only two seams, Workspace and Evidence; the other four layers are shared |
| Consensus assessment | the structured stance card first; retry on a parse failure, and record `unknown` on a second | CLI agents often break the format; the matrix is computable, explicable and reviewable; no guessing and no writing on anyone's behalf |
| Honesty about outcomes | consensus / deadlock / exhausted kept distinct | stuck ≠ united, and nothing is papered over |
| **No arbiter** | the consensus is the deliverable; if it does not converge, the positions go to a person side by side | it avoids "overwriting the debate's result with an opinion that was never debated", and removes the friction of one more API key |
| Writing up after convergence | a participant writes it on rotation (the Rapporteur), with `drafted_by` attached | the writing starts only once the disagreements are zero, and integrates wording rather than ruling on right and wrong |
| Number of participants | the user's choice, at least 2, no upper bound | configuration-driven, with no assumed "recommended number" |
| Output shape | a `RESULT.md` with a constant skeleton, where agreement and disagreement change only the proportions | one way of reading it; a disagreement must carry its root cause, what is needed to decide, and a next step, rather than being listed as-is |
| Disagreement is resumable | `sesa resume --inject` continues from where it stopped | a deadlock is an intermediate state, not a failure |
| Turn scheduling | phases in sequence, parallel within a phase; round 0 forced parallel | taking turns carries position bias, and the answering happens in the next round anyway |
| Solo roles | the rapporteur / the speaking order / the proposer **all rotate by default** | not by picking "an impartial one", but by keeping the position moving |
| Visibility of the reasoning | not shared by default, openable with `on_deadlock` / `always` | the reason has been corrected to **cost and parity between adapters** (measured +54% tokens); "it prevents premature convergence" was pointed the other way by a controlled experiment, see 14.3 |
| Credibility of evidence | graded `engine` / `claimed`; cross-testing against self-testing; evidence is rebuttable | an execution result is harder than rhetoric, but **it is not truth** — a green light means nothing when whoever writes the code also writes the tests |
| Premise for taking a position | a position may be taken only on someone **whose turn was read**; no position taken blocks consensus | otherwise the model rates people out of thin air and "consensus" can be reached in round 0 |
| Two-way false-consensus check | the rapporteur reports a conflict, or the matrix claims agreement while the rapporteur lists disagreements | the latter is the case where the stance cards failed and the rapporteur caught it |
| default-deny counting | only an explicit `agree` counts as resolved | measured twice: "both sides partial on each other → judged converged → the rapporteur read a substantive disagreement out of it" |
| Consensus ≠ termination | consensus is a report label; termination is guaranteed by the rounds and the budget | conflating them makes people believe "a strict assessment would degrade the tool" |
| Reservations as a grade | `consensus_with_reservations` is a grade of its own | having reservations is not a failure, and it should not be written up as full agreement either |
| The budget applies per call | one call's timeout is squeezed by the remaining wall clock | checking only at round boundaries let a 900s cap be overrun to 1185s in practice |
| Persisted configuration | a global participant library plus per-project overrides, with credentials in the keyring by default | filled in once and reused indefinitely; an open-source project should not teach people to store keys in plaintext |

### 14.25 "Rating a conclusion" and "checking the evidence" are two different things

The stance card originally let a participant rate only the **conclusion**:
`agree | partial | disagree`. No field in the schema touched the other side's **evidence**. The
consequence showed up cleanly in run 20260901-103359, where one party took this position:

```json
"stance_on": {"kimi": "partial"},
"premises": ["kimi's test file has been written into the working directory, but my execution
              happened before that, so I cannot verify its failure myself; I am relying on its
              reported output."]
```

**It admits it did not verify, gives a conceding position at the same time, and this is entirely
compliant.** The protocol had no slot for saying "I did not check", so it went into the premises;
the consensus assessment could not see it, and I only found it by reading the premises by hand.

So default-deny was extended from "stance parsing" to "evidence":

> **When the other side produced evidence the engine executed and you agreed without checking any
> of it — that is not agreement, it is not measured.**

Four boundaries that do not move:

1. **The downgrade goes to `unknown`, not to `partial`.** Downgrading to partial would hand two
   unverified parties a "consensus with reservations" badge, which is exactly the hole this
   project keeps falling into: taking "not measured" for "a weaker agreement". Better to fail
   visibly than to succeed suspiciously.

2. **The bar is conditional on the other side really having produced something checkable.** A
   pure design debate contains nothing executable and nobody can verify anybody; demanding
   verification unconditionally would make such deliberations **permanently unable to reach
   consensus** — and those are exactly the ones that most need several parties.

3. **Only evidence the engine executed (`is_fact`) triggers the verification duty.** A
   participant's "I ran it, it passed" is a claim awaiting verification; treating it as evidence
   lets someone impose a verification duty on everyone else by mere assertion.

4. **"I could not check" is a respectable answer and costs nothing** (`how: unable` + the
   reason). Once "is it verifiable" is scored, the parties will manufacture evidence that looks
   executable in order to appear compliant, systematically suppressing the opinions that were
   never meant to be proved by running a test (design trade-offs, naming, maintainability) — and
   those are what a multi-party deliberation should most preserve.

Alongside it: `render_evidence` now gives the **branch name** of the artefacts. A bare "exit code
0" gives the others nothing to re-check, and demanding verification without saying where to look
is unreasonable.

#### 14.25.1 A rule's point of imposition and its point of announcement must be the same place

When the `verified` bar landed I changed the consensus assessment first **and gave the
participants no way to comply** — the prompt did not ask for that field at all. An audit
afterwards of every path by which "a participant can be downgraded" found the same error
committed **five times**:

| # | The hole | The consequence |
|---|---|---|
| 1 | the degraded retry's line table has no room for verification | whoever failed the first parse is moved to a format that **structurally cannot comply** |
| 2 | `stance.emit` does not serialise `verified` | the event stream calls itself the only truth and had lost it |
| 3 | resume recovery does not carry `verified` | after a resume every agree loses its foundation and **consensus becomes impossible** |
| 4 | only the debate family renders the evidence | `adversarial`'s participants are downgraded by a rule they were never told |
| 5 | the bar looks at this round's evidence | this round's evidence runs after the stance card — **punishing the impossible** |

2 and 3 are **the same illness** as the "truncation flag lost" fixed that same morning (kimi's
scan item 62): a new rule written into memory, with no thought for how it comes back from the
event stream. Committing it again in new code immediately after fixing one means this is not
carelessness but a missing discipline.

The discipline that came out of it:

> **Wherever a participant can be downgraded, there has to be a path they can take.**
> The code that imposes the rule and the code that announces it must either be in the same place,
> or be pinned together by a test.

So the announcement moved into `Engine._run_move`, right next to `stance_instruction` — **the one
place every protocol passes through**. Left in each protocol's template, every new protocol would
commit it again.
`tests/test_compliance_path.py` pins all five holes down one by one.
