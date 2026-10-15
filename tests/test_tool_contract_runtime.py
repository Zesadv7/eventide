"""Production catalogs and bounded argument correction reuse the main loop."""

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def call(identity, name, **arguments):
    return {"id": identity, "name": name, "arguments": arguments}


async def test_invalid_write_can_be_corrected_once_without_replaying(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [call("bad", "write_file", path="new.txt", content=123)]},
            {"tool_calls": [call("good", "write_file", path="new.txt", content="correct")]},
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work", mode="agent"))
        assert result.status == "completed"
        assert (isolated_workspace / "new.txt").read_text() == "correct"
        results = [
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "tool.completed"
        ]
        assert results[0]["metadata"]["code"] == "invalid_arguments"
        assert results[1]["is_error"] is False
        assert provider.requests[0].tools[0]["input_schema"]["additionalProperties"] is False
    finally:
        await host.close()


async def test_second_invalid_turn_stops_without_replaying_prior_write(isolated_workspace):
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    call("write", "write_file", path="kept.txt", content="once"),
                    call("bad1", "read_file", path="kept.txt", offset="0"),
                ]
            },
            {"tool_calls": [call("bad2", "read_file", path="kept.txt", offset="0")]},
            {"tool_calls": [call("never", "write_file", path="kept.txt", content="wrong")]},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work", mode="agent"))
        assert result.status == "failed"
        assert "argument repair failed" in result.output
        assert len(provider.requests) == 2
        assert (isolated_workspace / "kept.txt").read_text() == "once"
        assert (
            len(
                [
                    e
                    for e in host.store.run_events(result.run_id)
                    if e["type"] == "tool.completed" and e["payload"]["name"] == "write_file"
                ]
            )
            == 1
        )
    finally:
        await host.close()


async def test_dynamic_tool_contract_is_registered_in_production(isolated_workspace):
    seen = []
    name = "mcp__fixture__count"
    tools = [
        {
            "name": name,
            "description": "count",
            "input_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
        }
    ]
    host = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider(
            [
                {"tool_calls": [call("wrong", name, count="2")]},
                {"tool_calls": [call("right", name, count=2)]},
                {"text": "done"},
            ]
        ),
        tools=tools,
        handlers={name: lambda count: seen.append(count) or "ok"},
    )
    try:
        result = await host.run(RunRequest("work", mode="agent"))
        assert result.status == "completed"
        assert seen == [2]
    finally:
        await host.close()
