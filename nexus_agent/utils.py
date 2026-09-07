"""Small utilities shared across the harness."""


def extract_text(content) -> str:
    """Extract plain text from an LLM content object or string."""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "") for block in content if getattr(block, "type", None) == "text"
    ).strip()


def has_tool_use(content) -> bool:
    """Return True if the LLM response contains a tool_use block.

    The loop uses the concrete tool_use block as the continuation signal,
    not just the stop_reason.
    """
    if not isinstance(content, list):
        return False
    return any(getattr(block, "type", None) == "tool_use" for block in content)
