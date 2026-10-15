"""Production roots, model attempts and durable acceptance have distinct owners."""

import asyncio
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import ModelRequest, ModelResponse, RunRequest, ToolCall
from eventide.providers import ProviderError, ScriptedProvider
from eventide.scheduler import RuntimeScheduler, SchedulingLimits
from tests.test_delivery_runtime import delivery_host
from tests.test_host import make_repo
from tests.test_model_budget import admitted
from tests.test_readonly_workers import report
from tests.test_runtime import settings_for
from tests.test_worktree_execution import make_host


class BlockingProvider:
    def __init__(self):
        self.requests = []
        self.entered = asyncio.Queue()
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0

    async def complete(self, request):
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.active, self.peak)
        await self.entered.put(request)
        try:
            await self.release.wait()
            return ModelResponse("done", usage={"input_tokens": 2, "output_tokens": 1})
        finally:
            self.active -= 1


def limited(host, roots=1, models=2, waiting=20):
    host.dispatch.scheduler = RuntimeScheduler(host.store, SchedulingLimits(roots, models, waiting))


async def accept(host, run_id, target="snapshot", session=None):
    session = session or host.create_session()
    await host.admit_run(RunRequest(
        f"goal {run_id}", session, run_id, execution_target=target,
        mode="plan" if target == "snapshot" else "agent",
    ))
    return host.start_admitted_run(run_id)


async def entered(provider, count=1):
    for _ in range(count):
        await asyncio.wait_for(provider.entered.get(), 8)


async def test_default_limits_http_capacity_idempotency_and_exact_cancel(isolated_workspace):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(host)), base_url="http://localhost",
    )
    sessions = [host.create_session() for _ in range(25)]
    body = {"prompt": "inspect", "mode": "plan", "execution_target": "snapshot"}
    try:
        accepted = []
        for index, session in enumerate(sessions[:24]):
            response = await client.post(
                f"/api/sessions/{session}/runs", json=body,
                headers={"Idempotency-Key": f"job-{index}"},
            )
            assert response.status_code == 202, response.text
            accepted.append(response.json()["run_id"])
        await entered(provider, 2)
        queue = (await client.get("/api/queue")).json()
        assert queue["limits"] == {"active_roots": 4, "model_requests": 2, "waiting_roots": 20}
        assert queue["counts"] == {
            "active_roots": 4, "waiting_roots": 20, "preparing_admissions": 0,
            "active_models": 2, "waiting_models": 2,
        }
        assert [row["run_id"] for row in queue["queued"]] == accepted[4:]
        assert [row["position"] for row in queue["queued"]] == list(range(1, 21))
        assert all(row["status"] == row["dispatch_state"] == "queued"
                   for row in queue["queued"])
        assert all(row["project_id"] == host.default_workspace.project_id
                   for row in queue["active"] + queue["queued"])
        assert host.session_status(sessions[-2])["status"] == "queued"
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        duplicate = await client.post(
            f"/api/sessions/{sessions[4]}/runs", json=body,
            headers={"Idempotency-Key": "job-4"},
        )
        assert duplicate.status_code == 202 and duplicate.json()["run_id"] == accepted[4]
        rejected = await client.post(f"/api/sessions/{sessions[-1]}/runs", json=body)
        assert rejected.status_code == 429
        assert host.store.load_messages(sessions[-1]) == []
        assert host.store.list_runs(sessions[-1]) == []
        config = await client.delete("/api/config/provider")
        assert config.status_code == 409
        stopped = await client.post(f"/api/runs/{accepted[4]}/cancel")
        assert stopped.status_code == 200 and stopped.json()["status"] == "interrupted"
        assert not host.session_busy(sessions[4])
        assert host.store.load_messages(sessions[4])[0]["content"] == "inspect"
        assert not host.store.usage_events(run_id=accepted[4])
        assert len(provider.requests) == 2
        replacement = await client.post(f"/api/sessions/{sessions[-1]}/runs", json=body)
        assert replacement.status_code == 202
        queue = (await client.get("/api/queue")).json()
        assert queue["queued"][-1]["run_id"] == replacement.json()["run_id"]
        assert (await client.get("/api/queue", params={"project_id": "missing"})).status_code == 404
        other = host.resolve_or_register_workspace(make_repo(isolated_workspace / "other"))
        filtered = (await client.get("/api/queue", params={"project_id": other.project_id})).json()
        assert not filtered["queued"] and not filtered["active"]
        assert filtered["counts"] == queue["counts"]  # Limits/counts always describe the service.
    finally:
        await client.aclose()
        await host.close()
    assert not host.dispatch._leases and not host._admission_done and not host._admissions


async def test_waiting_for_model_does_not_reserve_or_create_attempt(isolated_workspace):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    try:
        tasks = [await accept(host, f"root-{i}") for i in range(4)]
        await entered(provider, 2)
        await asyncio.sleep(0.01)
        assert host.dispatch.scheduler.counts()["waiting_models"] == 2
        waiting = next(run for run in ("root-2", "root-3")
                       if not host.store.usage_events(run_id=run))
        await host.cancel_run(waiting)
        assert not host.store.usage_events(run_id=waiting)
        assert all(item["run_id"] != waiting
                   for item in host.budget_status()["reservations"])
        assert len(provider.requests) == 2
        provider.release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(item, asyncio.CancelledError) for item in results) == 1
        assert provider.peak == 2
        assert host.dispatch.scheduler.counts()["active_models"] == 0
    finally:
        await host.close()


async def test_connection_probe_uses_same_model_capacity_without_fake_run_usage(
    isolated_workspace, monkeypatch,
):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    monkeypatch.setattr("eventide.host.build_provider", lambda _settings: provider)
    probe = None
    try:
        tasks = [await accept(host, f"root-{i}") for i in range(2)]
        await entered(provider, 2)
        probe = asyncio.create_task(host.probe_provider(
            provider="openai", api_key="test-only", base_url=None, model="probe",
        ))
        await asyncio.sleep(0.01)
        assert host.dispatch.scheduler.counts()["waiting_models"] == 1
        assert len(provider.requests) == 2
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        provider.release.set()
        await asyncio.gather(*tasks)
        assert len(host.budget_status()["reservations"]) == 2
        assert host.dispatch.scheduler.counts()["waiting_models"] == 0
    finally:
        if probe is not None:
            probe.cancel()
        await host.close()


async def test_unstarted_queue_keeps_configuration_and_session_ownership(isolated_workspace):
    host = make_host(isolated_workspace, ScriptedProvider([]))
    limited(host)
    first_session, queued_session = host.create_session(), host.create_session()
    try:
        for identity, session in (("first", first_session), ("queued", queued_session)):
            await host.admit_run(RunRequest("frozen", session, identity, mode="plan",
                                            execution_target="snapshot"))
        await host.cancel_run("first")
        assert not host._active_tasks and not host._admissions
        assert host.runtime_busy() and host.session_busy(queued_session)
        assert host.dispatch.snapshot()["counts"]["waiting_roots"] == 1
        with pytest.raises(ValueError, match="active runs"):
            await host.configure_provider(
                provider="openai", api_key=None, base_url=None, model="replacement",
            )
        with pytest.raises(ValueError, match="active runs"):
            await host.reset_provider_configuration()
        stopped = await host.cancel_run("queued")
        assert stopped["status"] == "interrupted"
        assert not host.runtime_busy() and not host.session_busy(queued_session)
        assert not host.provider.requests
    finally:
        await host.close()


async def test_unverifiable_source_is_rejected_before_queue_acceptance(isolated_workspace):
    host = make_host(isolated_workspace, ScriptedProvider([]))
    limited(host)
    other = isolated_workspace / "plain-directory"
    other.mkdir()
    workspace = host.resolve_or_register_workspace(other)
    session = host.create_session(workspace_id=workspace.workspace_id)
    try:
        await host.admit_run(RunRequest("hold", "hold", "hold", mode="plan",
                                       execution_target="snapshot"))
        with pytest.raises(ValueError, match="Git checkpoint"):
            await host.admit_run(RunRequest("unsafe baseline", session, "unverifiable"))
        assert not host.store.get_run_identity("unverifiable")
        assert not host.store.load_messages(session)
        assert host.dispatch.scheduler.counts()["waiting_roots"] == 0
    finally:
        await host.close()


async def test_same_session_preparation_ticket_is_http_conflict(isolated_workspace, monkeypatch):
    host = make_host(isolated_workspace, ScriptedProvider([{"text": "done"}]))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(host)), base_url="http://localhost",
    )
    session = host.create_session()
    preparing, release = asyncio.Event(), asyncio.Event()
    original = host._prepare_run

    async def slow_prepare(*args, **kwargs):
        preparing.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(host, "_prepare_run", slow_prepare)
    first = asyncio.create_task(client.post(
        f"/api/sessions/{session}/runs", json={"prompt": "first"},
    ))
    try:
        await asyncio.wait_for(preparing.wait(), 5)
        queue = (await client.get("/api/queue")).json()
        assert queue["counts"]["preparing_admissions"] == 1
        assert queue["counts"]["active_roots"] == 1
        assert not queue["queued"] and not queue["active"]
        second = await client.post(f"/api/sessions/{session}/runs", json={"prompt": "second"})
        assert second.status_code == 409
        assert not host.store.load_messages(session)
        release.set()
        accepted = await first
        assert accepted.status_code == 202
        task = host._run_tasks.get(accepted.json()["run_id"])
        if task:
            await task
        assert host.store.load_messages(session)[0]["content"] == "first"
    finally:
        release.set()
        await first
        await client.aclose()
        await host.close()


@pytest.mark.parametrize("target", ["source", "snapshot", "worktree"])
async def test_queued_sources_and_copies_recheck_frozen_input(isolated_workspace, target):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    limited(host)
    try:
        first = await accept(host, "first")
        await entered(provider)
        session = host.create_session()
        queued = await accept(host, "queued", target, session)
        key = host._execution_key("queued", host._admitted_intent("queued"))
        assert not host.workspace_busy(key) and host.session_busy(session)
        assert host.store.get_run("queued")["status"] == "queued"
        with pytest.raises(ValueError, match="busy"):
            await host.admit_run(RunRequest("another", session))
        root = host.settings.workdir
        if target == "worktree":
            binding = host.run_execution("queued")
            root = Path(host.store.get_workspace(binding["execution_workspace_id"]).path)
        (root / "code.py").write_text("changed after receipt\n")
        provider.release.set()
        assert (await first).status == "completed"
        if target == "snapshot":
            assert (await queued).status == "completed"
            assert len(provider.requests) == 2
        else:
            with pytest.raises(ValueError, match="changed after admission"):
                await queued
            assert host.store.get_run("queued")["status"] == "failed"
            assert not host.store.usage_events(run_id="queued")
            assert len(provider.requests) == 1
        assert host.dispatch.snapshot()["counts"]["active_roots"] == 0
    finally:
        await host.close()


async def test_control_lock_release_wakes_oldest_eligible_queued_run(isolated_workspace):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    limited(host)
    try:
        first = await accept(host, "first")
        await entered(provider)
        blocked = await accept(host, "blocked", "source")
        later = await accept(host, "later")
        async with host._workspace_execution(host.default_workspace.workspace_id):
            provider.release.set()
            assert (await first).status == "completed"
            assert (await asyncio.wait_for(later, 8)).status == "completed"
            assert not blocked.done()
            assert host.dispatch.scheduler.counts()["waiting_roots"] == 1
        assert (await asyncio.wait_for(blocked, 8)).status == "completed"
        assert host.dispatch.scheduler.counts()["waiting_roots"] == 0
    finally:
        await host.close()


async def test_dispatch_failure_frees_slot_for_next_exact_run(isolated_workspace):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    limited(host)
    try:
        first = await accept(host, "first")
        await entered(provider)
        broken = await accept(host, "broken", "worktree")
        following = await accept(host, "following")
        binding = host._execution_binding("broken")
        root, _ = host._execution_paths(binding)
        (root / "code.py").write_text("unaccepted change\n")
        provider.release.set()
        await first
        with pytest.raises(ValueError, match="changed after admission"):
            await broken
        assert (await asyncio.wait_for(following, 8)).status == "completed"
        assert host.dispatch.snapshot()["active"] == []
    finally:
        await host.close()


async def test_task_creation_failure_terminates_acceptance_and_releases_capacity(
    isolated_workspace, monkeypatch,
):
    host = make_host(isolated_workspace, ScriptedProvider([]))
    try:
        await host.admit_run(RunRequest("accepted", "session", "failed-start", mode="plan",
                                       execution_target="snapshot"))

        def failed_create(_coroutine):
            raise RuntimeError("cannot launch task")

        with monkeypatch.context() as patch:
            patch.setattr("eventide.run_dispatch.asyncio.create_task", failed_create)
            with pytest.raises(RuntimeError, match="cannot launch"):
                host.start_admitted_run("failed-start")
        assert host.store.get_run("failed-start")["status"] == "failed"
        assert not host._admissions and not host._admission_done
        assert not host.dispatch._ready and not host.dispatch._leases
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        await host.close()


@pytest.mark.parametrize("target", ["source", "worktree"])
async def test_queued_continue_cannot_hide_drift_or_bypass_session_owner(
    isolated_workspace, target,
):
    provider = BlockingProvider()
    host = make_host(isolated_workspace, provider)
    limited(host)
    session = host.create_session()
    try:
        original = await accept(host, "original", target, session)
        await entered(provider)
        await host.cancel_run("original")
        assert original.cancelled()
        root = host.settings.workdir
        if target == "worktree":
            root, _ = host._execution_paths(host._execution_binding("original"))
        holder = await accept(host, "holder")
        await entered(provider)
        await host.admit_continue(session, "continued")
        continued = host.start_admitted_run("continued")
        assert host.store.get_run("continued")["status"] == "queued"
        (root / "code.py").write_text("changed after Continue was accepted")
        with pytest.raises(ValueError, match="busy"):
            await host.admit_run(RunRequest("new goal", session, execution_target=target))
        provider.release.set()
        await holder
        with pytest.raises(ValueError, match="changed after admission"):
            await continued
        assert not host.store.usage_events(run_id="continued")
        assert len(provider.requests) == 2
        assert (host.store.get_session(session)["workspace_id"]
                == host.default_workspace.workspace_id)
    finally:
        await host.close()


async def test_restart_parks_unstarted_source_without_replacing_checkpoint(isolated_workspace):
    source = make_repo(isolated_workspace / "source")
    state = isolated_workspace / "state"
    code = '''
import asyncio, os, sys
from pathlib import Path
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.scheduler import RuntimeScheduler, SchedulingLimits
from tests.test_runtime import settings_for
from dataclasses import replace
async def main():
    host = RuntimeHost(replace(settings_for(Path(sys.argv[1])), state_dir=Path(sys.argv[2])),
                       enable_mcp=False)
    host.dispatch.scheduler = RuntimeScheduler(host.store, SchedulingLimits(1, 2, 20))
    await host.admit_run(RunRequest("hold", "hold-session", "hold", mode="plan",
                                   execution_target="snapshot"))
    await host.admit_run(RunRequest("must preserve", "queued-session", "queued"))
    os._exit(0)
asyncio.run(main())
'''
    child = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code, str(source), str(state)],
        capture_output=True, text=True, timeout=20,
    )
    assert child.returncode == 0, child.stderr
    (source / "a.txt").write_text("changed after process death")
    host = RuntimeHost(replace(settings_for(source), state_dir=state), ScriptedProvider([]))
    try:
        events = host.store.run_events("queued")
        assert sum(event["type"] == "workspace.checkpoint" for event in events) == 1
        assert events[-1]["payload"]["reason"] == "host_stopped_before_dispatch"
        assert host.store.get_run("queued")["status"] == "interrupted"
        assert not host.dispatch.snapshot()["queued"]
        assert not host.provider.requests
        with pytest.raises(ValueError, match="changed or checkpoint"):
            await host.continue_session("queued-session")
        assert not host.provider.requests
        assert host.store.load_messages("queued-session")[0]["content"] == "must preserve"
    finally:
        await host.close()


async def test_retry_backoff_releases_model_slot_before_another_request(isolated_workspace):
    seen = asyncio.Queue()
    class RetryProvider:
        retried = False

        async def complete(self, request):
            await seen.put(request.system)
            if request.system == "retry" and not self.retried:
                self.retried = True
                raise ProviderError("retry once", retryable=True)
            return ModelResponse("done")

    host = make_host(isolated_workspace, RetryProvider())
    limited(host, models=1)
    try:
        first_id, second_id = admitted(host, "retry"), admitted(host, "other")
        request = ModelRequest("retry", [], [], "scripted", 10)
        first = asyncio.create_task(host._complete_with_retry(request, first_id, None))
        assert await asyncio.wait_for(seen.get(), 5) == "retry"
        second = asyncio.create_task(host._complete_with_retry(
            replace(request, system="other"), second_id, None,
        ))
        assert await asyncio.wait_for(seen.get(), 5) == "other"
        await second
        await first
        assert await seen.get() == "retry"
        assert host.dispatch.scheduler.counts()["active_models"] == 0
    finally:
        await host.close()


async def test_sync_tool_cleanup_keeps_root_slot_during_repeated_cancel(isolated_workspace):
    arrived, release, finished = threading.Event(), threading.Event(), threading.Event()
    source = make_repo(isolated_workspace / "source")

    def slow():
        arrived.set()
        assert release.wait(10)
        (source / "late.txt").write_text("finished under the same owner")
        finished.set()
        return "written"

    provider = ScriptedProvider([
        {"tool_calls": [{"id": "slow", "name": "slow", "arguments": {}}]}, {"text": "next"},
    ])
    host = RuntimeHost(
        replace(settings_for(source), state_dir=isolated_workspace / "state"), provider,
        tools=[{"name": "slow", "input_schema": {"type": "object", "properties": {}}}],
        handlers={"slow": slow},
    )
    limited(host)
    other = host.resolve_or_register_workspace(make_repo(isolated_workspace / "other"))
    try:
        first = await accept(host, "writing", "source")
        assert await asyncio.to_thread(arrived.wait, 5)
        next_task = await accept(
            host, "next", "source", host.create_session(workspace_id=other.workspace_id),
        )
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.sleep(0.02)
        assert not first.done() and not next_task.done() and not finished.is_set()
        queue = host.dispatch.snapshot()
        assert queue["counts"]["active_roots"] == queue["counts"]["waiting_roots"] == 1
        assert host.workspace_busy(host.default_workspace.workspace_id)
        assert len(provider.requests) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await asyncio.wait_for(next_task, 8)).output == "next"
        assert finished.is_set() and host.store.get_run("writing")["status"] == "interrupted"
    finally:
        release.set()
        await host.close()


async def test_four_parents_and_workers_share_model_slots_without_root_deadlock(isolated_workspace):
    class WorkerProvider(BlockingProvider):
        async def complete(self, request):
            self.requests.append(request)
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                worker = "You are a read-only" in request.messages[0]["content"]
                if worker:
                    await self.entered.put(request)
                    await self.release.wait()
                    return ModelResponse(report(), usage={"input_tokens": 2, "output_tokens": 1})
                if len(request.messages) == 1:
                    return ModelResponse(tool_calls=(ToolCall(
                        "delegate", "delegate_readonly", {"tasks": [
                            {"role": "reviewer", "goal": "read code"},
                            {"role": "researcher", "goal": "locate code"},
                        ]},
                    ),), usage={"input_tokens": 2, "output_tokens": 1})
                return ModelResponse("coordinated", usage={"input_tokens": 2, "output_tokens": 1})
            finally:
                self.active -= 1

    provider = WorkerProvider()
    host = make_host(isolated_workspace, provider, workers_enabled=True)
    try:
        tasks = [await accept(host, f"parent-{i}", "worktree") for i in range(4)]
        await entered(provider, 2)
        queue = host.dispatch.snapshot()
        assert queue["counts"]["active_roots"] == 4
        assert len(queue["active"]) == 4 and not queue["queued"]
        assert all(item["run_id"].startswith("parent-") for item in queue["active"])
        provider.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert all(result.status == "completed" and result.output == "coordinated"
                   for result in results)
        assert provider.peak == 2
        for result in results:
            run = host.store.get_run(result.run_id)
            delegated = json.loads(run["tools"]["delegate"]["content"])
            assert len(delegated["workers"]) == 2
            assert not run["tools"]["delegate"]["is_error"]
        reservations = host.budget_status()["reservations"]
        assert len(reservations) == 16  # 2 parent + 2 children per root, never a second charge.
        assert {host.model_budget.context(row["run_id"])[1].root_run_id
                for row in reservations} == {
            f"parent-{i}" for i in range(4)
        }
        assert host.dispatch.snapshot()["counts"]["active_roots"] == 0
    finally:
        provider.release.set()
        await host.close()


async def test_delivery_managers_do_not_consume_their_writers_root_slots(isolated_workspace):
    host = delivery_host(isolated_workspace)
    provider = BlockingProvider()
    host.provider = provider
    prepared = [await host.manage_delivery("prepare", name=f"copy-{i}") for i in range(5)]
    tasks = [asyncio.create_task(host.manage_delivery(
        "run", delivery_id=item["delivery_id"], goal="inspect the fix",
    )) for item in prepared]

    async def wait_for_queue():
        while len(host.dispatch.snapshot()["queued"]) != 1:
            await asyncio.sleep(0.01)

    try:
        await entered(provider, 2)
        await asyncio.wait_for(wait_for_queue(), 15)
        queue = host.dispatch.snapshot()
        assert len(queue["active"]) == 4 and len(queue["queued"]) == 1
        ids = {row["run_id"] for row in queue["active"] + queue["queued"]}
        assert all(host._admitted_intent(run)["execution_config"]["writer_delivery_id"]
                   for run in ids)
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        provider.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert all(result["status"] == "completed" for result in results)
        assert len(host.dispatch.scheduler.entries()) == 5
        assert provider.peak == 2
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        provider.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()
