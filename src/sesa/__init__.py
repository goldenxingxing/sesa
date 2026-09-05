"""Sesa — a multi-agent deliberation engine.

Open sesame: let different models speak until their views converge.

It brings existing agent CLIs and bare model APIs to the same table, has them argue
under a chosen deliberation protocol until consensus converges — and reports the
disagreements honestly instead of pretending they are settled.
"""

from .types import (
    ConsensusReport,
    Disagreement,
    Outcome,
    ParticipantSpec,
    Result,
    Stance,
    Usage,
)

try:  # The version has exactly one source: the package metadata. A hand-copied second one
    # drifts from pyproject sooner or later — and it did: pyproject already said 0.1.0 while `sesa
    # version` still reported 0.1.0.dev0, so the published package would misreport its own version.
    # Catch only "the package is not installed". A bare `except Exception` would also report
    # corrupted metadata, or a fault in importlib itself, as "running from a source tree" — dressing
    # a real installation problem up as a normal state.
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("sesa")
except PackageNotFoundError:  # fallback for running straight from a source tree, with
    # nothing installed
    __version__ = "0.0.0+source"

__all__ = [
    "ConsensusReport",
    "Disagreement",
    "Outcome",
    "ParticipantSpec",
    "Result",
    "Stance",
    "Usage",
    "__version__",
]
