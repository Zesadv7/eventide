"""Workspace ownership, recovery, and projection integration contracts."""

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, WorkspaceTarget
from eventide.providers import ScriptedProvider
from eventide.workspace import workspace_checkpoint
from tests.test_runtime import settings_for


def make_repo(path: Path) -> Path:
    path.mkdir()
    for args in (
        ["init"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


async def test_host_project_binding_and_lease(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    child = repo / "src"
    child.mkdir()
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        workspace = host.resolve_or_register_workspace(child)
        assert workspace.path == str(repo.resolve())
        assert host.resolve_or_register_workspace(repo) == workspace
        assert host.resolve_or_register_workspace(
            WorkspaceTarget(workspace_id=workspace.workspace_id)
        )
        with pytest.raises(ValueError, match="exactly one"):
            host.resolve_or_register_workspace(WorkspaceTarget())
        with pytest.raises(RuntimeError, match="already owned"):
            RuntimeHost(settings_for(isolated_workspace))
        session = host.create_session(workspace_id=workspace.workspace_id)
        result = await host.run(RunRequest("hello", session))
        assert host.session_status(session)["status"] == "completed"
        assert result.turn_id
        assert {t["name"] for t in host.tools} == {
            "bash",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "compact",
        }
    finally:
        await host.close()
    await host.close()


async def test_workspace_serialization_and_cross_workspace_parallelism(isolated_workspace):
    entered = asyncio.Queue()
    release = asyncio.Event()

    class BlockingProvider:
        async def complete(self, request):
            await entered.put(request.messages[-1]["content"])
            await release.wait()
            return ModelResponse("done")

    host = RuntimeHost(settings_for(isolated_workspace), BlockingProvider())
    other = isolated_workspace / "other"
    other.mkdir()
    ws = host.resolve_or_register_workspace(other)
    first = host.create_session()
    second = host.create_session()
    third = host.create_session(workspace_id=ws.workspace_id)
    tasks = [
        asyncio.create_task(host.run(RunRequest(text, session)))
        for text, session in [("first", first), ("second", second), ("third", third)]
    ]
    try:
        observed = {await asyncio.wait_for(entered.get(), 5) for _ in range(2)}
        assert observed == {"first", "third"}
        assert entered.empty()
        release.set()
        await asyncio.gather(*tasks)
        assert await entered.get() == "second"
    finally:
        release.set()
        await host.close()


async def interrupted_host(root: Path, prepared: str | None = None) -> tuple[RuntimeHost, str, str]:
    repo = make_repo(root / "repo")
    settings = replace(settings_for(repo), state_dir=root / "state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    session = host.create_session()
    host.store.create_run("crashed", session)
    host.store.append_fact(
        session,
        "message.user",
        {
            "message": {"role": "user", "content": "original task"},
        },
        run_id="crashed",
    )
    host.store.append_fact(
        session,
        "workspace.checkpoint",
        {
            "checkpoint": workspace_checkpoint(repo),
        },
        run_id="crashed",
    )
    if prepared:
        host.store.append_fact(
            session,
            "model.response",
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call", "name": prepared, "input": {}},
                    ],
                },
            },
            run_id="crashed",
        )
        host.store.append_fact(
            session,
            "tool.prepared",
            {
                "call_id": "call",
                "name": prepared,
                "readonly": prepared == "read_file",
            },
            run_id="crashed",
        )
    await host.close()
    host = RuntimeHost(settings, ScriptedProvider([{"text": "continued"}]))
    return host, session, str(repo)


@pytest.mark.parametrize("prepared", [None, "read_file"])
async def test_continue_new_identity_without_duplicate_user(isolated_workspace, prepared):
    host, session, _ = await interrupted_host(isolated_workspace, prepared)
    try:
        assert host.store.get_run("crashed")["status"] == "interrupted"
        assert host.session_status(session)["status"] == "parked"
        result = await host.continue_session(session)
        assert result.status == "completed"
        assert result.continuation_of == "crashed" and result.run_id != "crashed"
        events = host.store.session_events(session)
        assert len([e for e in events if e["type"] == "message.user"]) == 1
        if prepared:
            assert any(e["type"] == "tool.abandoned" for e in events)
        assert host.store.load_messages(session)[-1]["role"] == "assistant"
        with pytest.raises(ValueError, match="Only an interrupted"):
            await host.continue_session(session)
    finally:
        await host.close()


@pytest.mark.parametrize("prepared", ["write_file", "edit_file", "bash", "mcp__demo__echo"])
async def test_continue_unknown_mutation_stays_parked(isolated_workspace, prepared):
    host, session, _ = await interrupted_host(isolated_workspace, prepared)
    try:
        with pytest.raises(ValueError, match="Unknown tool outcome"):
            await host.continue_session(session)
        with pytest.raises(ValueError, match="parked"):
            await host.run(RunRequest("bypass recovery", session))
        assert host.store.latest_run(session)["id"] == "crashed"
    finally:
        await host.close()


async def test_continue_changed_workspace_rejected(isolated_workspace):
    host, session, repo = await interrupted_host(isolated_workspace)
    try:
        Path(repo, "user-change.txt").write_text("changed", encoding="utf-8")
        with pytest.raises(ValueError, match="Workspace changed"):
            await host.continue_session(session)
        assert host.session_status(session)["status"] == "parked"
    finally:
        await host.close()


async def test_abandon_interruption_unlocks_parked_session(isolated_workspace):
    host, session, repo = await interrupted_host(isolated_workspace, "write_file")
    try:
        Path(repo, "user-change.txt").write_text("changed", encoding="utf-8")
        with pytest.raises(ValueError, match="Workspace changed"):
            await host.continue_session(session)

        abandoned = await host.abandon_interruption(session)
        assert abandoned.status == "completed"
        assert abandoned.continuation_of == "crashed"
        assert host.session_status(session)["status"] == "completed"
        assert any(
            event["type"] == "recovery.abandoned"
            for event in host.store.session_events(session)
        )
        assert "outcome was not assumed" in json.dumps(host.store.load_messages(session))

        resumed = await host.run(RunRequest("start safely from current state", session))
        assert resumed.status == "completed"
        assert resumed.output == "continued"
    finally:
        await host.close()


async def test_event_and_model_normalization_are_identical(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "r", "name": "read_file", "arguments": {"path": "data"}}]},
            {"text": "done"},
        ]
    )
    (isolated_workspace / "data").write_text("private-key" + "x" * 8000, encoding="utf-8")
    (isolated_workspace / "AGENTS.md").write_text("Use pytest.", encoding="utf-8")
    host = RuntimeHost(settings_for(isolated_workspace, api_key="private-key"), provider)
    try:
        result = await host.run(RunRequest("read"))
        events = host.store.run_events(result.run_id)
        completed = next(e for e in events if e["type"] == "tool.completed")
        result_block = provider.requests[-1].messages[-1]["content"][0]
        assert result_block["content"] == completed["payload"]["content"]
        assert "private-key" not in json.dumps(events)
        assert "Use pytest." in provider.requests[0].system
        assert events.index(completed) > next(
            i for i, e in enumerate(events) if e["type"] == "tool.prepared"
        )
    finally:
        await host.close()


async def test_execution_data_is_not_truncated_or_redacted(isolated_workspace):
    long_prompt = "p" * 5_000
    long_file = "x" * 5_000
    long_answer = "y" * 5_000
    captured = []
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "write",
                        "name": "write_file",
                        "arguments": {"path": "long.txt", "content": long_file},
                    },
                    {
                        "id": "capture",
                        "name": "capture",
                        "arguments": {
                            "token": "business-token",
                            "max_tokens": 123,
                            "content": long_file,
                        },
                    },
                ]
            },
            {"text": long_answer},
        ]
    )
    host = RuntimeHost(
        replace(settings_for(isolated_workspace), context_limit=100_000),
        provider,
    )
    host.tools.append(
        {
            "name": "capture",
            "description": "Capture arguments for a runtime contract test.",
            "input_schema": {"type": "object", "properties": {}},
        }
    )

    def capture(**arguments):
        captured.append(arguments)
        return "captured"

    host.handlers["capture"] = capture
    try:
        result = await host.run(RunRequest(long_prompt))
        assert result.status == "completed"
        assert result.output == long_answer
        assert provider.requests[0].messages[0]["content"] == long_prompt
        assert (isolated_workspace / "long.txt").read_text(encoding="utf-8") == long_file
        assert captured == [
            {"token": "business-token", "max_tokens": 123, "content": long_file}
        ]

        response_event = next(
            event
            for event in host.store.session_events(result.session_id)
            if event["type"] == "model.response"
        )
        capture_block = next(
            block
            for block in response_event["payload"]["message"]["content"]
            if block.get("id") == "capture"
        )
        assert capture_block["input"]["token"] == "[REDACTED]"
        assert capture_block["input"]["max_tokens"] == "[REDACTED]"
        assert capture_block["input"]["content"] == long_file
    finally:
        await host.close()


async def test_long_turn_folds_older_tool_results_instead_of_failing(isolated_workspace):
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {"id": f"r{index}", "name": "read_file", "arguments": {"path": f"d{index}"}}
                ]
            }
            for index in range(5)
        ]
        + [{"text": "done"}]
    )
    for index in range(5):
        (isolated_workspace / f"d{index}").write_text("y" * 3000, encoding="utf-8")
    host = RuntimeHost(settings_for(isolated_workspace, context_limit=12000), provider)
    try:
        result = await host.run(RunRequest("read"))
        assert result.status == "completed"
        events = host.store.run_events(result.run_id)
        trimmed = next(e for e in events if e["type"] == "context.trimmed")
        assert trimmed["payload"]["call_ids"]
        blocks = [
            block
            for message in provider.requests[-1].messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert any("folded" in str(block["content"]) for block in blocks)
    finally:
        await host.close()


async def test_step_budget_parks_the_session_and_continue_resumes(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "r1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            {"tool_calls": [{"id": "r2", "name": "read_file", "arguments": {"path": "README.md"}}]},
            {"text": "resumed"},
        ]
    )
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state", max_steps=2)
    host = RuntimeHost(settings, provider)
    try:
        workspace = host.resolve_or_register_workspace(repo)
        session = host.create_session(workspace_id=workspace.workspace_id)
        result = await host.run(RunRequest("read", session))
        assert result.status == "interrupted"
        assert "Maximum agent steps exceeded" in result.output
        assert host.session_status(session)["status"] == "parked"
        assert host.store.run_events(result.run_id)[-1]["type"] == "run.interrupted"
        continued = await host.continue_session(session)
        assert continued.status == "completed"
        assert continued.output == "resumed"
    finally:
        await host.close()


async def test_truncated_final_answer_fails_instead_of_reporting_success(isolated_workspace):
    provider = ScriptedProvider([{"text": "half an answer", "stop_reason": "max_tokens"}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("write a long answer"))
        assert result.status == "failed"
        assert "truncated" in result.output
        assert "EVENTIDE_MAX_TOKENS" in result.output
    finally:
        await host.close()


async def test_cancel_and_close_release_owner(isolated_workspace):
    entered = asyncio.Event()

    class WaitingProvider:
        async def complete(self, request):
            entered.set()
            await asyncio.Future()

    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, WaitingProvider())
    session = host.create_session()
    task = asyncio.create_task(host.run(RunRequest("wait", session)))
    await asyncio.wait_for(entered.wait(), 5)
    await host.close()
    assert task.cancelled()
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert reopened.session_status(session)["status"] == "parked"
        with pytest.raises(ValueError, match="Git workspace"):
            await reopened.continue_session(session)
    finally:
        await reopened.close()


async def test_cancel_run_by_id_persists_interruption(isolated_workspace):
    entered = asyncio.Event()

    class WaitingProvider:
        async def complete(self, _request):
            entered.set()
            await asyncio.Future()

    host = RuntimeHost(settings_for(isolated_workspace), WaitingProvider())
    task = asyncio.create_task(
        host.run(RunRequest("wait", run_id="run_cancel_by_id"))
    )
    try:
        await entered.wait()
        record = await host.cancel_run("run_cancel_by_id")
        assert record["status"] == "interrupted"
        assert "cancelled" in record["error"].lower()
        assert task.cancelled()
    finally:
        await host.close()


async def test_model_request_timeout_fails_with_durable_retries(isolated_workspace):
    class WaitingProvider:
        async def complete(self, _request):
            await asyncio.sleep(60)

    settings = replace(settings_for(isolated_workspace), model_timeout=0.01)
    host = RuntimeHost(settings, WaitingProvider())
    try:
        result = await host.run(RunRequest("wait"))
        assert result.status == "failed"
        assert "timed out" in result.output
        retries = [
            event
            for event in host.store.run_events(result.run_id)
            if event["type"] == "model.retry"
        ]
        assert len(retries) == 3
    finally:
        await host.close()


async def test_mcp_close_failure_does_not_overwrite_success(isolated_workspace, monkeypatch):
    class BrokenCloseManager:
        tools = []
        handlers = {}
        readonly_tools = set()

        @staticmethod
        def load_configs(_path):
            return []

        async def connect_all(self, _configs, *, timeout=None):
            return []

        async def close(self):
            raise RuntimeError("close broke")

    monkeypatch.setattr("eventide.host.MCPManager", BrokenCloseManager)
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        assert any(
            event["type"] == "mcp.close_failed"
            for event in host.store.run_events(result.run_id)
        )
    finally:
        await host.close()


async def test_context_checkpoint_survives_reopen_and_rebuild(isolated_workspace):
    provider = ScriptedProvider([{"text": "summary"}, {"text": "done"}])
    settings = settings_for(isolated_workspace, context_limit=2_200)
    host = RuntimeHost(settings, provider)
    session = host.create_session()
    host.store.create_run("previous", session)
    host.store.append_message(session, {"role": "user", "content": "x" * 2000})
    host.store.finish_run("previous", status="completed", output="ok")
    result = await host.run(RunRequest("next", session))
    assert result.status == "completed"
    original = host.store.session_events(session)
    assert host.store.checkpoints(session)
    host.store._connection.execute("UPDATE context_checkpoints SET source_digest='invalid'")
    host.store._connection.commit()
    await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([{"text": "new summary"}, {"text": "done"}]))
    try:
        assert (await reopened.run(RunRequest("again", session))).status == "completed"
        assert reopened.store.session_events(session)[: len(original)] == original
        assert len(reopened.store.checkpoints(session)) == 2
    finally:
        await reopened.close()


async def test_truncated_context_summary_is_never_persisted(isolated_workspace):
    provider = ScriptedProvider(
        [{"text": "partial summary", "stop_reason": "max_tokens"}]
    )
    settings = settings_for(isolated_workspace, context_limit=2_200)
    host = RuntimeHost(settings, provider)
    session = host.create_session()
    host.store.create_run("previous", session)
    host.store.append_message(session, {"role": "user", "content": "x" * 2_000})
    host.store.finish_run("previous", status="completed", output="ok")
    try:
        result = await host.run(RunRequest("next", session))
        assert result.status == "failed"
        assert "summary truncated" in result.output
        assert host.store.checkpoints(session) == []
    finally:
        await host.close()


async def test_context_budget_includes_system_and_tool_catalog(isolated_workspace):
    provider = ScriptedProvider([{"text": "must not be called"}])
    settings = settings_for(isolated_workspace, context_limit=500)
    host = RuntimeHost(settings, provider)
    try:
        result = await host.run(RunRequest("small prompt"))
        assert result.status == "failed"
        assert "fixed system/tool characters" in result.output
        assert provider.requests == []
    finally:
        await host.close()
