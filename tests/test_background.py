"""Tests for background task dispatch."""

import time
from types import SimpleNamespace

from nexus_agent.scheduling.background import (
    collect_background_results,
    is_slow_operation,
    should_run_background,
    start_background_task,
)


def test_is_slow_operation():
    assert is_slow_operation("bash", {"command": "npm install"})
    assert not is_slow_operation("bash", {"command": "echo hi"})
    assert not is_slow_operation("read_file", {"path": "x"})


def test_should_run_background_flag():
    assert should_run_background("bash", {"command": "echo", "run_in_background": True})


def test_background_task_lifecycle(monkeypatch):
    monkeypatch.setattr("nexus_agent.scheduling.background.trigger_hooks", lambda *a, **k: None)
    block = SimpleNamespace(id="bg_block", name="bash", input={"command": "echo ok"})
    handlers = {"bash": lambda command: "ok"}
    bg_id = start_background_task(block, handlers)
    # Wait briefly for daemon thread.
    for _ in range(20):
        notifications = collect_background_results()
        if notifications:
            break
        time.sleep(0.05)
    assert any(bg_id in note for note in notifications)
