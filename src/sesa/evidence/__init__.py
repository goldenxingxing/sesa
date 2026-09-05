"""Evidence: objective results the engine executed itself.

**Evidence can be wrong too** (see DESIGN.md §6.2), so everything in this layer is
built around one thing: **keeping the source and the scope of a piece of evidence
checkable**, rather than treating an exit code as truth.
"""

from .runner import CrossTestMatrix, EvidenceRunner, run_verify

__all__ = ["CrossTestMatrix", "EvidenceRunner", "run_verify"]
