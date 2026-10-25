from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


async def test_loaded_schema_is_advertised_on_next_real_host_request(
    isolated_workspace, monkeypatch
):
    name = "mcp__demo__read"

    class Manager:
        tools = [
            {
                "name": name,
                "description": "Read demo",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ]
        handlers = {name: lambda: "demo"}
        readonly_tools = {name}

        @staticmethod
        def load_configs(path):
            return []

        async def connect_all(self, configs, *, timeout):
            return []

        async def close(self):
            pass

    monkeypatch.setattr("eventide.host.MCPManager", Manager)
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "load1", "name": "tool_load", "arguments": {"name": name}}]},
            {"tool_calls": [{"id": "read1", "name": name, "arguments": {}}]},
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("read"))
        assert result.status == "completed"
        assert name not in {t["name"] for t in provider.requests[0].tools}
        assert name in {t["name"] for t in provider.requests[1].tools}
        prepared = [
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "tool.prepared"
        ]
        assert all(p["readonly"] for p in prepared)
    finally:
        await host.close()
