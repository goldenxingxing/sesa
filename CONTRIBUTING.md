# Contributing

Thanks for looking. A few things about this project that are easier to say up front than to
discover.

## Getting set up

```bash
uv venv && uv pip install -e ".[dev,keyring]"
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
```

The optional extras matter for a full run: `[tui]` (Textual) and `[semantic]` (sentence
transformers). Tests that need one are guarded and skip without it, so a missing extra never
turns the suite red — if you see a red that says "you did not install an optional extra", that
is a bug in the test, not in your setup.

## What this project is picky about

These are not style preferences; each of them is a hole this project has fallen into, and every
one has a regression test with the story in its docstring.

**An empty value must never masquerade as data.** "Not measured" and "measured as zero" are
different things and must stay different all the way to the deliverable. A default of `1.0` for
coverage, a partial sum kept when half the usage is unknown, a similarity of `0.0` returned
because there was nothing to compare — each of those was a real defect here.

**A rule imposed must come with a way to comply.** If a participant can be downgraded for
failing to do something, there has to be a path by which they could have done it, and the code
that imposes the rule and the code that announces it must either be the same place or be pinned
together by a test.

**Say it in both languages or neither.** English is the source language and Chinese lives in
`src/sesa/locales/zh.py`, keyed by the English text. A test reads every `t("...")` literal out
of the AST, so adding a string without a translation goes red immediately. Parsing markers
(what counts as an "agree", what counts as a reservation) are a different matter: they follow
the *deliberation's* language, so both languages' markers are always active and never switch
with the interface.

**Test behaviour, not source text.** Several tests here used to assert on
`inspect.getsource(...)`, and they went red on a rename while the behaviour was unchanged —
and, worse, stayed green when the behaviour broke. Where a source check is genuinely the only
option, say so in the docstring.

**A passing test can also have measured nothing.** At least one fixture here was misconfigured
so that two different inputs returned the same result and the assertion held either way. If a
test cannot fail, it is not evidence.

## Writing a test

Docstrings here carry the *why*, not the *what* — the test name says what it checks, and the
docstring says which failure it came from and what it cost. That is deliberate: most of these
tests exist because something went wrong once, and a test without that story gets deleted by
the next person who finds it inconvenient.

## Running the deliberation on your own project

`examples/self-review/` is a reusable configuration; its README explains how to point it at
your own code and the three traps to watch for. It is also the only test here that runs a whole
deliberation for real, so it costs real model calls and is not part of `pytest`.

## Adding an adapter or a protocol

Neither requires changing this package: `sesa.adapters.register` and
`sesa.protocols.register` both take a class. An adapter answers "how do I hand it the words and
how do I get the words back"; a protocol answers "who speaks in which phase and what can they
see". Everything else — concurrency, budget, stance extraction, consensus, events — belongs to
the engine, which is why each concrete implementation is short.
