"""Trusted same-response reads share bounded ownership and preserve every result."""

import asyncio
import threading

import pytest

from eventide.host import RuntimeHost
from eventide.models import RunRequest, ToolCall
from eventide.providers import ScriptedProvider
from eventide.scheduler import plan_readonly_batches
from eventide.tools.runtime_catalog import HANDLERS
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def read(number, path=None):
    return {"id": f"r{number}", "name": "read_file", "arguments": {"path": path or str(number)}}


def script(calls):
    return ScriptedProvider([{"tool_calls": calls}, {"text": "done"}])


async def entered(queue, count):
    return [await asyncio.wait_for(queue.get(), 5) for _ in range(count)]


async def wait_count(host, name, value):
    async def poll():
        while host.dispatch.scheduler.counts()[name] != value:
            await asyncio.sleep(0)
    await asyncio.wait_for(poll(), 5)


def test_plan_uses_identity_for_unhashable_handlers_and_barriers():
    class Custom:
        __hash__ = None

        def __call__(self, **_arguments):
            return "custom"

    handlers = {**HANDLERS, "read_file": Custom()}
    calls = [ToolCall(str(i), name, {}) for i, name in enumerate([
        "glob", "glob", "read_file", "search_text", "write_file", "glob",
    ])]
    plan = plan_readonly_batches(calls, handlers, (HANDLERS["glob"], HANDLERS["search_text"]))
    assert [(batch.parallel, [c.name for c in batch.calls]) for batch in plan] == [
        (True, ["glob", "glob"]), (False, ["read_file"]),
        (True, ["search_text"]), (False, ["write_file"]), (True, ["glob"]),
    ]


async def test_threads_overlap_and_finish_out_of_order_but_context_is_stable(
    isolated_workspace, monkeypatch,
):
    loop = asyncio.get_running_loop()
    starts, finishes = asyncio.Queue(), asyncio.Queue()
    gates = [threading.Event(), threading.Event()]

    def reading(path, cwd):
        index = int(path)
        loop.call_soon_threadsafe(starts.put_nowait, index)
        assert gates[index].wait(5)
        loop.call_soon_threadsafe(finishes.put_nowait, index)
        return f"actual-{index}"

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    provider = script([read(0), read(1)])
    host = RuntimeHost(settings_for(isolated_workspace), provider,
                       handlers={**HANDLERS, "read_file": reading})
    task = asyncio.create_task(host.run(RunRequest(
        "read", run_id="run-order", mode="plan",
    )))
    try:
        assert set(await entered(starts, 2)) == {0, 1}
        assert host.dispatch.scheduler.counts()["active_readonly_tools"] == 2
        state = host.store.get_run("run-order")["tools"]
        assert state["r0"]["status"] == state["r1"]["status"] == "running"
        assert state["r0"]["batch"]["position"] == 0
        assert state["r1"]["arguments"] == {"path": "1"}
        gates[1].set()
        assert await entered(finishes, 1) == [1]
        gates[0].set()
        result = await task
        assert result.status == "completed"
        facts = host.store.run_events(result.run_id)
        completed = [e["payload"] for e in facts if e["type"] == "tool.completed"]
        assert [e["call_id"] for e in completed] == ["r0", "r1"]
        assert [e["batch"]["position"] for e in completed] == [0, 1]
        assert all(e["batch"]["parallel"] for e in completed)
        assert [b["tool_use_id"] for b in provider.requests[-1].messages[-1]["content"]] == [
            "r0", "r1",
        ]
    finally:
        for gate in gates:
            gate.set()
        await host.close()


async def test_global_four_slots_include_single_read_runs(isolated_workspace, monkeypatch):
    repo = make_repo(isolated_workspace / "repo")
    loop = asyncio.get_running_loop()
    starts = asyncio.Queue()
    release = threading.Event()

    def reading(path, cwd):
        loop.call_soon_threadsafe(starts.put_nowait, path)
        assert release.wait(5)
        return path

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    provider = ScriptedProvider([
        {"tool_calls": [read(i) for i in range(4)]},
        {"tool_calls": [read(4)]}, {"text": "done"}, {"text": "done"},
    ])
    host = RuntimeHost(settings_for(repo), provider,
                       handlers={**HANDLERS, "read_file": reading})
    tasks = []
    try:
        tasks.append(asyncio.create_task(host.run(RunRequest(
            "first", mode="plan", execution_target="snapshot",
        ))))
        assert set(await entered(starts, 4)) == {"0", "1", "2", "3"}
        tasks.append(asyncio.create_task(host.run(RunRequest(
            "second", mode="plan", execution_target="snapshot",
        ))))
        await wait_count(host, "waiting_readonly_tools", 1)
        assert host.dispatch.snapshot()["limits"]["readonly_tools"] == 4
        assert starts.empty()
        release.set()
        assert await entered(starts, 1) == ["4"]
        assert all(r.status == "completed" for r in await asyncio.gather(*tasks))
        assert host.dispatch.scheduler.counts()["active_readonly_tools"] == 0
    finally:
        release.set()
        await host.close()


async def test_cancel_first_cancels_every_queued_call_then_waits_for_threads(
    isolated_workspace, monkeypatch,
):
    loop = asyncio.get_running_loop()
    starts = asyncio.Queue()
    release = threading.Event()

    def reading(path, cwd):
        loop.call_soon_threadsafe(starts.put_nowait, path)
        assert release.wait(5)
        return f"actual-{path}"

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    host = RuntimeHost(settings_for(isolated_workspace), script([read(i) for i in range(5)]),
                       handlers={**HANDLERS, "read_file": reading})
    run = "run-cancel-read"
    task = asyncio.create_task(host.run(RunRequest("read", run_id=run, mode="plan")))
    try:
        assert set(await entered(starts, 4)) == {"0", "1", "2", "3"}
        await wait_count(host, "waiting_readonly_tools", 1)
        owner = host._run_tasks[run]
        owner.cancel()
        await wait_count(host, "waiting_readonly_tools", 0)
        owner.cancel()  # Repeated stop cannot interrupt result persistence/ownership.
        await asyncio.sleep(0)
        assert not task.done() and host.workspace_busy(host.default_workspace.workspace_id)
        assert host.dispatch.scheduler.counts()["active_readonly_tools"] == 4
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert starts.empty()  # Call 4 never acquired a slot or reached its handler.
        events = host.store.run_events(run)
        completed = [e["payload"] for e in events if e["type"] == "tool.completed"]
        abandoned = [e["payload"] for e in events if e["type"] == "tool.abandoned"]
        assert [e["call_id"] for e in completed] == [f"r{i}" for i in range(4)]
        assert [(e["call_id"], e["dispatched"]) for e in abandoned] == [("r4", False)]
        assert abandoned[0]["batch"]["position"] == 4
        assert host.dispatch.scheduler.counts()["active_readonly_tools"] == 0
        assert not host.workspace_busy(host.default_workspace.workspace_id)
    finally:
        release.set()
        await host.close()


async def test_stop_during_first_result_sink_still_records_other_completed_reads(
    isolated_workspace, monkeypatch,
):
    sink_entered, sink_release = asyncio.Event(), asyncio.Event()

    class Sink:
        async def emit(self, event):
            if event["type"] == "tool.result" and event["payload"]["call_id"] == "r0":
                sink_entered.set()
                await sink_release.wait()

    def reading(path, cwd):
        return f"actual-{path}"

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    host = RuntimeHost(settings_for(isolated_workspace), script([read(0), read(1)]),
                       handlers={**HANDLERS, "read_file": reading})
    run = "run-cancel-sink"
    task = asyncio.create_task(host.run(RunRequest("read", run_id=run, mode="plan"), Sink()))
    try:
        await asyncio.wait_for(sink_entered.wait(), 5)
        host._run_tasks[run].cancel()
        await asyncio.sleep(0)
        host._run_tasks[run].cancel()
        sink_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = host.store.run_events(run)
        assert [e["payload"]["call_id"] for e in events if e["type"] == "tool.completed"] == [
            "r0", "r1",
        ]
        assert not [e for e in events if e["type"] == "tool.abandoned"]
        assert events[-1]["type"] == "run.interrupted"
    finally:
        sink_release.set()
        await host.close()


async def test_write_is_a_barrier_and_reads_after_it_observe_new_bytes(isolated_workspace):
    (isolated_workspace / "before").write_text("old")
    calls = [read(0, "before"), read(1, "before"),
             {"id": "write", "name": "write_file",
              "arguments": {"path": "before", "content": "new"}},
             read(2, "before"), read(3, "before")]
    provider = script(calls)
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("read/write/read", mode="agent"))
        assert result.status == "completed"
        complete = [e["payload"] for e in host.store.run_events(result.run_id)
                    if e["type"] == "tool.completed"]
        assert [e["call_id"] for e in complete] == ["r0", "r1", "write", "r2", "r3"]
        assert [e["content"].splitlines()[-1] for e in complete[:2]] == ["1: old", "1: old"]
        assert [e["content"].splitlines()[-1] for e in complete[-2:]] == ["1: new", "1: new"]
        assert complete[2]["batch"]["parallel"] is False
    finally:
        await host.close()


async def test_local_errors_and_schema_rejections_keep_other_results(isolated_workspace):
    (isolated_workspace / "ok").write_text("good")
    provider = script([read(0, "missing"), read(1, "ok"),
                       {"id": "invalid", "name": "read_file", "arguments": {}}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        completed = [e["payload"] for e in host.store.run_events(result.run_id)
                     if e["type"] == "tool.completed"]
        assert [e["call_id"] for e in completed] == ["r0", "r1", "invalid"]
        assert completed[0]["is_error"] is True
        assert completed[1]["content"].splitlines()[-1] == "1: good"
        assert completed[2]["metadata"]["code"] == "invalid_arguments"
        assert [b["tool_use_id"] for b in provider.requests[-1].messages[-1]["content"]] == [
            "r0", "r1", "invalid",
        ]
    finally:
        await host.close()


async def test_same_name_custom_callable_stays_serial(isolated_workspace):
    active = 0
    starts = asyncio.Queue()
    release = asyncio.Event()

    async def custom_read(path, cwd):
        nonlocal active
        active += 1
        assert active == 1
        starts.put_nowait(path)
        await release.wait()
        active -= 1
        return path

    host = RuntimeHost(settings_for(isolated_workspace), script([read(0), read(1)]),
                       handlers={**HANDLERS, "read_file": custom_read})
    task = asyncio.create_task(host.run(RunRequest("read", mode="plan")))
    try:
        assert await entered(starts, 1) == ["0"]
        assert starts.empty()
        assert host.dispatch.scheduler.counts()["active_readonly_tools"] == 0
        release.set()
        result = await task
        assert result.status == "completed"
        complete = [e["payload"] for e in host.store.run_events(result.run_id)
                    if e["type"] == "tool.completed"]
        assert all(not e["batch"]["parallel"] for e in complete)
    finally:
        release.set()
        await host.close()


async def test_failure_loop_keeps_already_executed_members_and_abandons_later_write(
    isolated_workspace, monkeypatch,
):
    repo = make_repo(isolated_workspace / "repo")

    def reading(path, cwd):
        raise OSError("fixed read failure")

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    calls = [read(i, "missing") for i in range(4)] + [
        {"id": "write", "name": "write_file", "arguments": {"path": "forbidden", "content": "x"}},
    ]
    host = RuntimeHost(settings_for(repo), script(calls),
                       handlers={**HANDLERS, "read_file": reading})
    try:
        result = await host.run(RunRequest("read", mode="agent"))
        assert result.status == "interrupted"
        events = host.store.run_events(result.run_id)
        assert [e["payload"]["call_id"] for e in events if e["type"] == "tool.completed"] == [
            "r0", "r1", "r2", "r3",
        ]
        assert [e["payload"]["call_id"] for e in events if e["type"] == "tool.abandoned"] == [
            "write",
        ]
        assert host.store.get_run(result.run_id)["reason"] == "repeated_tool_failure"
        assert not (repo / "forbidden").exists()
    finally:
        await host.close()


@pytest.mark.parametrize("stop_kind", ["budget", "unknown"])
async def test_stop_signals_keep_other_actual_results(isolated_workspace, monkeypatch, stop_kind):
    from eventide.budgets import BudgetExceeded
    from eventide.models import ToolOutput

    def reading(path, cwd):
        if path == "0":
            if stop_kind == "budget":
                raise BudgetExceeded([])
            return ToolOutput("lost read outcome", True, {"outcome_unknown": True})
        return f"actual-{path}"

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    host = RuntimeHost(settings_for(isolated_workspace), script([read(0), read(1)]),
                       handlers={**HANDLERS, "read_file": reading})
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        assert result.status == "interrupted"
        events = host.store.run_events(result.run_id)
        complete = [e["payload"] for e in events if e["type"] == "tool.completed"]
        assert complete[-1]["call_id"] == "r1" and complete[-1]["content"] == "actual-1"
        assert not [e for e in events if e["type"] == "tool.abandoned"]
        if stop_kind == "unknown":
            unknown = next(e["payload"] for e in events if e["type"] == "tool.outcome_unknown")
            assert unknown["readonly"] is True
            assert unknown["batch"]["position"] == 0
        else:
            assert complete[0]["metadata"]["code"] == "budget_exceeded"
    finally:
        await host.close()


async def test_stop_while_preparing_batch_dispatches_no_threads(isolated_workspace, monkeypatch):
    prepare_entered, prepare_release = asyncio.Event(), asyncio.Event()
    called = []

    class Sink:
        async def emit(self, event):
            if event["type"] == "tool.request":
                prepare_entered.set()
                await prepare_release.wait()

    def reading(path, cwd):
        called.append(path)
        return path

    monkeypatch.setattr("eventide.host.PARALLEL_READONLY_HANDLERS", (reading,))
    host = RuntimeHost(settings_for(isolated_workspace), script([read(0), read(1)]),
                       handlers={**HANDLERS, "read_file": reading})
    run = "run-cancel-prepare"
    task = asyncio.create_task(host.run(RunRequest("read", run_id=run, mode="plan"), Sink()))
    try:
        await asyncio.wait_for(prepare_entered.wait(), 5)
        host._run_tasks[run].cancel()
        await asyncio.sleep(0)
        prepare_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert called == []
        events = host.store.run_events(run)
        assert [e["payload"]["call_id"] for e in events if e["type"] == "tool.abandoned"] == [
            "r0", "r1",
        ]
        assert host.dispatch.scheduler.counts()["active_readonly_tools"] == 0
    finally:
        prepare_release.set()
        await host.close()


async def test_executor_exception_preserves_other_results_and_unknown_call_identity(
    isolated_workspace, monkeypatch,
):
    from eventide.executor import ToolExecutor

    original = ToolExecutor.execute

    async def execute(self, call, context):
        if call.id == "r0":
            raise RuntimeError("executor bookkeeping failed")
        return await original(self, call, context)

    monkeypatch.setattr(ToolExecutor, "execute", execute)
    (isolated_workspace / "ok").write_text("good")
    host = RuntimeHost(settings_for(isolated_workspace), script([read(0, "ok"), read(1, "ok")]))
    try:
        result = await host.run(RunRequest("read", mode="plan"))
        assert result.status == "failed" and "executor bookkeeping failed" in result.output
        events = host.store.run_events(result.run_id)
        unknown = next(e["payload"] for e in events if e["type"] == "tool.outcome_unknown")
        assert unknown["call_id"] == "r0" and unknown["readonly"] is True
        completed = [e["payload"] for e in events if e["type"] == "tool.completed"]
        assert [e["call_id"] for e in completed] == ["r1"]
        assert completed[0]["content"].endswith("1: good")
    finally:
        await host.close()
