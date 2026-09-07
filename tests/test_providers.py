"""Tests for provider-neutral OpenAI conversion."""

import json

from nexus_agent.providers.openai_compatible import convert_messages, convert_tools


def test_openai_tool_conversion():
    converted = convert_tools(
        [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}]
    )
    assert converted[0]["function"]["name"] == "read_file"


def test_openai_message_conversion_preserves_tool_roundtrip():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}],
        },
    ]
    converted = convert_messages(messages, "system")
    assert json.loads(converted[1]["tool_calls"][0]["function"]["arguments"]) == {"path": "x"}
    assert converted[2] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
