"""CLI parsing and no-key help behavior."""

import asyncio
import json

import pytest

from eventide.cli import build_parser, main
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.runtime import AgentRuntime
from tests.test_runtime import settings_for


def test_cli_subcommands_parse():
    parser = build_parser()
    assert parser.parse_args(["chat"]).command == "chat"
    run = parser.parse_args(["run", "hello", "--json"])
    assert run.prompt == "hello" and run.json
    serve = parser.parse_args(["serve", "--port", "9000"])
    assert serve.port == 9000
    evaluate = parser.parse_args(["eval", "evals/smoke.yaml", "--live"])
    assert evaluate.live
    plan = parser.parse_args(["plan", "show", "session_x", "--json"])
    assert plan.command == "plan" and plan.plan_action == "show"
    assert plan.session_id == "session_x" and plan.json


def test_help_needs_no_api_key(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    assert "eventide" in capsys.readouterr().out


def test_plan_show_reports_server_task_state(isolated_workspace, monkeypatch, capsys):
    """plan show mirrors session_status facts without recomputing the projection."""
    import eventide.cli as cli

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
        runtime = AgentRuntime(settings_for(isolated_workspace), provider)
        try:
            (isolated_workspace / "data").write_text("x", encoding="utf-8")
            result = await runtime.run(RunRequest("work with a plan"))
            assert result.status == "completed"
            return result.session_id
        finally:
            await runtime.close()

    session_id = asyncio.run(make_session())

    # plan show owns its Host per invocation; each main() call opens the same
    # isolated state root, and _manage closes it in its own finally. The outer
    # close is a no-op safety net for assertion failures.
    current_host: list[AgentRuntime] = []

    def host_factory(_settings):
        host = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
        current_host.append(host)
        return host

    monkeypatch.setattr(cli, "AgentRuntime", host_factory)

    def run_plan(*argv) -> int:
        try:
            return main(["plan", "show", *argv])
        finally:
            if current_host:
                asyncio.run(current_host.pop().close())

    assert run_plan(session_id) == 0
    text = capsys.readouterr().out
    assert "completed 1 · in_progress 1 · pending 0 · blocked 0" in text
    assert "[in_progress · active] t2 Follow up" in text
    assert "summary: Read the data file." in text
    assert "evidence: read_file ok" in text
    assert "不代表验证通过" in text
    assert run_plan(session_id, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == session_id
    assert payload["active_task_id"] == "t2"
    assert payload["task_state"]["tasks"][0]["id"] == "t1"
    assert payload["task_state"]["tasks"][0]["evidence"][0]["name"] == "read_file"


def test_plan_show_without_plan(isolated_workspace, monkeypatch, capsys):
    import eventide.cli as cli

    async def make_session():
        runtime = AgentRuntime(
            settings_for(isolated_workspace), ScriptedProvider([{"text": "no plan needed"}])
        )
        try:
            result = await runtime.run(RunRequest("simple"))
            return result.session_id
        finally:
            await runtime.close()

    session_id = asyncio.run(make_session())

    current_host: list[AgentRuntime] = []

    def host_factory(_settings):
        host = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
        current_host.append(host)
        return host

    monkeypatch.setattr(cli, "AgentRuntime", host_factory)

    def run_plan(*argv) -> int:
        try:
            return main(["plan", "show", *argv])
        finally:
            if current_host:
                asyncio.run(current_host.pop().close())

    assert run_plan(session_id) == 0
    assert "尚无计划" in capsys.readouterr().out
    assert run_plan("missing") == 1
