"""The data abilities, exposed as tools the model can call.

The same handlers the legacy engine reaches through regex parsers are offered
here as tools, so the model can decide what to look up and COMBINE facts across
several lookups — which the deterministic chain structurally cannot do, because
the first matching parser returns immediately.

Safety is unchanged: the triage floor and the emergency path run before any of
this, and the output validator plus the numeric-fidelity guard run after.
"""

from __future__ import annotations

from app.chat.tools.definitions import TOOL_SPECS
from app.chat.tools.registry import EXECUTORS, execute_tool

__all__ = ["EXECUTORS", "TOOL_SPECS", "execute_tool"]
