"""Real Host failure windows stop waste without replaying any tool side effects."""

import asyncio

import pytest

from eventide.budgets import BudgetExceeded
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def call(number, name="bash", **arguments):
    return {"id": f"call-{number}", "name": name, "arguments": arguments or {"command": "false"}}


def response(*calls):
    return {"tool_calls": list(calls), "usage": {"input_tokens": 5, "output_tokens": 2}}


def loop_events(host, run_id):
    return [
        event
        for event in host.store.run_events(run_id)
        if event["type"] == "tool.failure_loop_detected"
    ]


async def test_three_real_shell_failures_park_and_abandon_unprepared_batch_tail(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider(
        [
            response(
                call(1),
                call(2),
                call(3),
                call(4, "write_file", path="must-not-exist", content="bad"),
            ),
            {"text": "must not claim success"},
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)
    try:
        result = await host.run(RunRequest("repair", mode="agent"))
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "repeated_tool_failure"
        assert result.steps == 1 and result.tool_calls == 3
        assert len(provider.requests) == 1
        assert not (repo / "must-not-exist").exists()
        events = host.store.run_events(result.run_id)
        actual = [event for event in events if event["type"] == "tool.completed"]
        assert len(actual) == 3
        assert all(event["payload"]["metadata"]["exit_code"] == 1 for event in actual)
        assert {
            event["payload"]["call_id"] for event in events if event["type"] == "tool.prepared"
        } == {"call-1", "call-2", "call-3"}
        abandoned = next(event["payload"] for event in events if event["type"] == "tool.abandoned")
        assert abandoned["call_id"] == "call-4" and abandoned["dispatched"] is False
        assert abandoned["reason"] == "repeated_tool_failure"
        assert len(host.store.load_messages(result.session_id)[-1]["content"]) == 4
        evidence = loop_events(host, result.run_id)[0]["payload"]
        assert evidence["matching_failure_window"] == evidence["threshold"] == 3
        assert evidence["continued_failure_chain"] is False
        assert evidence["calls"] == [
            {"run_id": result.run_id, "call_id": f"call-{i}"} for i in range(1, 4)
        ]
        ledger = build_usage_ledger(events)
        assert ledger["attempt_count"] == 1 and ledger["unknown_attempts"] == 0
        assert ledger["reported_totals"] == {"input_tokens": 5, "output_tokens": 2}
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["arguments", "source", "error"])
async def test_real_progress_or_a_changed_failure_does_not_park(isolated_workspace, change):
    repo = make_repo(isolated_workspace / "repo")
    script = [
        response(
            call(
                i,
                command=(
                    "printf x >> progress.txt; exit 1"
                    if change == "source"
                    else f"false # attempt {i}"
                    if change == "arguments"
                    else "false"
                ),
            )
        )
        for i in range(1, 5)
    ] + [{"text": "finished"}]
    provider = ScriptedProvider(script)
    host = RuntimeHost(settings_for(repo), provider)
    if change == "error":
        from eventide.models import CommandResult

        class Executor:
            count = 0

            async def execute(self, command, cwd):
                self.count += 1
                return CommandResult(1, stderr=f"error {self.count}")

        host.executor.command_executor = Executor()
    try:
        result = await host.run(RunRequest("repair", mode="agent"))
        assert result.status == "completed" and result.output == "finished"
        assert len(provider.requests) == 5
        assert not loop_events(host, result.run_id)
        if change == "source":
            assert (repo / "progress.txt").read_text() == "xxxx"
    finally:
        await host.close()


@pytest.mark.parametrize("body", ["valid data", "Error: this is file data, not a failed read"])
async def test_successful_repeated_reads_never_create_extra_progress_scans(
    isolated_workspace, body
):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "source.txt").write_text(body)
    provider = ScriptedProvider(
        [
            *(response(call(i, "read_file", path="source.txt")) for i in range(4)),
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        assert result.status == "completed"
        events = host.store.run_events(result.run_id)
        first_tool = next(
            event["session_seq"] for event in events if event["type"] == "tool.prepared"
        )
        assert not [
            event
            for event in events
            if event["type"] == "workspace.checkpoint" and event["session_seq"] > first_tool
        ]
        assert not loop_events(host, result.run_id)
        actual = [event["payload"] for event in events if event["type"] == "tool.completed"]
        assert len(actual) == 4
        assert all(event["content"] == f"1: {body}" for event in actual)
        assert all(not event["is_error"] for event in actual)
        assert all("failure_fingerprint" not in event for event in actual)
    finally:
        await host.close()


async def test_readonly_failure_refresh_detects_external_source_progress(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")

    class Provider(ScriptedProvider):
        async def complete(self, request):
            if len(self.requests) == 2:
                (repo / "edited-externally.txt").write_text("editor progress")
            return await super().complete(request)

    provider = Provider(
        [
            *(response(call(i, "read_file", path="missing.txt")) for i in range(3)),
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)

    def read_unhandled(path, **kwargs):
        return (repo / path).read_text()

    host.handlers["read_file"] = read_unhandled
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        assert result.status == "completed"
        assert not loop_events(host, result.run_id)
        events = host.store.run_events(result.run_id)
        third = [event for event in events if event["type"] == "tool.completed"][2]
        progress = [
            event
            for event in events
            if event["type"] == "workspace.checkpoint"
            and event["session_seq"] > third["session_seq"]
        ]
        assert len(progress) == 1
        assert progress[0]["payload"]["checkpoint"]["untracked"]
    finally:
        await host.close()


async def test_readonly_failures_stop_only_after_one_threshold_progress_check(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider(
        [
            *(response(call(i, "read_file", path="missing.txt")) for i in range(4)),
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)

    def read_unhandled(path, **kwargs):
        return (repo / path).read_text()

    host.handlers["read_file"] = read_unhandled
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        assert result.status == "interrupted"
        assert len(provider.requests) == result.tool_calls == 3
        events = host.store.run_events(result.run_id)
        first, _, third = [
            event["session_seq"] for event in events if event["type"] == "tool.completed"
        ]
        assert not [
            event
            for event in events
            if event["type"] == "workspace.checkpoint" and first < event["session_seq"] < third
        ]
        assert len(loop_events(host, result.run_id)) == 1
    finally:
        await host.close()


async def test_success_between_failures_breaks_the_window(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider(
        [
            response(call(1), call(2)),
            response(call(3, "glob", pattern="*")),
            response(call(4), call(5)),
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)
    try:
        result = await host.run(RunRequest("repair", mode="agent"))
        assert result.status == "completed"
        assert not loop_events(host, result.run_id)
    finally:
        await host.close()


async def test_non_git_directory_keeps_original_step_limit(isolated_workspace):
    provider = ScriptedProvider([response(call(i)) for i in range(4)])
    host = RuntimeHost(settings_for(isolated_workspace, max_steps=4), provider)
    try:
        result = await host.run(RunRequest("repair", mode="agent"))
        assert result.status == "interrupted" and result.tool_calls == 4
        assert host.store.get_run(result.run_id)["reason"] == "step_budget"
        assert not loop_events(host, result.run_id)
    finally:
        await host.close()


async def test_continue_after_restart_reconstructs_the_exact_failure_chain(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = settings_for(repo, max_steps=2)
    host = RuntimeHost(settings, ScriptedProvider([response(call(1)), response(call(2))]))
    try:
        prior = await host.run(RunRequest("repair", mode="agent"))
        assert host.store.get_run(prior.run_id)["reason"] == "step_budget"
        original = host.store.run_events(prior.run_id)
    finally:
        await host.close()
    provider = ScriptedProvider([response(call(3)), {"text": "must not claim success"}])
    reopened = RuntimeHost(settings, provider)

    async def approve(*args):
        return True

    try:
        result = await reopened.continue_session(prior.session_id, approval_handler=approve)
        assert result.status == "interrupted" and len(provider.requests) == 1
        assert reopened.store.get_run(result.run_id)["reason"] == "repeated_tool_failure"
        assert reopened.store.run_events(prior.run_id) == original
        evidence = loop_events(reopened, result.run_id)[0]["payload"]
        assert evidence["continued_failure_chain"] is True
        assert [item["run_id"] for item in evidence["calls"]] == [
            prior.run_id,
            prior.run_id,
            result.run_id,
        ]
    finally:
        await reopened.close()


@pytest.mark.parametrize("control", ["budget", "unknown", "invalid_arguments"])
async def test_control_failures_keep_their_existing_priority(isolated_workspace, control):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider(
        [
            response(
                call(i, "read_file", path=(5 if control == "invalid_arguments" else "missing.txt"))
            )
            for i in range(4)
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)
    if control == "budget":

        def blocked(**kwargs):
            raise BudgetExceeded([])

        host.handlers["read_file"] = blocked
    elif control == "unknown":

        class Unknown(RuntimeError):
            outcome_unknown = True

        def unknown(**kwargs):
            raise Unknown("Unknown actual outcome")

        host.handlers["read_file"] = unknown
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        assert not loop_events(host, result.run_id)
        if control == "invalid_arguments":
            assert result.status == "failed" and len(provider.requests) == 2
            assert "argument repair failed" in result.output
        else:
            assert result.status == "interrupted" and len(provider.requests) == 1
            assert host.store.get_run(result.run_id)["reason"] == (
                "token_budget" if control == "budget" else "unknown_tool_outcome"
            )
    finally:
        await host.close()


async def test_abandoned_batch_tail_does_not_erase_the_failure_chain(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider(
        [
            response(
                call(1), call(2), call(3), call(4, "write_file", path="unused", content="bad")
            ),
            response(call(5)),
            {"text": "must not claim success"},
        ]
    )
    host = RuntimeHost(settings_for(repo), provider)

    async def approve(*args):
        return True

    try:
        prior = await host.run(RunRequest("repair", mode="agent"))
        result = await host.continue_session(prior.session_id, approval_handler=approve)
        assert result.status == "interrupted"
        assert len(provider.requests) == 2
        evidence = loop_events(host, result.run_id)[0]["payload"]
        assert evidence["continued_failure_chain"] is True
        assert [item["call_id"] for item in evidence["calls"]] == ["call-2", "call-3", "call-5"]
        assert not (repo / "unused").exists()
    finally:
        await host.close()


async def test_simultaneous_runs_do_not_share_failure_windows(isolated_workspace):
    first_repo = make_repo(isolated_workspace / "first")
    second_repo = make_repo(isolated_workspace / "second")

    class Provider(ScriptedProvider):
        def __init__(self):
            super().__init__([])
            self.counts = {"first": 0, "second": 0}

        async def complete(self, request):
            label = request.messages[0]["content"]
            self.counts[label] += 1
            await asyncio.sleep(0)
            from eventide.models import ModelResponse, ToolCall

            if self.counts[label] <= 2:
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            f"{label}-{self.counts[label]}",
                            "bash",
                            {"command": "false"},
                        ),
                    )
                )
            return ModelResponse("done")

    provider = Provider()
    host = RuntimeHost(settings_for(first_repo), provider)
    second = host.resolve_or_register_workspace(second_repo)
    first_session = host.create_session()
    second_session = host.create_session(workspace_id=second.workspace_id)
    try:
        results = await asyncio.gather(
            host.run(RunRequest("first", first_session, mode="agent")),
            host.run(RunRequest("second", second_session, mode="agent")),
        )
        assert all(result.status == "completed" and result.tool_calls == 2 for result in results)
        assert not any(loop_events(host, result.run_id) for result in results)
    finally:
        await host.close()


async def test_cancel_after_third_completed_failure_keeps_cancel_reason(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider([response(call(1), call(2), call(3))])
    host = RuntimeHost(settings_for(repo), provider)
    third_completed = asyncio.Event()
    results = []

    class Sink:
        async def emit(self, event):
            if event["type"] == "tool.result":
                results.append(event)
                if len(results) == 3:
                    third_completed.set()
                    await asyncio.Future()

    task = asyncio.create_task(host.run(RunRequest("repair", mode="agent"), sink=Sink()))
    try:
        await asyncio.wait_for(third_completed.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        run_id = results[0]["run_id"]
        assert host.store.get_run(run_id)["reason"] == "cancelled"
        assert (
            len(
                [
                    event
                    for event in host.store.run_events(run_id)
                    if event["type"] == "tool.completed"
                ]
            )
            == 3
        )
        assert not loop_events(host, run_id)
        assert build_usage_ledger(host.store.run_events(run_id))["attempt_count"] == 1
    finally:
        task.cancel()
        await host.close()
