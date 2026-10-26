"""MK05: complete redacted command logs bound to Run/call, paged by identity."""

from __future__ import annotations

import asyncio
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.command_logs import (
    CommandLogLimits,
    CommandLogs,
    stream_observer,
)
from eventide.executor import LocalCommandExecutor, ToolContext, ToolExecutor
from eventide.host import RuntimeHost
from eventide.models import CommandResult, ToolCall
from eventide.policy import PolicyEngine
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_runtime import settings_for


def python_command(workspace, source):
    script = workspace / "command.py"
    script.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def make_logs(tmp_path, *, limits: CommandLogLimits | None = None, secrets: tuple[str, ...] = ()):
    from eventide.store import RuntimeStore

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    store.create_run(run_id, "session_probe")
    known = secrets

    def clean(value: str) -> str:
        for secret in known:
            value = value.replace(secret, "[REDACTED]")
        return value

    logs = CommandLogs(
        store, tmp_path, limits=limits or CommandLogLimits(), secrets=secrets,
        clean=clean,
    )
    return store, logs, run_id


def logged_executor(logs: CommandLogs, **executor_kwargs) -> ToolExecutor:
    return ToolExecutor(
        {"bash": lambda **_: None},
        command_executor=LocalCommandExecutor(
            executor_kwargs.pop("timeout", 30),
            output_limit=executor_kwargs.pop("output_limit", 25_000),
            output_observer=stream_observer,
        ),
        command_logs=logs,
    )


async def run_bash(executor: ToolExecutor, run_id: str, cwd, command: str, call_id="call1"):
    return await executor.execute(
        ToolCall(call_id, "bash", {"command": command}),
        ToolContext(cwd, run_id, PolicyEngine()),
    )


@pytest.mark.asyncio
async def test_truncated_output_pages_complete_by_identity(isolated_workspace):
    store, logs, run_id = make_logs(isolated_workspace)
    executor = logged_executor(logs, timeout=30, output_limit=200)
    marker = "NEEDLE-MIDDLE-777"
    command = python_command(
        isolated_workspace,
        "import sys\n"
        "sys.stdout.write('HEAD' + 'x' * 900 + '" + marker + "' + 'y' * 900 + 'TAIL')\n",
    )
    result = await run_bash(executor, run_id, isolated_workspace, command)
    assert result.is_error is False
    # The model preview stays bounded and honest.
    assert "output bytes omitted" in result.content
    assert marker not in result.content
    reference = result.metadata["command_log"]
    assert reference["run_id"] == run_id and reference["call_id"] == "call1"
    stdout = reference["streams"]["stdout"]
    assert stdout["capture_state"] == "complete"
    assert stdout["received_bytes"] > 1800
    # Identity-bound paging reaches the middle the preview dropped.
    page = logs.page(run_id, "call1", "stdout", 0, 100_000)
    assert page["available"] is True
    assert marker in page["text"]
    assert page["total_chars"] == stdout["stored_chars"]
    offset = page["text"].find(marker)
    window = logs.page(run_id, "call1", "stdout", offset, len(marker))
    assert window["text"] == marker
    # Cross-run and cross-call reads are refused, not guessed.
    assert logs.page(f"run_{uuid.uuid4().hex[:16]}", "call1", "stdout", 0, 10)[
        "availability"] == "unknown_run"
    assert logs.page(run_id, "other_call", "stdout", 0, 10)["availability"] == "not_recorded"


@pytest.mark.asyncio
async def test_secret_split_across_chunks_never_reaches_the_log(isolated_workspace):
    secret = "sk-supersecret-value-987654321"
    store, logs, run_id = make_logs(isolated_workspace, secrets=(secret,))
    executor = logged_executor(logs)
    # Two writes with a pause force separate pipe reads across the secret.
    command = python_command(
        isolated_workspace,
        "import sys, time\n"
        f"sys.stdout.write('token=' + {secret[:12]!r})\n"
        "sys.stdout.flush()\ntime.sleep(0.2)\n"
        f"sys.stdout.write({secret[12:]!r})\nsys.stdout.flush()\ntime.sleep(0.2)\n"
        "sys.stdout.write(' done')\n",
    )
    await run_bash(executor, run_id, isolated_workspace, command)
    page = logs.page(run_id, "call1", "stdout", 0, 100_000)
    assert page["available"] is True
    assert secret not in page["text"]
    assert "token=[REDACTED] done" in page["text"]


@pytest.mark.asyncio
async def test_cancel_terminates_process_and_marks_log_interrupted(isolated_workspace):
    store, logs, run_id = make_logs(isolated_workspace)
    executor = logged_executor(logs)
    command = python_command(
        isolated_workspace,
        "import time\n"
        "for _ in range(600):\n"
        "    print('tick', flush=True); time.sleep(0.05)\n",
    )
    task = asyncio.create_task(run_bash(executor, run_id, isolated_workspace, command))
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    page = logs.page(run_id, "call1", "stdout", 0, 100_000)
    assert page["available"] is True
    assert page["capture_state"] == "interrupted"
    assert page["reason"] == "cancelled"
    stored = page["received_bytes"]
    assert stored is not None and stored > 0


@pytest.mark.asyncio
async def test_timeout_keeps_partial_log_with_honest_process_facts(isolated_workspace):
    store, logs, run_id = make_logs(isolated_workspace)
    executor = logged_executor(logs, timeout=0.8)
    command = python_command(
        isolated_workspace,
        "import time\n"
        "for _ in range(600):\n"
        "    print('tick', flush=True); time.sleep(0.05)\n",
    )
    result = await run_bash(executor, run_id, isolated_workspace, command)
    assert result.metadata["timed_out"] is True
    page = logs.page(run_id, "call1", "stdout", 0, 100_000)
    assert page["available"] is True
    assert page["capture_state"] == "complete"
    assert page["received_bytes"] == result.metadata["stdout_bytes"]
    assert "tick" in page["text"]


@pytest.mark.asyncio
async def test_stream_quota_marks_explicit_truncation(isolated_workspace):
    store, logs, run_id = make_logs(
        isolated_workspace, limits=CommandLogLimits(stream_bytes=256),
    )
    executor = logged_executor(logs)
    await run_bash(
        executor, run_id, isolated_workspace,
        python_command(isolated_workspace, "print('z' * 5000)\n"),
    )
    page = logs.page(run_id, "call1", "stdout", 0, 100_000)
    assert page["available"] is True
    assert page["capture_state"] == "quota_exceeded"
    assert page["reason"] == "stream_quota"
    assert page["stored_bytes"] == 256
    assert page["received_bytes"] > 5000


@pytest.mark.asyncio
async def test_restart_recovery_replays_recorded_and_interrupted_logs(isolated_workspace):
    store, logs, run_id = make_logs(isolated_workspace)
    executor = logged_executor(logs)
    await run_bash(
        executor, run_id, isolated_workspace,
        python_command(isolated_workspace, "print('before restart')\n"), call_id="done_call",
    )
    # A capture abandoned mid-flight models a crash between start and settle.
    crashed = logs.start(run_id, "crashed_call", secrets=(), clean=lambda v: v)
    crashed.feed("stdout", b"partial before crash")
    recovered = CommandLogs(store, isolated_workspace, secrets=(), clean=lambda v: v)
    recovered.recover()
    done = recovered.page(run_id, "done_call", "stdout", 0, 100_000)
    assert done["available"] is True and "before restart" in done["text"]
    partial = recovered.page(run_id, "crashed_call", "stdout", 0, 100_000)
    assert partial["available"] is True
    assert partial["capture_state"] == "interrupted"
    assert partial["reason"] == "process_restart"
    assert "partial before crash" in partial["text"]


@pytest.mark.asyncio
async def test_skill_script_path_captures_argv_execution(isolated_workspace):
    store, logs, run_id = make_logs(isolated_workspace)
    inner = LocalCommandExecutor(30, output_observer=stream_observer)

    async def run_skill_script(**_arguments):
        # Mirrors skill_runtime: handlers own the executor and call execute_argv.
        return await inner.execute_argv(
            ["/bin/sh", "-c", "echo skill-output-line"], isolated_workspace,
        )

    executor = ToolExecutor(
        {"run_skill_script": run_skill_script},
        tools=[{"name": "run_skill_script",
                "input_schema": {"type": "object", "properties": {}}}],
        command_logs=logs,
    )

    async def approve(_approval_id, _call, _reason):
        return True

    result = await executor.execute(
        ToolCall("call1", "run_skill_script",
                 {"name": "demo", "path": "scripts/demo.sh"}),
        ToolContext(isolated_workspace, run_id, PolicyEngine(), approve),
    )
    assert result.is_error is False
    assert result.metadata["command_log"]["streams"]["stdout"]["capture_state"] == "complete"
    page = logs.page(run_id, "call1", "stdout", 0, 100_000)
    assert "skill-output-line" in page["text"]


@pytest.mark.asyncio
async def test_rejected_call_logs_not_started_without_fake_output(isolated_workspace):
    store, logs, run_id = make_logs(isolated_workspace)
    executor = ToolExecutor({}, command_logs=logs)
    result = await executor.execute(
        ToolCall("call1", "bash", {"command": "echo hi"}),
        ToolContext(isolated_workspace, run_id, PolicyEngine()),
    )
    assert result.is_error is True
    reference = result.metadata["command_log"]
    assert reference["streams"]["stdout"]["capture_state"] == "not_started"
    page = logs.page(run_id, "call1", "stdout", 0, 100)
    assert page["available"] is False
    assert page["availability"] == "not_saved"
    assert page["capture_state"] == "not_started"


@pytest.mark.asyncio
async def test_expired_log_is_explicitly_unavailable(isolated_workspace):
    import time as time_module

    from eventide.store import RuntimeStore

    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    run_id = f"run_{uuid.uuid4().hex[:16]}"
    store.create_run(run_id, "session_probe")
    clock = [time_module.time() + 1_000.0]
    logs = CommandLogs(
        store, isolated_workspace, clock=lambda: clock[0],
        limits=CommandLogLimits(retention_seconds=100), clean=lambda v: v,
    )
    executor = logged_executor(logs)
    await run_bash(
        executor, run_id, isolated_workspace,
        python_command(isolated_workspace, "print('soon expired')\n"),
    )
    assert logs.page(run_id, "call1", "stdout", 0, 100)["available"] is True
    store.append_fact(
        store.get_run(run_id)["session_id"], "run.completed", {"steps": 0}, run_id=run_id,
    )
    clock[0] += 10_000
    logs.cleanup()
    page = logs.page(run_id, "call1", "stdout", 0, 100)
    assert page["available"] is False
    assert page["availability"] == "expired"
    # A fresh process (only replayed facts) sees the same honest state.
    fresh = CommandLogs(store, isolated_workspace, clock=lambda: clock[0], clean=lambda v: v)
    assert fresh.page(run_id, "call1", "stdout", 0, 100)["availability"] == "expired"


def test_command_result_metadata_shape():
    result = CommandResult(exit_code=0)
    assert result.metadata()["exit_code"] == 0


def test_full_stack_http_paging(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import sys\nsys.stdout.write('A' * 400 + 'NEEDLE-42' + 'B' * 400)\n",
    )
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "logme", "name": "bash",
                         "arguments": {"command": command}}]},
        {"text": "done"},
    ])
    runtime = RuntimeHost(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        run_id = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "produce log"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        wait_for_run(client, run_id)

        body = client.get(
            f"/api/runs/{run_id}/calls/logme/command-log",
            params={"stream": "stdout", "offset": 400, "limit": 9},
        )
        assert body.status_code == 200, body.text
        page = body.json()
        assert page["available"] is True
        assert page["text"] == "NEEDLE-42"
        assert page["total_chars"] == 809
        # The completed event carries the log reference the workbench binds to.
        completed = [
            event for event in runtime.store.session_events(session)
            if event["type"] == "tool.completed"
        ]
        assert completed
        reference = completed[0]["payload"]["metadata"]["command_log"]
        assert reference["artifact_id"] and reference["run_id"] == run_id
        # Unknown call and unknown run are explicit 404s.
        assert client.get(
            f"/api/runs/{run_id}/calls/other/command-log"
        ).status_code == 404
        assert client.get(
            f"/api/runs/run_{uuid.uuid4().hex[:16]}/calls/logme/command-log"
        ).status_code == 404
        # Invalid pagination is a request error, not a silent clamp.
        assert client.get(
            f"/api/runs/{run_id}/calls/logme/command-log", params={"limit": 0},
        ).status_code == 422
        assert client.get(
            f"/api/runs/{run_id}/calls/logme/command-log", params={"stream": "stdin"},
        ).status_code == 422


@pytest.mark.asyncio
async def test_host_model_reads_omitted_middle_and_loaded_key_is_redacted(isolated_workspace):
    from eventide.models import ModelResponse, RunRequest, ToolCall
    from eventide.tool_results import reader

    secret = "sk-loaded-after-constructor-abcdef123456"
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    await host.configure_provider(provider="openai", api_key=secret, base_url=None,
                                  model=settings.model, persist=True)
    await host.close()
    marker = "ACTUAL-MIDDLE-EVIDENCE"
    command = python_command(isolated_workspace,
        "import sys,time\n"
        f"sys.stdout.write('x'*30000 + {marker!r} + {secret[:15]!r})\n"
        "sys.stdout.flush(); time.sleep(0.2)\n"
        f"sys.stdout.write({secret[15:]!r} + 'y'*30000)\n")
    run_ids = []

    class Provider:
        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse(tool_calls=[ToolCall("large", "bash", {"command": command})])
            if len(self.requests) == 2:
                preview = str(request.messages)
                assert marker not in preview
                assert "Recorded command streams" in preview
                return ModelResponse(tool_calls=[ToolCall("middle", "read_tool_result", {
                    "run_id": run_ids[0], "call_id": "large", "stream": "stdout",
                    "offset": 30000, "limit": 150,
                })])
            assert marker in str(request.messages)
            assert secret not in str(request.messages)
            return ModelResponse("done")

    provider = Provider()
    host = RuntimeHost(settings, provider)
    session = host.create_session()
    run_id = "run_" + uuid.uuid4().hex[:16]
    run_ids.append(run_id)
    try:
        result = await host.run(RunRequest(
            "inspect logs", session_id=session, run_id=run_id, mode="agent",
        ))
        assert result.status == "completed"
        page = host.command_logs.page(run_id, "large", "stdout", 30000, 150)
        assert marker in page["text"] and secret not in page["text"]
        assert secret[:15] not in page["text"] and secret[15:] not in page["text"]
        other = host.create_session()
        assert "not found in this session" in reader(
            host.store, other, command_logs=host.command_logs,
        )(run_id, "large", stream="stdout")
        evidence = host.command_logs.recorded(session, run_id, "large")
        host.command_logs.expired.add(evidence["artifact_id"])
        assert "unavailable: expired" in reader(
            host.store, session, command_logs=host.command_logs,
        )(run_id, "large", stream="stdout")
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_log_disk_failure_does_not_change_command_exit(isolated_workspace, monkeypatch):
    import errno
    store, logs, run_id = make_logs(isolated_workspace)
    original_path = logs._path
    def failing_path(*args, **kwargs):
        if not kwargs["sealed"]:
            raise OSError(errno.ENOSPC, "full")
        return original_path(*args, **kwargs)
    monkeypatch.setattr(logs, "_path", failing_path)
    result = await run_bash(
        logged_executor(logs), run_id, isolated_workspace, "printf actual-output",
    )
    assert result.metadata["exit_code"] == 0 and not result.is_error
    assert "actual-output" in result.content
    assert result.metadata["command_log"]["streams"]["stdout"]["capture_state"] == "write_failed"
    assert logs.page(run_id, "call1", "stdout", 0, 100)["availability"] == "not_saved"
