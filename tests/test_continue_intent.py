"""Continue preserves read-only intent without extending automatic approvals."""

from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.workspace import workspace_checkpoint
from tests.test_host import make_repo
from tests.test_runtime import settings_for


async def test_plan_and_selected_model_survive_reopen(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state", max_steps=1)
    first = RuntimeHost(
        settings,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "read",
                            "name": "read_file",
                            "arguments": {"path": "missing"},
                        }
                    ]
                }
            ]
        ),
    )
    session = first.create_session()
    parked = await first.run(
        RunRequest("read-only investigation", session, mode="plan", model="chosen")
    )
    assert parked.status == "interrupted"
    await first.close()
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "write",
                        "name": "write_file",
                        "arguments": {
                            "path": "forbidden.txt",
                            "content": "must not be written",
                        },
                    }
                ]
            },
            {"text": "read-only work complete"},
        ]
    )
    host = RuntimeHost(replace(settings, max_steps=2, model="other-default"), provider)
    try:
        result = await host.continue_session(session)
        assert result.status == "completed"
        assert all(request.model == "chosen" for request in provider.requests)
        assert all(not any(t["name"] == "write_file" for t in r.tools) for r in provider.requests)
        assert not (repo / "forbidden.txt").exists()
        started = next(
            e for e in host.store.run_events(result.run_id) if e["type"] == "run.started"
        )
        assert started["payload"]["mode"] == "plan"
        assert host.settings.model == "other-default"
    finally:
        await host.close()


async def test_agent_auto_approval_does_not_extend_to_continue(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state", max_steps=1)
    calls = []
    tool = {
        "name": "mcp__fixture__risky",
        "description": "fixture",
        "input_schema": {"type": "object"},
    }
    host = RuntimeHost(
        settings,
        ScriptedProvider(
            [
                {"tool_calls": [{"id": "first", "name": "mcp__fixture__risky", "arguments": {}}]},
                {"tool_calls": [{"id": "second", "name": "mcp__fixture__risky", "arguments": {}}]},
                {"text": "done"},
            ]
        ),
        tools=[tool],
        handlers={"mcp__fixture__risky": lambda: calls.append("called") or "ok"},
    )
    try:
        session = host.create_session()
        assert (await host.run(RunRequest("work", session, mode="agent"))).status == "interrupted"
        host.settings = replace(host.settings, max_steps=2)
        result = await host.continue_session(session)
        assert result.status == "completed"
        assert calls == ["called"]
        decisions = [
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "approval.resolved"
        ]
        assert decisions and decisions[0]["approved"] is False
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["provider", "endpoint"])
async def test_model_does_not_cross_provider_or_endpoint_identity(isolated_workspace, change):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state", max_steps=1)
    first = RuntimeHost(
        settings,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "read",
                            "name": "read_file",
                            "arguments": {"path": "missing"},
                        }
                    ]
                }
            ]
        ),
    )
    session = first.create_session()
    await first.run(RunRequest("inspect", session, mode="plan", model="old-backend-model"))
    await first.close()
    updates = (
        {"provider": "anthropic"} if change == "provider" else {"base_url": "https://other.invalid"}
    )
    provider = ScriptedProvider([{"text": "done"}])
    host = RuntimeHost(replace(settings, model="new-backend-model", **updates), provider)
    try:
        await host.continue_session(session)
        assert provider.requests[0].model == "new-backend-model"
        assert not any(tool["name"] == "bash" for tool in provider.requests[0].tools)
    finally:
        await host.close()


async def test_pending_continue_cancel_closes_ancestral_read_calls(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    session = host.create_session()
    host.store.create_run("source", session)
    host.store.append_event(
        "source",
        "run.started",
        {
            "mode": "plan",
            "provider": "scripted",
            "model": "saved",
        },
    )
    host.store.append_event(
        "source",
        "message.user",
        {
            "message": {"role": "user", "content": "continue this goal"},
        },
    )
    host.store.append_event(
        "source",
        "model.response",
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read",
                        "name": "read_file",
                        "input": {"path": "missing"},
                    }
                ],
            }
        },
    )
    host.store.append_event(
        "source",
        "tool.prepared",
        {
            "call_id": "read",
            "name": "read_file",
            "readonly": True,
        },
    )
    host.store.append_event(
        "source", "workspace.checkpoint", {"checkpoint": workspace_checkpoint(repo)}
    )
    host.store.append_event("source", "run.interrupted", {"error": "fixture"})
    await host.admit_continue(session, "pending-continuation")
    await host.cancel_run("pending-continuation")
    assert host.store.load_messages(session)[-1]["content"][0]["is_error"] is True
    await host.close()
    provider = ScriptedProvider([{"text": "resumed"}])
    reopened = RuntimeHost(settings, provider)
    try:
        result = await reopened.continue_session(session)
        assert result.status == "completed"
        assert provider.requests[0].model == "saved"
        events = reopened.store.session_events(session)
        assert len([e for e in events if e["type"] == "tool.abandoned"]) == 1
        assert len([e for e in events if e["type"] == "message.user"]) == 1
    finally:
        await reopened.close()
