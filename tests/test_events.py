"""Semantic constraints on the event stream. It is the only source of truth, and what it cannot
say, nobody can say.
"""

from __future__ import annotations

from sesa import events as ev


def test_files_applied_flags_silently_dropped_code():
    """Code written without a path marked ≠ nothing needed changing this round."""
    talked_only = ev.FilesApplied(round=0, participant="bob", files=[], rejected=[], fences_seen=25)
    assert talked_only.silently_dropped

    nothing_to_do = ev.FilesApplied(
        round=1, participant="bob", files=[], rejected=[], fences_seen=0
    )
    assert not nothing_to_do.silently_dropped

    delivered = ev.FilesApplied(
        round=0, participant="alice", files=["semver.py"], rejected=[], fences_seen=3
    )
    assert not delivered.silently_dropped
