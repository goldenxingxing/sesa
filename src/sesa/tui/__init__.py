"""The Textual front end.

**The engine knows nothing about terminals** (DESIGN §2) — this is just one more
consumer of the event stream, a peer of the CLI, the SDK and MCP. Pushing rendering
into the engine would force anyone embedding it to strip that out again.
"""

from .app import run_tui

__all__ = ["run_tui"]
