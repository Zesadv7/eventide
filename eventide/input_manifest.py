"""Body-free provenance of the request handed to a provider adapter."""
from __future__ import annotations

from typing import Any

from eventide.input_composition import measure_input_composition
from eventide.models import ModelRequest
from eventide.workspace import digest


def input_manifest(request: ModelRequest) -> dict[str, Any]:
    """Keep identities and counts, never duplicate source, prompts, or arguments."""
    results: list[str] = []
    for message in request.messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            results.extend(
                block["tool_use_id"] for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
                and isinstance(block.get("tool_use_id"), str)
            )
    return {
        "manifest_version": 1,
        "system_hash": digest(request.system), "messages_hash": digest(request.messages),
        "tools_hash": digest(request.tools),
        "message_count": len(request.messages), "tool_count": len(request.tools),
        "tool_names": [tool["name"] for tool in request.tools],
        "tool_result_ids": results,
        "composition": measure_input_composition(request),
    }
