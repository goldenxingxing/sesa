""" "Someone objected" and "the engine did not measure it" must not be compressed into one
number.

This is the README's second bottom line, and the error this project has committed most
often: the same shape of mistake was committed once at each of **four outlets** — the
RESULT.md prose, the terminal progress output, the consensus blockers, and the REPORT.md
minutes — and fixing one had it emerge from the next.

Fixing them one by one does not work. Two things are done here:
1. give the correct wording a single source (`ConsensusReport.describe_unresolved`)
2. scan **the whole repository** and forbid labelling `unresolved` as "disagreement"
   directly
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sesa.types import ConsensusReport

SRC = Path(__file__).resolve().parent.parent / "src" / "sesa"


def _report(**kw) -> ConsensusReport:
    base = {
        "round": 0,
        "matrix": {},
        "min_confidence": 0.5,
        "converged": False,
        "stalled_rounds": 0,
    }
    return ConsensusReport(**{**base, **kw})


def test_the_wording_separates_opposition_from_missing_data():
    described = _report(opposed=2, unmeasured=3).describe_unresolved()

    assert "2 cells hold explicit opposition" in described
    assert "3 cells not measured" in described
    assert "which is not objection" in described


def test_pure_opposition_says_nothing_about_missing_data():
    assert "not measured" not in _report(opposed=2).describe_unresolved()


def test_pure_missing_data_is_never_called_opposition():
    described = _report(unmeasured=4).describe_unresolved()

    assert "opposition" not in described.replace("which is not objection", "")
    assert "4 cells not measured" in described


def test_nothing_unresolved_says_so_plainly():
    assert _report().describe_unresolved() == "no unresolved cells"


#: phrasings that call "unresolved" "disagreement" outright. The second bottom line forbids that
#: accounting.
_CONFLATION = re.compile(r"未决分歧\s*\{?[a-z_.]*unresolved")


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_labels_unresolved_as_disagreement(path):
    """A repository-wide ban: `unresolved` must not be rendered directly as "N open
    disagreements".

    It equals opposed + unmeasured. To state it, state the parts, through describe_unresolved.
    """
    text = path.read_text(encoding="utf-8")

    assert not _CONFLATION.search(text), (
        f"{path.name} calls unresolved (= opposition + not measured) 'open disagreements' outright. "
        "Use ConsensusReport.describe_unresolved() instead."
    )


# --------------------------------------------------------------------------- # The other face of
# the same class: a limit configured that looks like it is in charge and is not
# --------------------------------------------------------------------------- #


def test_a_cost_cap_that_cannot_fire_says_so():
    """The built-in adapters never invent pricing (usd=None), so max_usd never fires.

    And sesa.example.yaml carries `max_usd: 2.0`. A limit that looks like it is in charge and
    is not is worse than not offering the setting at all — the user believes they have a
    backstop.
    """
    from sesa.budget import Budget
    from sesa.types import Usage

    budget = Budget(max_usd=2.0)
    budget.add(Usage(input_tokens=1000, output_tokens=500, usd=None, known=True))

    assert budget.exceeded() is None, "the premise: the cost cap really did not fire"
    warning = budget.unenforceable()
    assert warning and "will not take effect" in warning
    assert "max_tokens" in warning, "it has to tell the user what to use instead"


def test_a_cap_that_can_fire_stays_quiet():
    from sesa.budget import Budget
    from sesa.types import Usage

    budget = Budget(max_usd=2.0)
    budget.add(Usage(input_tokens=10, output_tokens=10, usd=0.5, known=True))

    assert budget.unenforceable() is None


def test_no_cap_configured_is_not_a_problem():
    from sesa.budget import Budget

    assert Budget().unenforceable() is None


def test_a_priceless_call_is_not_counted_as_free():
    """Token counts with no amount ≠ having spent nothing. The two have to be tellable apart."""
    from sesa.budget import Budget
    from sesa.types import Usage

    budget = Budget()
    budget.add(Usage(input_tokens=1, output_tokens=1, usd=None, known=True))
    budget.add(Usage.unknown())

    assert budget.priceless_calls == 1, "tokens but no amount"
    assert budget.unknown_calls == 1, "not even tokens"
