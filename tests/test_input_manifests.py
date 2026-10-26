import json

import httpx

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.input_composition import measure_input_composition
from eventide.models import ModelResponse, RunRequest
from eventide.providers import ProviderError
from eventide.workspace import digest
from tests.test_runtime import settings_for


async def test_real_retry_inputs_have_distinct_attempts_exact_hashes_and_rule_bounds(
    isolated_workspace,
):
    (isolated_workspace / "AGENTS.md").write_text("rule-private " * 400)

    class Provider:
        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ProviderError("disconnected", retryable=True, category="network")
            return ModelResponse("done")

        async def close(self):
            pass

    provider = Provider()
    host = RuntimeHost(settings_for(isolated_workspace, model_retry_base_delay=0.001), provider)
    try:
        result = await host.run(RunRequest("private user text"))
        assert result.status == "completed"
        transport = httpx.ASGITransport(app=create_app(host))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/runs/{result.run_id}/input-manifests")
            assert response.status_code == 200
            payload = response.json()
            entries = payload["entries"]
            assert len(entries) == len(provider.requests) == 2
            assert entries[0]["attempt_id"] != entries[1]["attempt_id"]
            assert entries[0]["request_id"] == entries[1]["request_id"]
            for entry, request in zip(entries, provider.requests, strict=True):
                assert entry["system_hash"] == digest(request.system)
                assert entry["messages_hash"] == digest(request.messages)
                assert entry["tools_hash"] == digest(request.tools)
                assert entry["composition"] == measure_input_composition(request)
                assert entry["instructions"]["truncated"]
                assert entry["instructions"]["loaded_characters"] == 4000
            assert "rule-private" not in json.dumps(payload)
            assert "private user text" not in json.dumps(payload)
            newest = (await client.get(
                f"/api/runs/{result.run_id}/input-manifests?limit=1",
            )).json()
            older = (await client.get(
                f"/api/runs/{result.run_id}/input-manifests",
                params={"before": newest["next_before"], "limit": 1},
            )).json()
            assert newest["entries"] == entries[1:]
            assert older["entries"] == entries[:1]
            assert older["next_before"] is None
            assert (await client.get("/api/runs/missing/input-manifests")).status_code == 404
            assert (await client.get(
                f"/api/runs/{result.run_id}/input-manifests?limit=101",
            )).status_code == 422
    finally:
        await host.close()
