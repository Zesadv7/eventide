"""Completion claims are the model's; task evidence is what the Runtime observed."""

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.task_plan import TOOL, prompt
from tests.test_runtime import settings_for


def start_plan(content="Inspect"):
    return ToolCall(
        "plan", "todo_write", {"todos": [{"content": content, "status": "in_progress"}]}
    )


def read_call(name="shared", path="data"):
    return ToolCall(name, "read_file", {"path": path})


def completed_plan(summary="Read the data file."):
    return {
        "todos": [
            {
                "id": "t1",
                "content": "Inspect",
                "status": "completed",
                "summary": summary,
            }
        ]
    }


class Steps:
    """Dispatch on request order so every call id stays unique and the run ends."""

    def __init__(self, script):
        self.script = script
        self.count = 0

    async def complete(self, request):
        self.count += 1
        index = self.count - 1
        if index < len(self.script):
            return self.script[index](self.count)
        return ModelResponse("finished")


def test_prompt_stays_bounded_as_completions_accumulate():
    def plan(count):
        todos = [
            {
                "id": f"t{index}",
                "content": f"step {index}",
                "status": "completed",
                "summary": "s" * 100,
            }
            for index in range(1, count + 1)
        ]
        todos.append({"id": "tx", "content": "current", "status": "in_progress"})
        return todos

    # Only the newest claim is injected, so the plan block does not grow with history.
    assert len(prompt(plan(15))) - len(prompt(plan(3))) < 40
    assert prompt(plan(15)).count("s" * 100) == 1


def test_task_plan_wording_never_claims_verification():
    description = TOOL["description"].lower()
    for claim in ("verified", "passed", "tested"):
        assert claim not in description


async def test_completed_task_evidence_comes_from_tool_events(isolated_workspace):
    (isolated_workspace / "data").write_text("hello", encoding="utf-8")
    provider = Steps(
        [
            lambda _: ModelResponse(tool_calls=(start_plan(),)),
            lambda _: ModelResponse(tool_calls=(read_call("r1"),)),
            lambda _: ModelResponse(tool_calls=(ToolCall("g1", "glob", {"pattern": "*"}),)),
            lambda n: ModelResponse(
                tool_calls=(ToolCall(f"done{n}", "todo_write", completed_plan()),)
            ),
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("inspect"))
        assert result.status == "completed"
        task = host.session_status(result.session_id)["task_state"]["tasks"][0]
        assert task["summary"] == "Read the data file."
        # The plan calls themselves are bookkeeping, not task evidence.
        assert [entry["name"] for entry in task["evidence"]] == ["read_file", "glob"]
        assert [entry["call_id"] for entry in task["evidence"]] == ["r1", "g1"]
        assert all(entry["run_id"] == result.run_id for entry in task["evidence"])
        assert all(entry["is_error"] is False for entry in task["evidence"])
        assert task["evidence_omitted"] == 0
    finally:
        await host.close()


async def test_failed_calls_are_recorded_as_evidence(isolated_workspace):
    provider = Steps(
        [
            lambda _: ModelResponse(tool_calls=(start_plan(),)),
            lambda _: ModelResponse(tool_calls=(read_call("bad", "missing"),)),
            lambda n: ModelResponse(
                tool_calls=(
                    ToolCall(
                        f"done{n}",
                        "todo_write",
                        completed_plan(summary="The file was not there."),
                    ),
                )
            ),
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("inspect"))
        assert result.status == "completed"
        task = host.session_status(result.session_id)["task_state"]["tasks"][0]
        # A failed call is still evidence of what happened, not proof of success.
        assert [entry["is_error"] for entry in task["evidence"]] == [True]
        assert task["evidence"][0]["call_id"] == "bad"
    finally:
        await host.close()


async def test_task_evidence_is_capped_and_reports_what_it_dropped(isolated_workspace):
    globs = [
        (lambda n, index=index: ModelResponse(
            tool_calls=(ToolCall(f"g{index}", "glob", {"pattern": "*"}),)
        ))
        for index in range(10)
    ]
    provider = Steps(
        [
            lambda _: ModelResponse(tool_calls=(start_plan("Look around"),)),
            *globs,
            lambda n: ModelResponse(
                tool_calls=(
                    ToolCall(
                        f"done{n}",
                        "todo_write",
                        completed_plan(summary="Searched the workspace."),
                    ),
                )
            ),
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("look around"))
        assert result.status == "completed"
        task = host.session_status(result.session_id)["task_state"]["tasks"][0]
        assert len(task["evidence"]) == 8
        assert task["evidence_omitted"] == 2
        # The newest calls are the ones kept.
        assert task["evidence"][-1]["call_id"] == "g9"
    finally:
        await host.close()


async def test_task_evidence_never_crosses_sessions(isolated_workspace):
    (isolated_workspace / "data").write_text("hello", encoding="utf-8")
    # Each session consumes four requests, and both sessions use the same call id.
    provider = Steps(
        [
            lambda _: ModelResponse(tool_calls=(start_plan(),)),
            lambda _: ModelResponse(tool_calls=(read_call(),)),
            lambda n: ModelResponse(
                tool_calls=(
                    ToolCall(f"done{n}", "todo_write", completed_plan(summary="Read it.")),
                )
            ),
            lambda _: ModelResponse("finished"),
            lambda _: ModelResponse(tool_calls=(start_plan(),)),
            lambda _: ModelResponse(tool_calls=(read_call(),)),
            lambda n: ModelResponse(
                tool_calls=(
                    ToolCall(f"done{n}", "todo_write", completed_plan(summary="Read it.")),
                )
            ),
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        first = await host.run(RunRequest("first"))
        second = await host.run(RunRequest("second"))
        assert first.session_id != second.session_id
        for result in (first, second):
            task = host.session_status(result.session_id)["task_state"]["tasks"][0]
            assert [entry["call_id"] for entry in task["evidence"]] == ["shared"]
            # The same call id in another session is a different call.
            assert task["evidence"][0]["run_id"] == result.run_id
    finally:
        await host.close()
