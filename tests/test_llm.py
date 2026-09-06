"""Tests for LLM retry and recovery."""

import pytest

from nexus_agent.llm import RecoveryState, is_prompt_too_long_error, with_retry


def test_retry_429_then_success(monkeypatch):
    state = RecoveryState()
    monkeypatch.setattr("nexus_agent.llm.time.sleep", lambda _: None)

    calls = {"count": 0}

    class RatelimitError(Exception):
        pass

    def fn():
        calls["count"] += 1
        if calls["count"] < 2:
            raise RatelimitError("429")
        return "ok"

    assert with_retry(fn, state) == "ok"
    assert calls["count"] == 2


def test_retry_529_switches_fallback(monkeypatch):
    monkeypatch.setenv("FALLBACK_MODEL_ID", "fallback-model")
    monkeypatch.setattr("nexus_agent.llm.time.sleep", lambda _: None)

    state = RecoveryState()

    def fn():
        raise Exception("overloaded: 529")

    with pytest.raises(RuntimeError):
        with_retry(fn, state)
    assert state.current_model == "fallback-model"


def test_is_prompt_too_long_error():
    assert is_prompt_too_long_error(Exception("prompt too long"))
    assert is_prompt_too_long_error(Exception("context_length_exceeded"))
    assert not is_prompt_too_long_error(Exception("unknown error"))
