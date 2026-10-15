"""Durable Plan copies overlap without sharing source files or conversation owners."""

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def snapshot_host(root, provider, **changes):
    source = make_repo(root / "source")
    (source / "code.py").write_text("value = 'original'\n")
    settings = replace(settings_for(source), state_dir=root / "state", **changes)
    return RuntimeHost(settings, provider)


def request(session=None, **kwargs):
    return RunRequest(
        "inspect", session_id=session, mode="plan", execution_target="snapshot", **kwargs
    )


class OverlappingProvider:
    def __init__(self):
        self.entered = 0
        self.both = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def complete(self, query):
        self.requests.append(query)
        if len(query.messages) == 1:
            self.entered += 1
            if self.entered == 2:
                self.both.set()
            await self.release.wait()
            return ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "code.py"}),))
        return ModelResponse(text=str(query.messages[-1]))


async def test_same_project_snapshot_runs_overlap_and_source_drift_does_not_change_inputs(
    isolated_workspace,
):
    provider = OverlappingProvider()
    host = snapshot_host(isolated_workspace, provider)
    sessions = [host.create_session(), host.create_session()]
    tasks = [asyncio.create_task(host.run(request(session))) for session in sessions]
    try:
        await asyncio.wait_for(provider.both.wait(), 5)
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        assert all(host.session_busy(session) for session in sessions)
        (host.settings.workdir / "code.py").write_text("value = 'changed-source'\n")
        provider.release.set()
        results = await asyncio.gather(*tasks)
        assert all(result.status == "completed" for result in results)
        assert all(
            "original" in result.output and "changed-source" not in result.output
            for result in results
        )
        assert all(host.run_execution(result.run_id)["kind"] == "snapshot" for result in results)
        assert all(
            host.store.get_session(session)["workspace_id"] == host.default_workspace.workspace_id
            for session in sessions
        )
    finally:
        provider.release.set()
        await host.close()


async def test_stopping_one_snapshot_does_not_stop_its_sibling_or_allow_same_session(
    isolated_workspace,
):
    provider = OverlappingProvider()
    host = snapshot_host(isolated_workspace, provider)
    first, second = host.create_session(), host.create_session()
    tasks = [
        asyncio.create_task(host.run(request(first, run_id="one"))),
        asyncio.create_task(host.run(request(second, run_id="two"))),
    ]
    try:
        await asyncio.wait_for(provider.both.wait(), 5)
        with pytest.raises(ValueError, match="Session is busy"):
            await host.admit_run(request(first, run_id="duplicate"))
        stopped = await host.cancel_run("one")
        assert stopped["status"] == "interrupted"
        assert not tasks[1].done()
        provider.release.set()
        result = await tasks[1]
        assert result.status == "completed"
        assert host.store.latest_run(first)["id"] == "one"
        assert host.store.get_run_identity("duplicate") is None
    finally:
        provider.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()


async def test_snapshot_continue_after_restart_reads_original_copy(isolated_workspace):
    host = snapshot_host(
        isolated_workspace,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}
                    ]
                },
            ]
        ),
        max_steps=1,
    )
    session = host.create_session()
    result = await host.run(request(session))
    assert result.status == "interrupted"
    execution = host.run_execution(result.run_id)
    settings = host.settings
    await host.close()
    (settings.workdir / "code.py").write_text("source changed\n")
    reopened = RuntimeHost(settings, ScriptedProvider([{"text": "done"}]))
    try:
        continued = await reopened.continue_session(session)
        assert continued.status == "completed"
        assert reopened.run_execution(continued.run_id) == execution
        assert (
            reopened.provider.requests[0].messages[-1]["content"][0]["content"].find("original")
            >= 0
        )
    finally:
        await reopened.close()


async def test_snapshot_tampering_refuses_start_without_model_request(isolated_workspace):
    provider = ScriptedProvider([{"text": "must not run"}])
    host = snapshot_host(isolated_workspace, provider)
    try:
        run_id = await host.admit_run(request())
        binding = host._execution_binding(run_id)
        root, _ = host.execution_snapshots.restore(binding)
        (root / "code.py").chmod(0o644)
        (root / "code.py").write_text("tampered")
        with pytest.raises(ValueError, match="snapshot changed"):
            await host.start_admitted_run(run_id)
        assert provider.requests == []
        assert host.store.get_run(run_id)["status"] == "failed"
    finally:
        await host.close()


async def test_accept_then_cancel_snapshot_remains_resumable(isolated_workspace):
    host = snapshot_host(isolated_workspace, ScriptedProvider([{"text": "done"}]))
    try:
        run_id = await host.admit_run(request())
        session = host.store.get_run_identity(run_id)["session_id"]
        await host.cancel_run(run_id)
        assert host.provider.requests == []
        assert (await host.continue_session(session)).status == "completed"
    finally:
        await host.close()


def test_snapshot_http_contract_and_legacy_source_default(isolated_workspace):
    host = snapshot_host(isolated_workspace, ScriptedProvider([{"text": "done"}, {"text": "done"}]))
    with TestClient(create_app(host)) as client:
        session = client.post("/api/sessions").json()["session_id"]
        invalid = client.post(
            f"/api/sessions/{session}/runs", json={"prompt": "x", "execution_target": "snapshot"}
        )
        assert invalid.status_code == 422
        assert host.store.list_runs(session) == []
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={
                "prompt": "inspect",
                "mode": "plan",
                "execution_target": "snapshot",
            },
        )
        assert accepted.status_code == 202
        result = wait_for_run(client, accepted.json()["run_id"])
        assert result["status"] == "completed"
        assert result["execution"]["kind"] == "snapshot"
        assert client.get(f"/api/sessions/{session}").json()["execution"] == result["execution"]
        source_session = client.post("/api/sessions").json()["session_id"]
        direct = client.post(f"/api/sessions/{source_session}/runs", json={"prompt": "hello"})
        assert wait_for_run(client, direct.json()["run_id"])["execution"] == {"kind": "source"}


async def test_snapshot_uses_frozen_skill_cwd_and_never_live_mcp_index_or_worker(
    isolated_workspace,
):
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {"id": "read", "name": "read_file", "arguments": {"path": "inside.py"}}
                ]
            },
            {"text": "done"},
        ]
    )
    host = snapshot_host(isolated_workspace, provider, workers_enabled=True)
    source = host.settings.workdir
    (source / "sub").mkdir()
    (source / "sub" / "inside.py").write_text("snapshot content\n")
    (source / ".env").write_text("PRIVATE=excluded")
    (source / "data.bin").write_bytes(b"binary\0data")
    (source / "linked.py").symlink_to(source / "code.py")
    skills = source / "skills" / "review"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n---\nOriginal instructions"
    )
    (skills / "scripts").mkdir()
    (skills / "scripts" / "check.py").write_text("raise RuntimeError('must not run')")
    session = host.create_session(working_directory="sub")
    try:
        run_id = await host.admit_run(request(session))
        (skills / "SKILL.md").write_text("changed invalid instructions")
        (source / "sub").rename(source / "moved-sub")
        result = await host.start_admitted_run(run_id)
        assert result.status == "completed"
        assert "snapshot content" in str(provider.requests[1].messages)
        names = {tool["name"] for tool in provider.requests[0].tools}
        assert "load_skill" in names
        assert not names & {
            "bash",
            "write_file",
            "edit_file",
            "run_skill_script",
            "delegate_readonly",
            "search_knowledge",
        }
        assert not any(name.startswith("mcp__") for name in names)
        root, cwd = host.execution_snapshots.restore(host._execution_binding(run_id))
        assert cwd == root / "sub"
        assert not (root / ".env").exists()
        assert not (root / "data.bin").exists()
        assert not (root / "linked.py").exists()
        assert "Original instructions" in (root / "skills" / "review" / "SKILL.md").read_text()
        assert host.session_status(session)["execution"]["kind"] == "snapshot"
    finally:
        await host.close()


async def test_snapshot_drift_during_admission_rejects_before_fact_or_request(
    isolated_workspace, monkeypatch
):
    provider = ScriptedProvider([{"text": "no"}])
    host = snapshot_host(isolated_workspace, provider)
    original = host.execution_snapshots.create

    def change_source(*args):
        binding = original(*args)
        (host.settings.workdir / "code.py").write_text("edited during preparation")
        return binding

    monkeypatch.setattr(host.execution_snapshots, "create", change_source)
    try:
        with pytest.raises(ValueError, match="changed during snapshot admission"):
            await host.admit_run(request(run_id="drift"))
        assert host.store.get_run_identity("drift") is None
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        assert provider.requests == []
    finally:
        await host.close()


async def test_direct_source_still_exclusive_but_does_not_block_existing_snapshot(
    isolated_workspace,
):
    provider = OverlappingProvider()
    host = snapshot_host(isolated_workspace, provider)
    reader_session, writer_session = host.create_session(), host.create_session()
    reader = asyncio.create_task(host.run(request(reader_session, run_id="reader")))
    while provider.entered != 1:
        await asyncio.sleep(0)
    writer = asyncio.create_task(host.run(RunRequest("direct", writer_session, "writer")))
    try:
        await asyncio.wait_for(provider.both.wait(), 5)
        assert host.workspace_busy(host.default_workspace.workspace_id)
        with pytest.raises(ValueError, match="Workspace is busy"):
            await host.admit_run(request(run_id="new-snapshot"))
        assert not reader.done()
        provider.release.set()
        assert all(result.status == "completed" for result in await asyncio.gather(reader, writer))
    finally:
        provider.release.set()
        await host.close()


async def test_cancel_snapshot_preparation_keeps_source_owned_until_copy_settles(
    isolated_workspace, monkeypatch
):
    import threading

    host = snapshot_host(isolated_workspace, ScriptedProvider([]))
    original = host.execution_snapshots.create
    entered, release = threading.Event(), threading.Event()

    def waiting_copy(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(host.execution_snapshots, "create", waiting_copy)
    task = asyncio.create_task(host.admit_run(request(run_id="preparing")))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert host.workspace_busy(host.default_workspace.workspace_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        assert host.store.get_run_identity("preparing") is None
    finally:
        release.set()
        await host.close()


@pytest.mark.parametrize("damage", ["missing", "link", "extra", "oversize", "identity", "cwd"])
async def test_snapshot_integrity_boundaries(isolated_workspace, damage):
    host = snapshot_host(isolated_workspace, ScriptedProvider([]))
    try:
        run_id = await host.admit_run(request())
        binding = host._execution_binding(run_id)
        root, _ = host.execution_snapshots.restore(binding)
        target = root / "code.py"
        if damage in {"missing", "link"}:
            target.unlink()
            if damage == "link":
                target.symlink_to(host.settings.workdir / "code.py")
        elif damage == "extra":
            (root / "extra").write_text("unexpected")
        elif damage == "oversize":
            target.chmod(0o644)
            target.write_bytes(b"x" * (1024 * 1024 + 1))
        elif damage == "identity":
            binding["snapshot_id"] = "a" * 64
        else:
            binding["working_directory"] = "../escape"
        with pytest.raises(ValueError):
            host.execution_snapshots.restore(binding)
    finally:
        await host.close()


async def test_unknown_snapshot_tool_blocks_continue_without_quarantining_source(
    isolated_workspace,
):
    from eventide.container_executor import ContainerCleanupError

    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
            {"text": "source is still available"},
        ]
    )
    host = snapshot_host(isolated_workspace, provider)

    def unknown_read(**_kwargs):
        raise ContainerCleanupError("injected unknown read result")

    host.handlers["read_file"] = unknown_read
    try:
        result = await host.run(request())
        assert result.status == "interrupted"
        assert not host.store.workspace_has_unknown_outcome(host.default_workspace.workspace_id)
        with pytest.raises(ValueError, match="Unknown tool outcome"):
            await host.continue_session(result.session_id)
        direct = await host.run(RunRequest("independent source"))
        assert direct.status == "completed"
        unknown = next(
            event["payload"]
            for event in host.store.run_events(result.run_id)
            if event["type"] == "tool.outcome_unknown"
        )
        assert unknown["snapshot_id"] == host.run_execution(result.run_id)["snapshot_id"]
    finally:
        await host.close()


async def test_http_snapshot_acceptance_concurrency_and_idempotency(isolated_workspace):
    import httpx

    provider = OverlappingProvider()
    host = snapshot_host(isolated_workspace, provider)
    app = create_app(host)
    sessions = [host.create_session(), host.create_session()]
    body = {"prompt": "inspect", "mode": "plan", "execution_target": "snapshot"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        f"/api/sessions/{session}/runs",
                        json=body,
                        headers={"Idempotency-Key": "snapshot-intent"},
                    )
                    for session in sessions
                )
            )
            assert [response.status_code for response in responses] == [202, 202]
            await asyncio.wait_for(provider.both.wait(), 5)
            first = responses[0].json()["run_id"]
            repeated = await client.post(
                f"/api/sessions/{sessions[0]}/runs",
                json=body,
                headers={"Idempotency-Key": "snapshot-intent"},
            )
            assert repeated.json()["run_id"] == first
            conflict = await client.post(
                f"/api/sessions/{sessions[0]}/runs",
                json={**body, "execution_target": "source"},
                headers={"Idempotency-Key": "snapshot-intent"},
            )
            assert conflict.status_code == 409
            busy = await client.post(f"/api/sessions/{sessions[0]}/runs", json=body)
            assert busy.status_code == 409
            cancelled = await client.post(f"/api/runs/{first}/cancel")
            assert cancelled.status_code == 200
            second = responses[1].json()["run_id"]
            assert host.store.get_run(second)["status"] == "running"
            provider.release.set()
            await asyncio.gather(*list(host._run_tasks.values()), return_exceptions=True)
            assert host.store.get_run(second)["status"] == "completed"
    finally:
        provider.release.set()
        await host.close()


async def test_snapshot_recovery_after_process_exit_preserves_original_inputs(isolated_workspace):
    import json
    import subprocess
    import sys

    source = make_repo(isolated_workspace / "source")
    (source / "code.py").write_text("fixed content\n")
    state = isolated_workspace / "state"
    code = """
import asyncio, json, os, sys
from pathlib import Path
from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.models import RunRequest
async def main():
    host = RuntimeHost(Settings(Path(sys.argv[1]), Path(sys.argv[2]),
        'scripted', None, None, 'saved-model'), enable_mcp=False)
    session = host.create_session()
    run = await host.admit_run(RunRequest('inspect', session, mode='plan',
                                         execution_target='snapshot'))
    print(json.dumps({'session': session, 'run': run}), flush=True)
    os._exit(0)
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(source), str(state)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 0, child.stderr
    saved = json.loads(child.stdout)
    (source / "code.py").write_text("later changed source\n")
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
            {"text": "done"},
        ]
    )
    host = RuntimeHost(replace(settings_for(source), state_dir=state), provider)
    try:
        assert provider.requests == []
        assert host.store.get_run(saved["run"])["status"] == "interrupted"
        assert (await host.continue_session(saved["session"])).status == "completed"
        assert "fixed content" in str(provider.requests[1].messages)
        assert "later changed" not in str(provider.requests[1].messages)
    finally:
        await host.close()
