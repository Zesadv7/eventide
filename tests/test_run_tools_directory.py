"""MK-16 slice 2: run-frozen tool directory, trusted load facts, HTTP projection.

The directory a run sees is frozen once before the first model request into the same run
EventLog. Projection and HTTP views read only those persisted facts, so a
reopened store or a changed MCP server can never rewrite an old run's
history, and a run without frozen facts says so instead of re-enumerating
the current MCP manager.
"""

from typing import Any

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.tool_projection import project_run_tools
from tests.test_api import wait_for_run
from tests.test_host import make_repo
from tests.test_runtime import settings_for

ALPHA = "mcp__demo__alpha"
BETA = "mcp__demo__beta"
ALPHA_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}
BETA_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "additionalProperties": False,
}


class FakeManager:
    """Stand-in for MCPManager: fixed tools, execution counters, no transport."""

    def __init__(self, tools: list[dict[str, Any]], readonly: set[str]):
        self.tools = tools
        self.readonly_tools = readonly
        self.handlers = {ALPHA: self._run_alpha, BETA: self._run_beta}
        self.alpha_calls: list[dict[str, Any]] = []
        self.beta_calls: list[dict[str, Any]] = []

    async def connect_all(self, configs, *, timeout=None):
        return []

    async def close(self):
        return None

    @staticmethod
    def load_configs(_path):
        return []

    async def _run_alpha(self, **arguments):
        self.alpha_calls.append(arguments)
        return f"alpha executed with {arguments}"

    async def _run_beta(self, **arguments):
        self.beta_calls.append(arguments)
        return f"beta executed with {arguments}"


def _start_run(client: TestClient, workspace_id: str, key: str, prompt: str) -> str:
    session = client.post(
        "/api/sessions", headers={"Idempotency-Key": f"{key}-session"},
        json={"workspace_id": workspace_id},
    ).json()["session_id"]
    accepted = client.post(
        f"/api/sessions/{session}/runs", json={"prompt": prompt},
        headers={"Idempotency-Key": f"{key}-run"},
    )
    return accepted.json()["run_id"]


def test_discovery_load_updates_next_request_and_freezes_trusted_facts(
    isolated_workspace, monkeypatch
):
    repo = make_repo(isolated_workspace / "repo")
    manager = FakeManager(
        [
            {"name": ALPHA, "description": "Reads demo paths", "input_schema": ALPHA_SCHEMA},
            {"name": BETA, "description": "Writes demo values", "input_schema": BETA_SCHEMA},
        ],
        readonly={ALPHA},
    )
    monkeypatch.setattr("eventide.host.MCPManager", lambda: manager)
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "d1", "name": "tool_discover",
                         "arguments": {"query": "alpha"}}]},
        {"tool_calls": [{"id": "l1", "name": "tool_load",
                         "arguments": {"name": ALPHA}}]},
        {"tool_calls": [{"id": "a1", "name": ALPHA, "arguments": {"path": "README"}}]},
        {"text": "done"},
    ])
    host = RuntimeHost(settings_for(repo), provider)
    with TestClient(create_app(host)) as client:
        workspace_id = host.store.list_workspaces()[0].workspace_id
        run_id = _start_run(client, workspace_id, "main", "use the demo reader")
        assert wait_for_run(client, run_id)["status"] == "completed"

        # Round 1 carried only discovery verbs, not the unloaded on-demand schema.
        first_names = [tool["name"] for tool in provider.requests[0].tools]
        assert "tool_discover" in first_names and "tool_load" in first_names
        assert ALPHA not in first_names
        # After tool_load, the next request carries the frozen schema verbatim.
        third = {tool["name"]: tool for tool in provider.requests[2].tools}
        assert third[ALPHA]["input_schema"] == ALPHA_SCHEMA
        assert manager.alpha_calls == [{"path": "README"}]
        assert manager.beta_calls == []

        events = host.store.run_events(run_id)
        frozen = [e for e in events if e["type"] == "tool.catalog.frozen"]
        assert len(frozen) == 1
        catalog = frozen[0]["payload"]
        assert catalog["catalog_version"]
        entries = {entry["name"]: entry for entry in catalog["entries"]}
        assert entries[ALPHA]["input_schema"] == ALPHA_SCHEMA
        assert entries[ALPHA]["provided"] is False
        assert entries[ALPHA]["effect"] == "readonly"
        assert entries[BETA]["effect"] == "effect"
        loaded = [e for e in events if e["type"] == "tool.loaded"]
        assert len(loaded) == 1
        assert loaded[0]["payload"]["tool"] == ALPHA
        assert loaded[0]["payload"]["catalog_version"] == catalog["catalog_version"]
        assert loaded[0]["payload"]["schema_digest"] == entries[ALPHA]["version"]

        projection = project_run_tools(events, run_id=run_id)
        assert projection["catalog_frozen"] is True and projection["stale_receipts"] == 0
        by_name = {entry["name"]: entry for entry in projection["entries"]}
        assert by_name[ALPHA]["loaded"] is True and by_name[BETA]["loaded"] is False

        directory = client.get(f"/api/runs/{run_id}/tools")
        assert directory.status_code == 200
        page = directory.json()
        assert page["catalog_frozen"] is True and page["loaded_count"] == 1
        assert page["total"] == page["catalog_total"] == len(catalog["entries"])
        names = {item["name"] for item in page["items"]}
        assert {ALPHA, BETA} <= names
        assert "input_schema" not in page["items"][0]
        unloaded_page = client.get(f"/api/runs/{run_id}/tools", params={"status": "unloaded"})
        assert {item["name"] for item in unloaded_page.json()["items"]} == {BETA}
        loaded_page = client.get(f"/api/runs/{run_id}/tools", params={"status": "loaded"})
        assert [item["name"] for item in loaded_page.json()["items"]] == [ALPHA]
        detail = client.get(f"/api/runs/{run_id}/tools/{ALPHA}")
        assert detail.status_code == 200
        assert detail.json()["input_schema"] == ALPHA_SCHEMA
        assert client.get(f"/api/runs/{run_id}/tools/not_in_catalog").status_code == 404
        search = client.get(f"/api/runs/{run_id}/tools", params={"query": "alpha"})
        assert search.json()["total"] == 1
        assert {item["name"] for item in search.json()["items"]} == {ALPHA}


async def test_unloaded_tool_rejected_and_write_tool_needs_approval(
    isolated_workspace, monkeypatch
):
    repo = make_repo(isolated_workspace / "repo")
    manager = FakeManager(
        [
            {"name": ALPHA, "description": "Reads demo paths", "input_schema": ALPHA_SCHEMA},
            {"name": BETA, "description": "Writes demo values", "input_schema": BETA_SCHEMA},
        ],
        readonly=set(),
    )
    monkeypatch.setattr("eventide.host.MCPManager", lambda: manager)
    approvals: list[str] = []

    async def deny(approval_id, call, reason):
        approvals.append(call.name)
        return False

    provider = ScriptedProvider([
        {"tool_calls": [{"id": "b1", "name": BETA, "arguments": {"value": 1}}]},
        {"tool_calls": [{"id": "l1", "name": "tool_load", "arguments": {"name": BETA}}]},
        {"tool_calls": [{"id": "b2", "name": BETA, "arguments": {"value": 2}}]},
        {"text": "stopped trying"},
    ])
    host = RuntimeHost(settings_for(repo), provider)
    result = await host.run(
        RunRequest("write the demo value"), approval_handler=deny
    )
    assert result.status == "completed"

    # The unloaded write tool never executed: no handler, explicit error.
    assert manager.beta_calls == []
    events = host.store.run_events(result.run_id)
    errors = [
        e for e in events
        if e["type"] == "tool.completed" and e["payload"].get("name") == BETA
    ]
    assert errors and errors[0]["payload"].get("is_error") is True
    # Loading does not bypass policy: the loaded write tool still asked and the
    # denial kept the handler untouched.
    assert approvals == [BETA]
    assert manager.beta_calls == []
    projection = project_run_tools(events, run_id=result.run_id)
    by_name = {entry["name"]: entry for entry in projection["entries"]}
    assert by_name[BETA]["loaded"] is True and by_name[ALPHA]["loaded"] is False
    with TestClient(create_app(host)) as client:
        directory = client.get(
            f"/api/runs/{result.run_id}/tools", params={"status": "loaded"}
        )
        assert [item["name"] for item in directory.json()["items"]] == [BETA]
        failed = client.get(
            f"/api/runs/{result.run_id}/tools", params={"status": "failed"}
        )
        body = failed.json()
        assert body["total"] == 0 and body["failed_loads"] == []


def test_reopened_store_keeps_frozen_history_when_mcp_changes(
    isolated_workspace, monkeypatch
):
    repo = make_repo(isolated_workspace / "repo")
    manager = FakeManager(
        [{"name": ALPHA, "description": "Reads demo paths", "input_schema": ALPHA_SCHEMA}],
        readonly={ALPHA},
    )
    monkeypatch.setattr("eventide.host.MCPManager", lambda: manager)
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "l1", "name": "tool_load", "arguments": {"name": ALPHA}}]},
        {"tool_calls": [{"id": "a1", "name": ALPHA, "arguments": {}}]},
        {"text": "done"},
    ])
    host = RuntimeHost(settings_for(repo), provider)
    with TestClient(create_app(host)) as client:
        workspace_id = host.store.list_workspaces()[0].workspace_id
        run_id = _start_run(client, workspace_id, "keep", "use the demo reader")
        assert wait_for_run(client, run_id)["status"] == "completed"
        original = client.get(f"/api/runs/{run_id}/tools").json()
        original_detail = client.get(f"/api/runs/{run_id}/tools/{ALPHA}").json()

    # Reopen the same state with a different MCP world: alpha renamed with a
    # changed schema, gamma added. History projection must not change.
    changed = FakeManager(
        [{"name": "mcp__demo__alpha2", "description": "Changed", "input_schema": BETA_SCHEMA},
         {"name": "mcp__demo__gamma", "description": "New", "input_schema": BETA_SCHEMA}],
        readonly=set(),
    )
    monkeypatch.setattr("eventide.host.MCPManager", lambda: changed)
    reopened = RuntimeHost(settings_for(repo), ScriptedProvider([{"text": "unused"}]))
    with TestClient(create_app(reopened)) as client:
        again = client.get(f"/api/runs/{run_id}/tools").json()
        assert again["catalog_version"] == original["catalog_version"]
        assert again["items"] == original["items"]
        assert again["loaded_count"] == original["loaded_count"] == 1
        assert again["stale_receipts"] == 0
        assert client.get(f"/api/runs/{run_id}/tools/{ALPHA}").json() == original_detail
        assert client.get(f"/api/runs/{run_id}/tools/mcp__demo__gamma").status_code == 404
        assert client.get(f"/api/runs/{run_id}/tools/mcp__demo__alpha2").status_code == 404

        # A fresh run under the changed manager freezes its own catalog.
        new_run = _start_run(client, workspace_id, "fresh", "hi")
        assert wait_for_run(client, new_run)["status"] == "completed"
        fresh = client.get(f"/api/runs/{new_run}/tools").json()
        fresh_names = {item["name"] for item in fresh["items"]}
        assert fresh["catalog_version"] != original["catalog_version"]
        assert "mcp__demo__gamma" in fresh_names and ALPHA not in fresh_names


def test_local_and_plan_catalogs_freeze_actual_exposed_tools(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(repo), ScriptedProvider([{"text": "done"}, {"text": "done"}]))
    with TestClient(create_app(host)) as client:
        workspace_id = host.store.list_workspaces()[0].workspace_id
        run_id = _start_run(client, workspace_id, "plain", "no mcp here")
        assert wait_for_run(client, run_id)["status"] == "completed"
        page = client.get(f"/api/runs/{run_id}/tools").json()
        assert page["catalog_frozen"] is True
        assert page["catalog_version"] is not None
        assert page["items"] and all(item["provided"] for item in page["items"])
        assert client.get(f"/api/runs/{run_id}/tools/read_file").status_code == 200
        assert client.get("/api/runs/missing_run/tools").status_code == 404
        session_id = client.post("/api/sessions", json={"workspace_id": workspace_id},
                                 headers={"Idempotency-Key": "plan-session"}).json()["session_id"]
        planned = client.post(f"/api/sessions/{session_id}/runs",
                              json={"prompt": "read only", "mode": "plan"},
                              headers={"Idempotency-Key": "plan-run"}).json()["run_id"]
        assert wait_for_run(client, planned)["status"] == "completed"
        names = {item["name"] for item in client.get(f"/api/runs/{planned}/tools").json()["items"]}
        assert "read_file" in names and "write_file" not in names and "bash" not in names
        assert project_run_tools([], run_id="historical")["catalog_frozen"] is False
