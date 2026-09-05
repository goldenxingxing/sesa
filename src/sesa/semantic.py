"""Semantic similarity — for telling "the same objections reworded" from "moved on to new
questions".

**Why it is needed**: counting cannot make that distinction. Three withdrawn and three
added may be the same batch of objections in new clothes, or the old ones resolved and
deeper ones found — the counts are identical either way. Three counting metrics in this
project have tripped over exactly this (see DESIGN.md §14.5).

**Why local embeddings and not a judge model**: reproducible, offline, zero marginal
cost, and it does not reintroduce the "referee" role we deliberately avoid — it answers
only "do these two passages resemble each other", never "who is right".

**When the dependency is missing it reports unavailable honestly and never falls back to
a number that merely looks usable** — that is the very hole this project keeps climbing
out of.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from ._install import install_hint
from .i18n import t

#: The default model is **chosen by calibration, not by hunch**. Three candidates were run
#: over the same known cases:
#:
#: ===================================== ========= ========== ========
#: model                                 min para. max new-Q. gap
#: ===================================== ========= ========== ========
#: paraphrase-multilingual-MiniLM-L12-v2    0.620      0.700    −0.081
#: BAAI/bge-small-zh-v1.5                   0.756      0.849    −0.093
#: **BAAI/bge-base-zh-v1.5**                0.717      0.602    **+0.115**
#: ===================================== ========= ========== ========
#:
#: The first two are **inverted** across the interval that matters: they score "same topic,
#: different claim" higher than "the same thing reworded", which is to say they capture the
#: topic rather than the claim. The smaller the model the worse it gets (bge-small gave
#: 0.849).
DEFAULT_MODEL = "BAAI/bge-base-zh-v1.5"

#: The threshold for calling something a rewording: the midpoint between the two classes as
#: calibrated, not a conventional value.
DEFAULT_THRESHOLD = 0.66


class SemanticUnavailable(RuntimeError):
    """Semantic comparison is unavailable (the dependency is missing, or the model failed to
    load).
    """


@dataclass(frozen=True)
class Availability:
    ok: bool
    detail: str


def availability(model: str = DEFAULT_MODEL) -> Availability:
    """Probe whether semantic comparison is available, without raising — so doctor and eval can
    decide whether to report that column.
    """
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return Availability(
            False,
            t(
                "sentence-transformers is not installed. Run `{hint}` to enable it. "
                "Until then every semantic metric is reported as unavailable rather "
                "than falling back to surface-form similarity.",
                hint=install_hint("semantic"),
            ),
        )
    try:
        _load(model)
    except Exception as exc:
        # A probe exists to report any failure honestly, so catching everything here is deliberate
        return Availability(
            False,
            t(
                "Model {model} failed to load: {kind}: {exc}",
                model=model,
                kind=type(exc).__name__,
                exc=exc,
            ),
        )
    return Availability(True, t("Model {model} is ready", model=model))


@lru_cache(maxsize=2)
def _load(model: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model)


def similarity_matrix(left: list[str], right: list[str], model: str = DEFAULT_MODEL):
    """Return the ``left × right`` cosine similarity matrix."""
    if not left or not right:
        return []
    state = availability(model)
    if not state.ok:
        raise SemanticUnavailable(state.detail)

    encoder = _load(model)
    from sentence_transformers import util

    a = encoder.encode(left, convert_to_tensor=True, normalize_embeddings=True)
    b = encoder.encode(right, convert_to_tensor=True, normalize_embeddings=True)
    return util.cos_sim(a, b).tolist()


def restatement_rate(
    before: list[str],
    after: list[str],
    threshold: float = DEFAULT_THRESHOLD,
    model: str = DEFAULT_MODEL,
) -> float | None:
    """What fraction of ``after`` is a rewording of something in ``before``; ``None`` = cannot
    be measured.

    ``0`` = every item is a newly raised question (the debate is moving forward);
    ``1`` = every item has a semantic near-match in the previous round (the same objections
    reworded).

    ``threshold`` defaults to the midpoint between the two calibrated classes (see
    :data:`DEFAULT_THRESHOLD`). A different model must be recalibrated —
    :func:`calibrate` tells you straight away whether the two classes separate at all.
    """
    # Nothing to compare ⇒ **cannot be measured**, not "measured as 0". In this metric 0.0 means
    # "not one item is a rewording" — a strong positive conclusion. Using it as the "no data" return
    # has missing measurement pass for a good result.
    if not after or not before:
        return None
    rows = similarity_matrix(after, before, model)
    restated = sum(1 for row in rows if max(row) >= threshold)
    return restated / len(after)


CALIBRATION_CASES: list[tuple[str, str, str, bool]] = [
    (
        "逐字相同",
        "残差绑定 ID 加文本相似度无法捕捉语义等价的保留意见",
        "残差绑定 ID 加文本相似度无法捕捉语义等价的保留意见",
        True,
    ),
    (
        "同义改写",
        "残差绑定 ID 加文本相似度无法捕捉语义等价的保留意见",
        "给残差加编号再比字面，识别不了换了说法但意思一样的那些保留",
        True,
    ),
    (
        "同话题不同主张",
        "残差绑定 ID 加文本相似度无法捕捉语义等价的保留意见",
        "残差绑定 ID 是可行的，文本相似度足以覆盖绝大多数情形",
        False,
    ),
    (
        "同领域新问题",
        "残差绑定 ID 加文本相似度无法捕捉语义等价的保留意见",
        "无人审查的全自动场景才是默认前提，人工兜底不该被计入设计假设",
        False,
    ),
    (
        "完全无关",
        "残差绑定 ID 加文本相似度无法捕捉语义等价的保留意见",
        "今天下午三点在会议室讨论季度预算分配",
        False,
    ),
    # What follows is taken from residuals in real deliberations. Hand-written short sentences are
    # distributed very differently from real text — real residuals are long and packed with shared
    # terminology and file line numbers, and shared terminology lifts similarity across the board. A
    # calibration set of hand-written short sentences calibrates the wrong interval.
    (
        "真实·同义改写",
        "我尚未接受：B 方案在现有引擎结构下（无主张 ID 机制、无结构化输出要求）"
        "的可行性未被论证，改造成本被低估",
        "我不接受 B 的可行性论证：当前引擎既没有主张 ID 也没有结构化输出要求，"
        "他对改造工作量的估计偏低",
        True,
    ),
    (
        "真实·推进到新问题",
        "我尚未接受：B 方案在现有引擎结构下（无主张 ID 机制、无结构化输出要求）"
        "的可行性未被论证，改造成本被低估",
        "未接受：无人审查全自动是唯一适用场景。我认为测量结果最终需给人看，纯自动闭环不是默认场景",
        False,
    ),
]


def calibrate(
    model: str = DEFAULT_MODEL, threshold: float | None = None
) -> list[tuple[str, float, bool, bool | None]]:
    """Run over the known cases and return (case, similarity, expected-to-be-a-rewording,
    verdict at the threshold).

    **Look at this table before using it.** Three metrics in this project were used first
    and calibrated afterwards, and two of them were inverted across the interval that
    matters (DESIGN.md §14.5).
    """
    # **The threshold has to travel with the model.** `DEFAULT_THRESHOLD` was calibrated for
    # `DEFAULT_MODEL`; judging another model by it is measuring one thing with another's ruler — and
    # the entire reason this function exists is "calibrate before you use it".
    if threshold is None:
        threshold = DEFAULT_THRESHOLD if model == DEFAULT_MODEL else None

    out = []
    for name, a, b, expected in CALIBRATION_CASES:
        score = similarity_matrix([a], [b], model)[0][0]
        # Give None when the threshold is unknown, rather than forcing a verdict with an
        # inapplicable one — that would make the whole calibration table look conclusive while the
        # conclusions are wrong.
        verdict = None if threshold is None else score >= threshold
        out.append((name, round(score, 3), expected, verdict))
    return out
