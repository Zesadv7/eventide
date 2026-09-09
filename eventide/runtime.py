"""v0.2-compatible entry point backed by the Workspace RuntimeHost."""

from eventide.host import EventSink, RuntimeHost

__all__ = ["AgentRuntime", "EventSink", "RuntimeHost"]


class AgentRuntime(RuntimeHost):
    """Compatibility facade: the default workspace comes from Settings.workdir."""
