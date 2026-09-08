"""Compatibility exports for event-backed storage and redaction."""

from nexus_agent.normalization import redact
from nexus_agent.store import RuntimeStore

TraceStore = RuntimeStore

__all__ = ["RuntimeStore", "TraceStore", "redact"]
