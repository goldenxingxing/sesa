"""Consistency of the evidence ledger: an invalidated metric must not still be holding a
conclusion up.

When a pillar of a conclusion is knocked out, the conclusion does not fall with it
automatically — not unless someone goes back to look. This project has twice measured
"the pillar was removed and the conclusion still hung there":

* 14.3 "sharing the reasoning does not cause premature convergence", with two and a half of
  its three tests overturned, still labelled "disproved" for a long time
* 14.4 "role does not raise divergence", whose primary metric was the overturned one

So the "reverse index" is made a test: **wherever an invalidated metric is mentioned, the
invalidation note must be mentioned with it**.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: an overturned or downgraded metric -> why it can no longer hold a conclusion up.
#: Keyed by language, because **the docs exist in both and the ledger has to hold in both** —
#: a citation without its retraction is exactly as misleading in English as in Chinese, and
#: the translated copy is the easier one to forget.
RETRACTED = {
    "en": {
        "position drift": "failed calibration: it measures wording, scoring "
        "'same conclusion, reworded' above 'opposite conclusions'",
        "divergence between participants": "the same root cause; retired",
        "laundering_index": "it means the opposite: a high value is moving on to new "
        "questions, not relisting in new wording",
    },
    "zh": {
        "立场漂移": "校准后失效：测的是措辞，「同结论不同措辞」得分高于「结论相反」",
        "参与者间发散度": "同源问题，已废弃",
        "laundering_index": "含义恰好相反：数值高是推进到新问题，不是换措辞重列",
    },
}

#: these phrases count as carrying the invalidation note
DISCLAIMERS = {
    "en": (
        "failed calibration",
        "retired",
        "withdrawn",
        "not measured",
        "no longer rests",
        "discounted",
        "overturned",
        "invalid",
        "void",
        "downgraded",
    ),
    "zh": ("失效", "已废弃", "撤回", "未测量", "不再有依据", "打折", "无效", "推翻"),
}

#: which language each document is written in
DOCS = {
    "DESIGN.md": "en",
    "README.md": "en",
    "DESIGN.zh.md": "zh",
    "README.zh.md": "zh",
}

#: the span of one paragraph — the note counts only if it is in the same paragraph
WINDOW = 900


def _sections_mentioning(text: str, metric: str) -> list[str]:
    out = []
    for m in re.finditer(re.escape(metric), text):
        start = text.rfind("\n\n", 0, max(0, m.start() - WINDOW))
        out.append(text[max(0, start) : m.end() + WINDOW])
    return out


@pytest.mark.parametrize("doc", sorted(DOCS))
def test_a_retracted_metric_is_never_cited_without_its_retraction(doc):
    lang = DOCS[doc]
    text = (ROOT / doc).read_text(encoding="utf-8")

    for metric, why in RETRACTED[lang].items():
        for chunk in _sections_mentioning(text, metric):
            assert any(word in chunk for word in DISCLAIMERS[lang]), (
                f"{doc} mentions '{metric}' without its invalidation note.\n"
                f"That metric {why}. Any conclusion standing on it has to be downgraded or withdrawn.\n"
                f"Context: …{chunk[:200]}…"
            )


def test_the_retracted_list_itself_is_documented():
    """The list of invalidations has to be in the documentation, or this test becomes the only
    thing that knows.
    """
    for doc, lang in sorted(DOCS.items()):
        if not doc.startswith("DESIGN"):
            continue
        design = (ROOT / doc).read_text(encoding="utf-8")
        for metric in RETRACTED[lang]:
            assert metric in design, (
                f"'{metric}' is not in {doc}, so the invalidation record will be lost with the code"
            )
