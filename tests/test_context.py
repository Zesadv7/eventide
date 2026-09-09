"""Tests for context compaction."""

from eventide.context import (
    estimate_size,
    micro_compact,
    snip_compact,
    tool_result_budget,
)


def test_estimate_size():
    messages = [{"role": "user", "content": "hi"}]
    assert estimate_size(messages) > 0


def test_snip_compact():
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(60)]
    compacted = snip_compact(messages, max_messages=50)
    assert len(compacted) < 60
    assert any("[snipped" in str(m.get("content", "")) for m in compacted)


def test_micro_compact():
    big = "x" * 500
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": big},
                {"type": "tool_result", "tool_use_id": "2", "content": big},
                {"type": "tool_result", "tool_use_id": "3", "content": big},
                {"type": "tool_result", "tool_use_id": "4", "content": big},
            ],
        },
    ]
    compacted = micro_compact(messages)
    # The oldest results should be compacted, recent ones kept.
    assert "compacted" in str(compacted[0]["content"][0]["content"])


def test_tool_result_budget_persists_large_output():
    huge = "x" * 500_000
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "big", "content": huge},
            ],
        },
    ]
    compacted = tool_result_budget(messages, max_bytes=100_000)
    content = compacted[0]["content"][0]["content"]
    assert "persisted-output" in content or len(content) < len(huge)
