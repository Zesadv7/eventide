"""v0.2-compatible entry point backed by the Workspace RuntimeHost."""

from pathlib import Path

from eventide.host import EventSink, RuntimeHost

__all__ = ["AgentRuntime", "EventSink", "RuntimeHost"]


class AgentRuntime(RuntimeHost):
    """Compatibility facade: the default workspace comes from Settings.workdir."""

    @property
    def state_dir(self) -> Path:
        """State root for runtime-owned session files such as attachments."""
        return self.settings.state_dir
