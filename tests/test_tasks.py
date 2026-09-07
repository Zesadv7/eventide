"""Tests for the task graph."""

from nexus_agent.tasks.store import (
    can_start,
    claim_task,
    complete_task,
    create_task,
    list_tasks,
    load_task,
)


def test_create_and_load_task():
    task = create_task("Test subject", "Test description")
    loaded = load_task(task.id)
    assert loaded.subject == "Test subject"
    assert loaded.status == "pending"


def test_list_tasks():
    create_task("A")
    create_task("B")
    assert len(list_tasks()) >= 2


def test_claim_and_complete():
    task = create_task("Do work")
    assert "Claimed" in claim_task(task.id)
    loaded = load_task(task.id)
    assert loaded.status == "in_progress"
    assert "Completed" in complete_task(task.id)
    assert load_task(task.id).status == "completed"


def test_dependency():
    first = create_task("First")
    second = create_task("Second", blocked_by=[first.id])
    assert not can_start(second.id)
    claim_task(first.id)
    complete_task(first.id)
    assert can_start(second.id)
