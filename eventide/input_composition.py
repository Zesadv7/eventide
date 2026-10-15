"""Numeric composition of the final provider-neutral request's JSON characters.

These are serialized Unicode character counts, not Token counts or causal cost
attribution. Only tool pairs present in this exact request establish provenance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from eventide.models import ModelRequest

METHOD = "serialized_unicode_characters_v1"
COMPONENTS = (
    "instructions", "history", "tool_catalog", "skills", "retrieval", "tool_results", "envelope",
)
_SKILL_TOOLS = {"load_skill", "read_skill_resource"}


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def measure_input_composition(request: ModelRequest) -> dict[str, Any]:
    """Partition disjoint serialized values; remaining JSON structure is envelope."""
    components = dict.fromkeys(COMPONENTS, 0)
    components["instructions"] = _size(request.system)
    components["tool_catalog"] = _size(request.tools)
    total = _size({"system": request.system, "messages": request.messages, "tools": request.tools})
    # Reused IDs are ambiguous even when the names agree. Never infer a tool from
    # arbitrary result names, nested JSON text, compacted summaries or future calls.
    calls: dict[str, tuple[int, str] | None] = {}
    for index, message in enumerate(request.messages):
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id, name = block.get("id"), block.get("name")
            if isinstance(call_id, str) and call_id:
                calls[call_id] = (
                    (index, name) if call_id not in calls and isinstance(name, str) and name
                    else None
                )

    for index, message in enumerate(request.messages):
        # Provider extensions may carry opaque reasoning outside content. Count
        # their values as history, not as mere message-envelope punctuation.
        components["history"] += sum(
            _size(value) for key, value in message.items() if key not in {"role", "content"}
        )
        if "content" not in message:
            continue
        content = message["content"]
        if not isinstance(content, list):
            components["history"] += _size(content)
            continue
        for block in content:
            component = "history"
            if isinstance(block, dict) and block.get("type") == "tool_result":
                component = "tool_results"
                call_id = block.get("tool_use_id")
                call = calls.get(call_id) if isinstance(call_id, str) else None
                if message.get("role") == "user" and call is not None and call[0] < index:
                    if call[1] in _SKILL_TOOLS:
                        component = "skills"
                    elif call[1] == "search_knowledge":
                        component = "retrieval"
            components[component] += _size(block)
    components["envelope"] = total - sum(components.values())
    return {
        "method": METHOD, "unit": "characters", "total_characters": total,
        "components": components,
    }


def validate_input_composition(value: Any) -> dict[str, Any] | None:
    """Reject malformed/unknown historical structures without leaking arbitrary fields."""
    if not isinstance(value, Mapping) or set(value) != {
        "method", "unit", "total_characters", "components",
    }:
        return None
    if value["method"] != METHOD or value["unit"] != "characters":
        return None
    counts = value["components"]
    total = value["total_characters"]
    if not isinstance(counts, Mapping) or set(counts) != set(COMPONENTS):
        return None
    if any(type(number) is not int or number < 0 for number in (total, *counts.values())):
        return None
    if sum(counts.values()) != total:
        return None
    return {
        "method": METHOD, "unit": "characters", "total_characters": total,
        "components": {key: counts[key] for key in COMPONENTS},
    }
