"""The semantic comparison layer.

Why it exists: counting cannot separate "the same objections reworded" from "moved on to new
questions" — three withdrawn and three added give identical counts either way. Three
counting metrics in this project have tripped over exactly this (DESIGN.md §14.5).
"""

from __future__ import annotations

import pytest

from sesa import semantic


def test_reports_unavailable_rather_than_faking_a_number(monkeypatch):
    """When the dependency is missing, say unavailable honestly and never fall back to a number
    that merely looks usable.

    Falling back to surface-form similarity is the very hole this project keeps climbing out of:
    the number is produced as usual, and whoever reads it assumes it measures what its name says.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("模拟未安装")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    state = semantic.availability()
    assert state.ok is False
    assert "sesa[semantic]" in state.detail

    with pytest.raises(semantic.SemanticUnavailable):
        semantic.similarity_matrix(["甲"], ["乙"])


def test_calibration_cases_cover_both_directions():
    """The calibration set must contain both "should be judged a rewording" and "should not" — a
    set of positive cases only always calibrates successfully.
    """
    expectations = {expected for _, _, _, expected in semantic.CALIBRATION_CASES}
    assert expectations == {True, False}


def test_calibration_includes_the_hard_case():
    """The hardest case is "same topic, different claim" — it shares a topic with "the same thing
    reworded" and opposes it in claim, which is precisely where surface-form methods go over.
    The calibration set cannot be without it.
    """
    names = [name for name, _, _, _ in semantic.CALIBRATION_CASES]
    assert "同话题不同主张" in names
    assert "同义改写" in names


def test_empty_inputs_are_handled():
    """Nothing to compare ⇒ **cannot be measured**, not "measured as 0".

    In this metric 0.0 means "not one item is a rewording" — a strong positive conclusion.
    Using it as the "no data" return has missing measurement pass for a good result.
    """
    assert semantic.similarity_matrix([], ["甲"]) == []
    assert semantic.restatement_rate(["甲"], []) is None
    assert semantic.restatement_rate([], ["甲"]) is None


@pytest.mark.skipif(
    not semantic.availability().ok,
    reason="需要可选依赖 sentence-transformers：pip install 'sesa[semantic]'",
)
def test_threshold_sensitivity_is_exposed_not_hidden(tmp_path):
    """The rewording rate is extremely sensitive to the threshold — the tool has to make people see
    that, rather than handing over a number and leaving it there.

    .. note::
       This one **must be guarded**. It depends on the optional ``[semantic]`` extra, which
       ``[dev]`` does not include — without it the test goes red. A contributor's first `pytest`
       showing a red would have them think the project is broken rather than that they are
       missing an optional extra. (Leaving semantic out of ``dev`` is deliberate: making everyone
       download hundreds of MB of model for one test is not a fair trade.)

    Measured: over the same residuals, a threshold of 0.50 gives 0.94 and 0.80 gives 0.02.
    Real similarities are a continuum from 0.44 to 0.81, and any threshold cuts right through
    the middle.
    """
    from sesa.evaluate import RoundMetrics, RunMetrics

    metrics = RunMetrics(run_id="r", task="t", protocol="debate", participants=["a", "b"])
    metrics.rounds = [
        RoundMetrics(0, residuals={"a → b": ["原始的一条保留意见"]}),
        RoundMetrics(1, residuals={"a → b": ["原始的一条保留意见", "另一条完全不同的新问题"]}),
    ]
    sens = metrics.restatement_sensitivity(thresholds=(0.3, 0.9))
    assert set(sens) == {0.3, 0.9}
    assert sens[0.3] >= sens[0.9]  # the higher the threshold, the fewer are judged reworded


def test_similarity_is_none_when_there_is_nothing_to_compare(tmp_path):
    from sesa.evaluate import RoundMetrics, RunMetrics

    metrics = RunMetrics(run_id="r", task="t", protocol="debate", participants=["a", "b"])
    metrics.rounds = [RoundMetrics(0), RoundMetrics(1)]
    assert metrics.residual_similarity() is None
