"""Tests for provider-neutral OpenAI conversion."""

import json

from eventide.providers.openai_compatible import convert_messages, convert_tools


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


def test_openai_message_conversion_supports_images():
    converted = convert_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image", "media_type": "image/png", "data": "aW1n"},
                ],
            }
        ],
        "system",
    )
    assert converted[1]["content"] == [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}},
    ]
