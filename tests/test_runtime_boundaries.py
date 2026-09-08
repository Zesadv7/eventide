"""Failure contracts that protect replay and ownership."""

import asyncio
import sqlite3
import subprocess
import sys

import pytest

from nexus_agent.context_builder import ContextOverflow
from nexus_agent.host import RuntimeHost
from nexus_agent.models import ModelResponse, RunRequest, ToolCall
from nexus_agent.providers import ScriptedProvider
from nexus_agent.store import RuntimeStore
from nexus_agent.workspace import HostLease, digest, workspace_checkpoint
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def test_owner_lock_is_cross_process_and_crash_released(isolated_workspace):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from nexus_agent.workspace import HostLease; "
            "lease = HostLease(Path(sys.argv[1])); "
            "print('ready', flush=True); sys.stdin.readline()",
            str(isolated_workspace),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="already owned"):
            HostLease(isolated_workspace)
    finally:
        child.communicate("exit\n", timeout=10)
    assert child.returncode == 0
    HostLease(isolated_workspace).close()


def test_legacy_and_future_schema_rejected(isolated_workspace):
    path = isolated_workspace / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sessions (id TEXT)")
    connection.close()
    with pytest.raises(ValueError, match="Legacy"):
        RuntimeStore(path)
    store = RuntimeStore(isolated_workspace / "runtime.sqlite")
    store._connection.execute("UPDATE schema_migrations SET version=99")
    store._connection.commit()
    store.close()
    with pytest.raises(ValueError, match="Unsupported"):
        RuntimeStore(isolated_workspace / "runtime.sqlite")


async def test_partial_events_and_identity_mismatch(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        session = host.create_session()
        host.store.create_run("r", session)
        host.store.append_fact(
            session,
            "message.user",
            {
                "message": {"role": "user", "content": "not committed text"},
            },
            run_id="r",
            partial=True,
        )
        assert host.store.load_messages(session) == []
        with pytest.raises(ValueError, match="partial"):
            host.store.append_fact(session, "run.completed", {}, run_id="r", partial=True)
        with pytest.raises(ValueError, match="mismatch"):
            host.store.append_fact("wrong", "note", {}, run_id="r")
    finally:
        await host.close()


async def test_provider_native_blocks_replay(isolated_workspace):
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(
                    tool_calls=(ToolCall("read", "read_file", {"path": "absent"}),),
                    provider_items=({"type": "reasoning", "encrypted_content": "opaque"},),
                )
            return ModelResponse("done")

    host = RuntimeHost(settings_for(isolated_workspace), Provider())
    try:
        result = await host.run(RunRequest("inspect"))
        assert result.status == "completed"
        replayed = requests[1].messages[1]["content"]
        assert replayed[0]["items"][0]["encrypted_content"] == "opaque"
        assert replayed[1]["id"] == "read"
    finally:
        await host.close()


async def test_summary_failure_uses_valid_checkpoint_or_overflows(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        session = host.create_session()
        host.store.create_run("r", session)
        host.store.append_message(session, {"role": "user", "content": "x" * 2000})
        host.store.finish_run("r", status="completed", output="done")
        events = host.store.session_events(session)
        host.store.save_checkpoint(
            session, events[-1]["session_seq"], digest(events), "valid", "scripted", "scripted"
        )
        host.store.create_run("next", session)
        host.store.append_message(session, {"role": "user", "content": "y" * 100})
        host.store.finish_run("next", status="completed", output="done")
        args = dict(provider_name="scripted", model="scripted", budget=500, force=True)
        messages, _ = await host.context_builder.build(
            session,
            provider=ScriptedProvider([{"error": "summary failure"}]),
            **args,
        )
        assert "valid" in messages[0]["content"]
        with pytest.raises(ContextOverflow, match="context_overflow"):
            await host.context_builder.build(
                session,
                provider=ScriptedProvider([{"text": "z" * 1000}]),
                provider_name="scripted",
                model="scripted",
                budget=20,
            )
        assert host.store.session_events(session)[: len(events)] == events
    finally:
        await host.close()


async def test_overflow_reports_the_active_turn_not_a_missing_checkpoint(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        session = host.create_session()
        host.store.create_run("r", session)
        host.store.append_message(session, {"role": "user", "content": "x" * 4000})
        host.store.finish_run("r", status="completed", output="done")
        host.store.create_run("next", session)
        host.store.append_message(session, {"role": "user", "content": "y" * 4000})
        with pytest.raises(ContextOverflow) as excinfo:
            await host.context_builder.build(
                session,
                provider=ScriptedProvider([{"text": "z" * 4000}]),
                provider_name="scripted",
                model="scripted",
                budget=1000,
            )
        message = str(excinfo.value)
        assert "context_overflow" in message
        assert "no usable checkpoint" not in message
        assert "1000 character budget" in message
    finally:
        await host.close()


async def test_no_post_write_checkpoint_blocks_continue(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        ws = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=ws.workspace_id)
        host.store.create_run("r", session)
        host.store.append_fact(
            session,
            "workspace.checkpoint",
            {
                "checkpoint": workspace_checkpoint(repo),
            },
            run_id="r",
        )
        host.store.append_fact(
            session,
            "tool.completed",
            {
                "call_id": "write",
                "name": "write_file",
                "content": "written",
                "is_error": False,
            },
            run_id="r",
        )
        host.store.append_event("r", "run.interrupted", {})
        with pytest.raises(ValueError, match="post-tool checkpoint"):
            await host.continue_session(session)
        assert host.store.latest_run(session)["id"] == "r"
    finally:
        await host.close()


async def test_observer_failure_and_approval_timeout_are_durable(isolated_workspace):
    class BrokenSink:
        async def emit(self, event):
            raise OSError("disconnected")

    async def never_approve(*args):
        await asyncio.Future()

    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "d", "name": "bash", "arguments": {"command": "rm absent"}}]},
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace, approval_timeout=0.01), provider)
    try:
        result = await host.run(RunRequest("test"), BrokenSink(), never_approve)
        assert result.status == "completed"
        events = host.store.run_events(result.run_id)
        decision = next(e for e in events if e["type"] == "approval.resolved")
        assert decision["payload"]["approved"] is False
        assert len([e for e in events if e["type"] == "run.completed"]) == 1
    finally:
        await host.close()
