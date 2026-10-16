"""Developer interfaces retain trace privacy and task evidence without a terminal product."""

import asyncio
import json

import pytest

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.telemetry import build_run_trace, collect_trace_events
from tests.test_runtime import settings_for


async def test_trace_python_interface_keeps_private_bodies_out(isolated_workspace):
    host = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "private output body"}])
    )
    try:
        result = await host.run(RunRequest("private user goal"))
        report = build_run_trace(
            collect_trace_events(host.store, result.run_id), run_id=result.run_id
        )
        encoded = json.dumps(report)
        assert "private user goal" not in encoded and "private output body" not in encoded
        assert report["spans"]
    finally:
        await host.close()


def test_python_status_retains_plan_evidence(isolated_workspace):
    async def make_session():
        # Ids are allocated by the Runtime, so the first submission has none and the
        # second echoes t1 back while the renamed-in-place item matches by content.
        initial = [
            {"content": "Inspect", "status": "in_progress"},
            {"content": "Follow up", "status": "pending"},
        ]
        settled = [
            {
                "id": "t1",
                "content": "Inspect",
                "status": "completed",
                "summary": "Read the data file.",
            },
            {"content": "Follow up", "status": "in_progress"},
        ]
        provider = ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {"id": "plan", "name": "todo_write", "arguments": {"todos": initial}}
                    ]
                },
                {
                    "tool_calls": [
                        {"id": "read", "name": "read_file", "arguments": {"path": "data"}}
                    ]
                },
                {
                    "tool_calls": [
                        {"id": "done", "name": "todo_write", "arguments": {"todos": settled}}
                    ]
                },
                {"text": "done"},
            ]
        )
        runtime = RuntimeHost(settings_for(isolated_workspace), provider)
        try:
            (isolated_workspace / "data").write_text("x", encoding="utf-8")
            result = await runtime.run(RunRequest("work with a plan"))
            assert result.status == "completed"
            return result.session_id
        finally:
            await runtime.close()

    session_id = asyncio.run(make_session())
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        payload = host.session_status(session_id)
        assert payload["session_id"] == session_id
        assert payload["active_task_id"] == "t2"
        tasks = payload["task_state"]["tasks"]
        assert tasks[0]["id"] == "t1" and tasks[0]["status"] == "completed"
        assert tasks[0]["summary"] == "Read the data file."
        assert tasks[0]["evidence"][0]["name"] == "read_file"
        assert tasks[1]["status"] == "in_progress"
    finally:
        asyncio.run(host.close())


def test_python_status_without_plan(isolated_workspace):
    async def make_session():
        runtime = RuntimeHost(
            settings_for(isolated_workspace), ScriptedProvider([{"text": "no plan needed"}])
        )
        try:
            result = await runtime.run(RunRequest("simple"))
            return result.session_id
        finally:
            await runtime.close()

    session_id = asyncio.run(make_session())
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        payload = host.session_status(session_id)
        assert payload["task_plan"] == []
        with pytest.raises(ValueError, match="Session not found"):
            host.session_status("missing")
    finally:
        asyncio.run(host.close())
