"""Production task plans are bounded, durable, and compact in model context."""

from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.task_plan import MAX_CONTENT_CHARS, MAX_TODOS, normalize_todos, prompt
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def test_task_plan_validation_and_compact_prompt():
    with pytest.raises(ValueError, match="array"):
        normalize_todos("not a list")
    with pytest.raises(ValueError, match="more than"):
        normalize_todos(
            [{"content": str(index), "status": "pending"} for index in range(MAX_TODOS + 1)]
        )
    with pytest.raises(ValueError, match="only content and status"):
        normalize_todos([{"content": "x", "status": "pending", "extra": True}])
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
    rendered = prompt(plan)
    assert "1 completed" in rendered
    assert "finished detail" not in rendered
    assert "[in_progress] current step" in rendered
    assert "[pending] later step" in rendered


async def test_task_plan_updates_system_and_elides_historical_arguments(isolated_workspace):
    initial = [
        {"content": "Inspect inputs", "status": "in_progress"},
        {"content": "Implement change", "status": "pending"},
        {"content": "Run validation", "status": "pending"},
    ]
    finished = [
        {"content": item["content"], "status": "completed"} for item in initial
    ]
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                assert "todo_write" in {tool["name"] for tool in request.tools}
                assert "three or more meaningful steps" in request.system
                return ModelResponse(
                    tool_calls=(ToolCall("plan", "todo_write", {"todos": initial}),)
                )
            if len(requests) == 2:
                assert "[in_progress] Inspect inputs" in request.system
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
        assert [event["payload"]["todos"] for event in updates] == [initial, finished]
        assert host.session_status(result.session_id)["task_plan"] == finished
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
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(
                    tool_calls=(ToolCall("plan", "todo_write", {"todos": plan}),)
                )
            assert "1 completed" in request.system
            assert "[in_progress] Implement" in request.system
            return ModelResponse("continued")

    host = RuntimeHost(settings, Provider())
    try:
        first = await host.run(RunRequest("large task"))
        assert first.status == "interrupted"
        assert host.session_status(first.session_id)["task_plan"] == plan

        host.settings = replace(host.settings, max_steps=2)
        continued = await host.continue_session(first.session_id)

        assert continued.status == "completed"
        assert continued.output == "continued"
        assert len(
            [
                event
                for event in host.store.session_events(first.session_id)
                if event["type"] == "message.user"
            ]
        ) == 1
    finally:
        await host.close()
