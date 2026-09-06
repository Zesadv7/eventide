"""Tests for the main agent loop with a fake Anthropic client."""

from types import SimpleNamespace

import pytest

from nexus_agent import agent as agent_module


def _make_text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_tool_block(name: str, tool_id: str, input_data: dict):
    return SimpleNamespace(type="tool_use", name=name, id=tool_id, input=input_data)


def _make_response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def test_agent_loop_no_tool_use(monkeypatch):
    calls = []

    def fake_create(*, model, system, messages, tools, max_tokens):
        calls.append((model, messages, tools))
        return _make_response([_make_text_block("Hello")])

    monkeypatch.setattr("nexus_agent.agent.client.messages.create", fake_create)
    history = []
    context = {"workdir": "."}
    agent_module.agent_loop(history, context)
    assert len(calls) == 1
    assert any(t["name"] == "bash" for t in calls[0][2])


def test_agent_loop_tool_use_then_stop(monkeypatch):
    responses = [
        _make_response([_make_tool_block("bash", "t1", {"command": "echo hi"})]),
        _make_response([_make_text_block("Done")]),
    ]

    def fake_create(*, model, system, messages, tools, max_tokens):
        return responses.pop(0)

    monkeypatch.setattr("nexus_agent.agent.client.messages.create", fake_create)
    history = []
    context = {"workdir": "."}
    agent_module.agent_loop(history, context)
    assert len(history) == 3  # assistant(tool)/user(result)/assistant(text)
