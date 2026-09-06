"""Long-term memory read from MEMORY.md."""

from nexus_agent.config import MEMORY_DIR


def read_memory() -> str:
    """Return the contents of .memory/MEMORY.md if it exists."""
    path = MEMORY_DIR / "MEMORY.md"
    if not path.exists():
        return ""
    return path.read_text()


def update_context(context: dict, _messages: list | None = None) -> dict:
    """Merge MEMORY.md into the context dict used for system prompt assembly."""
    memory = read_memory()
    if memory:
        context["memories"] = memory
    return context
