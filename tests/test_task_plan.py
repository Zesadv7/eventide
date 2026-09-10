"""Production task plans are bounded, durable, identified, and compact in model context."""

from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.task_plan import (
    MAX_CONTENT_CHARS,
    MAX_TODOS,
    normalize_todos,
    plan_update,
    prompt,
)
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def test_task_plan_validation_and_compact_prompt():
    with pytest.raises(ValueError, match="array"):
        normalize_todos("not a list")
    with pytest.raises(ValueError, match="more than"):
        normalize_todos(
            [{"content": str(index), "status": "pending"} for index in range(MAX_TODOS + 1)]
        )
    with pytest.raises(ValueError, match="only id, content and status"):
        normalize_todos([{"content": "x", "status": "pending", "extra": True}])
    with pytest.raises(ValueError, match="must contain content and status"):
        normalize_todos([{"content": "x"}])
    with pytest.raises(ValueError, match="non-empty"):
        normalize_todos([{"content": " ", "status": "pending"}])
    with pytest.raises(ValueError, match="cannot exceed"):
        normalize_todos([{"content": "x" * (MAX_CONTENT_CHARS + 1), "status": "pending"}])
    with pytest.raises(ValueError, match="duplicates"):
        normalize_todos(
            [
                {"content": "same", "status": "pending"},
                {"content": "same", "status": "blocked"},
            ]
        )
    with pytest.raises(ValueError, match="unsupported"):
        normalize_todos([{"content": "x", "status": "unknown"}])
    with pytest.raises(ValueError, match="only one"):
        normalize_todos(
            [
                {"content": "one", "status": "in_progress"},
                {"content": "two", "status": "in_progress"},
            ]
        )

    plan = normalize_todos(
        [
            {"content": "finished detail", "status": "completed"},
            {"content": "current step", "status": "in_progress"},
            {"content": "later step", "status": "pending"},
        ]
    )
    assert [item["id"] for item in plan] == ["t1", "t2", "t3"]
    rendered = prompt(plan)
    assert "1 completed" in rendered
    assert "finished detail" not in rendered
    assert "[in_progress] t2: current step" in rendered
    assert "[pending] t3: later step" in rendered


def test_task_ids_survive_rewording_and_are_never_recycled():
    first, sequence = plan_update(
        [
            {"content": "Inspect inputs", "status": "in_progress"},
            {"content": "Implement change", "status": "pending"},
        ]
    )
    assert first == [
        {"id": "t1", "content": "Inspect inputs", "status": "in_progress"},
        {"id": "t2", "content": "Implement change", "status": "pending"},
    ]
    assert sequence == 3

    second, sequence = plan_update(
        [
            {"id": "t1", "content": "Inspect every input", "status": "completed"},
            {"id": "t2", "content": "Implement change", "status": "in_progress"},
        ],
        first,
        sequence,
    )
    # Rewording keeps the identity the model echoed back.
    assert [(item["id"], item["content"]) for item in second] == [
        ("t1", "Inspect every input"),
        ("t2", "Implement change"),
    ]

    third, sequence = plan_update(
        [{"id": "t2", "content": "Implement change", "status": "completed"}],
        second,
        sequence,
    )
    assert [item["id"] for item in third] == ["t2"]

    # A dropped id is never handed to a later item.
    fourth, sequence = plan_update(
        [{"content": "Brand new step", "status": "pending"}], third, sequence
    )
    assert [item["id"] for item in fourth] == ["t3"]


def test_task_ids_are_recovered_by_content_when_the_model_omits_them():
    first, sequence = plan_update([{"content": "Inspect inputs", "status": "pending"}])
    second, _ = plan_update(
        [{"content": "Inspect inputs", "status": "in_progress"}], first, sequence
    )
    assert [item["id"] for item in second] == ["t1"]

    # A legacy plan carries no identity to recover, so fresh ids are allocated.
    legacy, _ = plan_update(
        [{"content": "Legacy step", "status": "pending"}],
        [{"content": "Known", "status": "pending"}],
    )
    assert [item["id"] for item in legacy] == ["t1"]

    # A sequence embedded in a previous payload is respected even without being passed in.
    resumed, _ = plan_update([{"content": "Later step", "status": "pending"}], first, None)
    assert [item["id"] for item in resumed] == ["t2"]


def test_task_ids_reject_unknown_and_duplicate_references():
    previous, sequence = plan_update([{"content": "Known", "status": "pending"}])
    with pytest.raises(ValueError, match="unknown task id t9"):
        plan_update([{"id": "t9", "content": "Known", "status": "pending"}], previous, sequence)
    with pytest.raises(ValueError, match="invalid task id"):
        plan_update(
            [{"id": "task-1", "content": "Known", "status": "pending"}], previous, sequence
        )
    with pytest.raises(ValueError, match="reuses task id t1"):
        plan_update(
            [
                {"id": "t1", "content": "First", "status": "pending"},
                {"id": "t1", "content": "Second", "status": "pending"},
            ],
            previous,
            sequence,
        )


async def test_task_plan_updates_system_and_elides_historical_arguments(isolated_workspace):
    initial = [
        {"content": "Inspect inputs", "status": "in_progress"},
        {"content": "Implement change", "status": "pending"},
        {"content": "Run validation", "status": "pending"},
    ]
    identified = [
        {"id": "t1", "content": "Inspect inputs", "status": "in_progress"},
        {"id": "t2", "content": "Implement change", "status": "pending"},
        {"id": "t3", "content": "Run validation", "status": "pending"},
    ]
    finished = [{"content": item["content"], "status": "completed"} for item in initial]
    finished_ids = [
        {"id": item["id"], "content": item["content"], "status": "completed"}
        for item in identified
    ]
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                todo_tool = next(
                    tool for tool in request.tools if tool["name"] == "todo_write"
                )
                assert "three or more meaningful steps" in todo_tool["description"]
                assert "Task planning:" not in request.system
                return ModelResponse(
                    tool_calls=(ToolCall("plan", "todo_write", {"todos": initial}),)
                )
            if len(requests) == 2:
                assert "[in_progress] t1: Inspect inputs" in request.system
                plan_call = request.messages[-2]["content"][0]
                assert plan_call["input"] == {"todos": []}
                return ModelResponse(
                    tool_calls=(ToolCall("finish", "todo_write", {"todos": finished}),)
                )
            assert "all 3 items completed" in request.system
            return ModelResponse("done")

    host = RuntimeHost(settings_for(isolated_workspace), Provider())
    try:
        result = await host.run(RunRequest("perform a multi-step change"))
        assert result.status == "completed"
        events = host.store.run_events(result.run_id)
        updates = [event for event in events if event["type"] == "task.plan_updated"]
        assert [event["payload"]["todos"] for event in updates] == [identified, finished_ids]
        # Ids are handed out once and the sequence is carried forward, not recomputed.
        assert [event["payload"]["next_task_seq"] for event in updates] == [4, 4]
        assert [event["payload"]["step"] for event in updates] == [1, 2]
        assert host.session_status(result.session_id)["task_plan"] == finished_ids
        assert next(
            event for event in events if event["type"] == "tool.prepared"
        )["payload"]["readonly"] is True
        assert any(
            event["type"] == "context.trimmed"
            and event["payload"].get("elided_plan_updates")
            for event in events
        )
        stored_call = next(
            message
            for message in host.store.load_messages(result.session_id)
            if message.get("role") == "assistant"
        )["content"][0]
        assert stored_call["input"]["todos"] == initial
    finally:
        await host.close()


async def test_legacy_plan_gains_identity_without_rewriting_history(isolated_workspace):
    legacy = [{"content": "Legacy step", "status": "in_progress"}]
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                # The model echoes the wording it can see; identity is assigned by Runtime.
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "plan",
                            "todo_write",
                            {"todos": [{"content": "Legacy step", "status": "completed"}]},
                        ),
                    )
                )
            return ModelResponse("done")

    host = RuntimeHost(settings_for(isolated_workspace), Provider())
    session = host.create_session()
    host.store.create_run("legacy_run", session)
    host.store.append_fact(
        session,
        "task.plan_updated",
        {"todos": legacy},
        run_id="legacy_run",
        author="model",
    )
    try:
        result = await host.run(RunRequest("continue legacy work", session))
        assert result.status == "completed"
        events = host.store.session_events(session)
        recorded = next(
            event
            for event in events
            if event["type"] == "task.plan_updated" and event["run_id"] == "legacy_run"
        )
        # The historical event is never rewritten in place.
        assert recorded["payload"] == {"todos": legacy}
        latest = [event for event in events if event["type"] == "task.plan_updated"][-1]
        assert latest["payload"]["todos"] == [
            {"id": "t1", "content": "Legacy step", "status": "completed"}
        ]
    finally:
        await host.close()


async def test_task_plan_survives_continue_without_repeating_user_intent(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(
        settings_for(repo),
        state_dir=isolated_workspace / "state",
        max_steps=1,
    )
    plan = [
        {"content": "Inspect", "status": "completed"},
        {"content": "Implement", "status": "in_progress"},
        {"content": "Verify", "status": "pending"},
    ]
    identified = [
        {"id": "t1", "content": "Inspect", "status": "completed"},
        {"id": "t2", "content": "Implement", "status": "in_progress"},
        {"id": "t3", "content": "Verify", "status": "pending"},
    ]
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(
                    tool_calls=(ToolCall("plan", "todo_write", {"todos": plan}),)
                )
            assert "1 completed" in request.system
            assert "[in_progress] t2: Implement" in request.system
            return ModelResponse("continued")

    host = RuntimeHost(settings, Provider())
    try:
        first = await host.run(RunRequest("large task"))
        assert first.status == "interrupted"
        assert host.session_status(first.session_id)["task_plan"] == identified

        host.settings = replace(host.settings, max_steps=2)
        continued = await host.continue_session(first.session_id)

        assert continued.status == "completed"
        assert continued.output == "continued"
        # Identity is unchanged by the park/continue cycle.
        assert host.session_status(first.session_id)["task_plan"] == identified
        assert len(
            [
                event
                for event in host.store.session_events(first.session_id)
                if event["type"] == "message.user"
            ]
        ) == 1
    finally:
        await host.close()
