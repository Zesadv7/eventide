"""Task dependencies share the production plan's identity, validation and Event Log."""

from copy import deepcopy
from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.projections import TaskPlanProjection
from eventide.providers import ScriptedProvider
from eventide.task_plan import MAX_TODOS, TOOL, plan_update, prompt
from tests.test_host import make_repo
from tests.test_runtime import settings_for
from tests.test_task_plan import plan_event


def initial_plan():
    return [
        {"content": "Inspect", "status": "in_progress"},
        {"content": "Implement", "status": "pending", "depends_on": ["t1"]},
    ]


def test_new_plan_dependencies_resolve_after_all_ids_are_allocated():
    plan, sequence = plan_update(
        [
            {"content": "Finish", "status": "pending", "depends_on": ["t3", "t2"]},
            {"content": "Implement", "status": "blocked", "depends_on": ["t3"]},
            {"content": "Inspect", "status": "in_progress"},
        ]
    )
    assert sequence == 4
    assert [todo["id"] for todo in plan] == ["t1", "t2", "t3"]
    assert plan[0]["depends_on"] == ["t3", "t2"]
    assert "t1: Finish (depends on t3, t2)" in prompt(plan)
    assert "t2: Implement (depends on t3)" in prompt(plan)
    assert (
        TOOL["input_schema"]["properties"]["todos"]["items"]["properties"]["depends_on"]["maxItems"]
        == MAX_TODOS
    )


@pytest.mark.parametrize(
    ("dependencies", "error"),
    [
        ("t2", "must be an array"),
        (None, "must be an array"),
        ([None], "invalid dependency id"),
        ([1], "invalid dependency id"),
        (["t0"], "invalid dependency id"),
        (["task2"], "invalid dependency id"),
        (["t1"], "cannot depend on itself"),
        (["t2", "t2"], "duplicate dependency"),
        (["t9"], "unknown dependency t9"),
        (["t2"] * (MAX_TODOS + 1), "more than 20 dependencies"),
    ],
)
def test_dependencies_reject_invalid_references_without_mutating_inputs(dependencies, error):
    previous, sequence = plan_update(initial_plan())
    candidate = [
        {"id": "t1", "content": "Inspect", "status": "pending", "depends_on": dependencies},
        {"id": "t2", "content": "Implement", "status": "pending", "depends_on": []},
    ]
    before_previous, before_candidate = deepcopy(previous), deepcopy(candidate)
    with pytest.raises(ValueError, match=error):
        plan_update(candidate, previous, sequence)
    assert previous == before_previous
    assert candidate == before_candidate
    # A rejected update cannot consume an id from the caller's persisted sequence.
    following, next_sequence = plan_update(
        [*previous, {"content": "Verify", "status": "pending"}], previous, sequence
    )
    assert following[-1]["id"] == "t3"
    assert next_sequence == 4


@pytest.mark.parametrize("length", [2, MAX_TODOS])
def test_dependency_cycles_are_rejected_even_when_every_task_is_pending(length):
    with pytest.raises(ValueError, match="cycle"):
        plan_update(
            [
                {
                    "content": f"step {index}",
                    "status": "pending",
                    "depends_on": [f"t{index % length + 1}"],
                }
                for index in range(1, length + 1)
            ]
        )


def test_bounded_dependency_chain_and_independent_plan_remain_valid():
    chain, _ = plan_update(
        [
            {
                "content": f"step {index}",
                "status": "pending",
                "depends_on": [f"t{index - 1}"] if index > 1 else [],
            }
            for index in range(1, MAX_TODOS + 1)
        ]
    )
    assert len(chain) == MAX_TODOS
    old_style, _ = plan_update([{"content": "Standalone", "status": "in_progress"}])
    assert old_style == [{"id": "t1", "content": "Standalone", "status": "in_progress"}]


@pytest.mark.parametrize(
    ("status", "prerequisite_status"),
    [
        ("in_progress", "pending"),
        ("in_progress", "blocked"),
        ("completed", "pending"),
        ("completed", "blocked"),
        ("completed", "in_progress"),
    ],
)
def test_starting_or_completing_requires_completed_dependencies(status, prerequisite_status):
    with pytest.raises(ValueError, match="requires completed dependencies: t1"):
        plan_update(
            [
                {"content": "Inspect", "status": prerequisite_status},
                {
                    "content": "Implement",
                    "status": status,
                    "depends_on": ["t1"],
                    **({"summary": "Made changes."} if status == "completed" else {}),
                },
            ]
        )


def test_dependencies_are_preserved_on_rewording_and_can_be_explicitly_cleared():
    previous, sequence = plan_update(initial_plan())
    reworded = [
        {"id": "t1", "content": "Inspect carefully", "status": "in_progress"},
        {"id": "t2", "content": "Implement safely", "status": "blocked"},
    ]
    updated, following = plan_update(reworded, previous, sequence)
    assert updated[1]["depends_on"] == ["t1"]
    assert following == sequence
    assert previous[1]["depends_on"] == ["t1"]
    updated[1]["depends_on"].clear()
    assert previous[1]["depends_on"] == ["t1"]

    reworded[1]["depends_on"] = []
    cleared, _ = plan_update(reworded, previous, sequence)
    assert cleared[1]["depends_on"] == []
    # Same-content identity recovery also retains the dependency when id is omitted.
    recovered, _ = plan_update(initial_plan(), previous, sequence)
    recovered, _ = plan_update(
        [
            {"content": "Inspect", "status": "pending"},
            {"content": "Implement", "status": "blocked"},
        ],
        recovered,
        sequence,
    )
    assert recovered[1]["id"] == "t2"
    assert recovered[1]["depends_on"] == ["t1"]


def test_removing_a_referenced_task_requires_removing_the_dependency():
    previous, sequence = plan_update(initial_plan())
    with pytest.raises(ValueError, match="unknown dependency t1"):
        plan_update([{"id": "t2", "content": "Implement", "status": "pending"}], previous, sequence)
    plan, _ = plan_update(
        [{"id": "t2", "content": "Implement", "status": "pending", "depends_on": []}],
        previous,
        sequence,
    )
    assert plan[0]["depends_on"] == []


def test_dependencies_and_completion_are_validated_in_one_atomic_snapshot():
    previous, sequence = plan_update(initial_plan())
    plan, _ = plan_update(
        [
            {"id": "t1", "content": "Inspect", "status": "completed", "summary": "Read."},
            {"id": "t2", "content": "Implement", "status": "completed", "summary": "Edited."},
        ],
        previous,
        sequence,
    )
    assert [todo["status"] for todo in plan] == ["completed", "completed"]
    assert plan[1]["depends_on"] == ["t1"]
    with pytest.raises(ValueError, match="requires completed dependencies"):
        plan_update(
            [{"id": "t1", "content": "Inspect", "status": "pending"}, plan[1]],
            plan,
            sequence,
        )


def test_projection_exposes_dependencies_without_changing_legacy_events():
    old = plan_event(1, [{"id": "t1", "content": "Inspect", "status": "completed"}])
    new = plan_event(
        2,
        [
            {"id": "t1", "content": "Inspect", "status": "completed"},
            {"id": "t2", "content": "Implement", "status": "in_progress", "depends_on": ["t1"]},
        ],
    )
    before = deepcopy([old, new])
    projected = TaskPlanProjection.project([old, new])
    assert [task["depends_on"] for task in projected["tasks"]] == [[], ["t1"]]
    projected["tasks"][1]["depends_on"].clear()
    assert [old, new] == before


async def test_rejected_dependency_change_does_not_append_a_plan_event(isolated_workspace):
    previous, _ = plan_update(initial_plan())
    candidate = [
        {"id": "t1", "content": "Inspect", "status": "pending", "depends_on": ["t2"]},
        {"id": "t2", "content": "Implement", "status": "pending"},
    ]
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {"id": "initial", "name": "todo_write", "arguments": {"todos": initial_plan()}}
                ]
            },
            {
                "tool_calls": [
                    {"id": "cycle", "name": "todo_write", "arguments": {"todos": candidate}}
                ]
            },
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("Plan implementation"))
        assert result.status == "completed"
        assert host.store.task_plan(result.session_id) == previous
        events = host.store.run_events(result.run_id)
        assert len([event for event in events if event["type"] == "task.plan_updated"]) == 1
        rejected = next(
            event
            for event in events
            if event["type"] == "tool.completed" and event["payload"]["call_id"] == "cycle"
        )
        assert rejected["payload"]["is_error"] is True
        assert "cycle" in rejected["payload"]["content"]
    finally:
        await host.close()


async def test_dependencies_survive_restart_and_continue(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state", max_steps=1)
    host = RuntimeHost(
        settings,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {"id": "plan", "name": "todo_write", "arguments": {"todos": initial_plan()}}
                    ]
                }
            ]
        ),
    )
    try:
        parked = await host.run(RunRequest("Inspect then implement"))
        assert parked.status == "interrupted"
    finally:
        await host.close()

    class Provider:
        def __init__(self):
            self.count = 0

        async def complete(self, request):
            self.count += 1
            if self.count == 1:
                assert "t2: Implement (depends on t1)" in request.system
                todos = [
                    {"id": "t1", "content": "Inspect", "status": "completed", "summary": "Read."},
                    {"id": "t2", "content": "Implement", "status": "in_progress"},
                ]
            elif self.count == 2:
                assert "[in_progress] t2: Implement (depends on t1)" in request.system
                todos = [
                    {"id": "t1", "content": "Inspect", "status": "completed"},
                    {
                        "id": "t2",
                        "content": "Implement",
                        "status": "completed",
                        "summary": "Edited.",
                    },
                ]
            else:
                return ModelResponse("finished")
            return ModelResponse(
                tool_calls=(ToolCall(f"update{self.count}", "todo_write", {"todos": todos}),)
            )

    reopened = RuntimeHost(replace(settings, max_steps=3), Provider())
    try:
        assert reopened.session_status(parked.session_id)["task_plan"][1]["depends_on"] == ["t1"]
        result = await reopened.continue_session(parked.session_id)
        assert result.status == "completed"
        state = reopened.session_status(parked.session_id)
        assert state["task_plan"][1]["depends_on"] == ["t1"]
        assert state["active_task_id"] is None
        assert [task["depends_on"] for task in state["task_state"]["tasks"]] == [[], ["t1"]]
        events = reopened.store.session_events(parked.session_id)
        assert len([event for event in events if event["type"] == "task.plan_updated"]) == 3
        assert len([event for event in events if event["type"] == "message.user"]) == 1
    finally:
        await reopened.close()
