"""Compatibility exports for event-backed storage and redaction."""

from eventide.normalization import redact
from eventide.store import RuntimeStore

TraceStore = RuntimeStore

__all__ = ["RuntimeStore", "TraceStore", "redact"]
