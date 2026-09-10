"""Runtime facts for the web contract: capabilities, run modes, request sizes."""

import json

from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.run_policy import mode_system_note
from eventide.runtime import AgentRuntime
from eventide.tools.runtime_catalog import TOOLS
from tests.test_runtime import settings_for


async def test_capabilities_config_path_applies_only_to_default_workspace(
    isolated_workspace,
):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        explicit = isolated_workspace / "shared-mcp.json"
        explicit.write_text("{}", encoding="utf-8")
        await runtime.initialize(explicit)
        other_root = isolated_workspace / "other-project"
        other_root.mkdir()
        other = runtime.resolve_or_register_workspace(other_root)

        default_caps = runtime.workspace_capabilities(
            runtime.default_workspace.workspace_id
        )
        other_caps = runtime.workspace_capabilities(other.workspace_id)
        assert default_caps["paths"]["mcp_config"] == str(explicit)
        assert other_caps["paths"]["mcp_config"] == str(other_root / "mcp.json")
        assert other_caps["notes"] == ["未配置 mcp.json"]
    finally:
        await runtime.close()


async def test_capabilities_skip_broken_skills_and_survive_bad_mcp_json(
    isolated_workspace,
):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        workspace_id = runtime.default_workspace.workspace_id
        broken = isolated_workspace / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00not utf-8")
        caps = runtime.workspace_capabilities(workspace_id)
        # An unreadable manifest is skipped, introspection never raises.
        assert caps["skills"] == []

        (isolated_workspace / "skills" / "wobbly").mkdir()
        (isolated_workspace / "skills" / "wobbly" / "SKILL.md").write_text(
            "---\nname: [unclosed\n---\nbody",
            encoding="utf-8",
        )
        (isolated_workspace / "mcp.json").write_text("{not json", encoding="utf-8")
        caps = runtime.workspace_capabilities(workspace_id)
        # Invalid frontmatter degrades to the directory name instead of failing.
        assert [skill["name"] for skill in caps["skills"]] == ["wobbly"]
        assert caps["mcp"] == []
        assert len(caps["notes"]) == 1
        assert "解析失败" in caps["notes"][0]
    finally:
        await runtime.close()


async def test_plan_mode_hides_writes_blocks_execution_and_appends_note(
    isolated_workspace,
):
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "w",
                        "name": "write_file",
                        "arguments": {"path": "evil.txt", "content": "nope"},
                    }
                ]
            },
            {"text": "plan ready"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    try:
        result = await runtime.run(RunRequest("plan only", mode="plan"))
        assert result.status == "completed"
        assert result.output == "plan ready"

        # The production catalog does contain the write tool, the plan view
        # shown to the model does not.
        assert "write_file" in {tool["name"] for tool in TOOLS}
        catalog = {tool["name"] for tool in provider.requests[0].tools}
        assert not catalog & {"write_file", "edit_file", "bash"}
        assert mode_system_note("plan") in provider.requests[0].system

        # The model ignored the catalog and called write_file anyway: the run
        # still finishes and nothing was ever written.
        assert not (isolated_workspace / "evil.txt").exists()
        completed = next(
            event
            for event in runtime.store.run_events(result.run_id)
            if event["type"] == "tool.completed"
        )
        assert completed["payload"]["name"] == "write_file"
        assert completed["payload"]["is_error"] is True
        assert completed["payload"]["content"] == "Unknown tool: write_file"
    finally:
        await runtime.close()


async def test_agent_mode_auto_approves_ask_without_a_handler(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "d", "name": "bash", "arguments": {"command": "rm x"}}]},
            {"text": "agent done"},
        ]
    )
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    try:
        # No approval handler at all: in auto mode an ASK decision is denied,
        # agent mode must resolve it without waiting for anyone.
        result = await runtime.run(RunRequest("clean up", mode="agent"))
        assert result.status == "completed"
        assert result.output == "agent done"
        events = runtime.store.run_events(result.run_id)
        resolved = [event for event in events if event["type"] == "approval.resolved"]
        assert len(resolved) == 1
        assert resolved[0]["payload"]["approved"] is True
        assert resolved[0]["payload"]["auto"] is True
        assert resolved[0]["author"] == "runtime"
        completed = next(
            event for event in events if event["type"] == "tool.completed"
        )
        assert completed["payload"]["name"] == "bash"
        assert not completed["payload"]["content"].startswith("Permission denied")
    finally:
        await runtime.close()


async def test_unknown_mode_falls_back_to_auto(isolated_workspace):
    provider = ScriptedProvider([{"text": "ok"}])
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    try:
        result = await runtime.run(RunRequest("hi", mode="yolo"))
        assert result.status == "completed"
        catalog = {tool["name"] for tool in provider.requests[0].tools}
        assert "write_file" in catalog
        assert mode_system_note("plan") not in provider.requests[0].system
    finally:
        await runtime.close()


async def test_model_request_chars_use_the_shared_budget_measure(isolated_workspace):
    provider = ScriptedProvider([{"text": "ok"}])
    runtime = AgentRuntime(settings_for(isolated_workspace), provider)
    try:
        result = await runtime.run(RunRequest("hi"))
        payload = next(
            event
            for event in runtime.store.run_events(result.run_id)
            if event["type"] == "model.request"
        )["payload"]
        sent = provider.requests[0]
        assert payload["request_chars"] == len(
            json.dumps(
                {"system": sent.system, "messages": sent.messages, "tools": sent.tools},
                ensure_ascii=False,
            )
        )
        assert payload["tool_catalog_chars"] == len(
            json.dumps(sent.tools, ensure_ascii=False)
        )
        assert payload["request_chars"] > 0
        assert payload["tool_catalog_chars"] > 0
    finally:
        await runtime.close()


async def test_available_models_without_api_key_degrades_to_note(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        result = await runtime.available_models()
        assert result == {"models": [], "error": "未配置 API Key"}
    finally:
        await runtime.close()


async def test_models_endpoint_returns_probed_ids(
    isolated_workspace, monkeypatch
):
    from eventide import model_catalog

    async def fake_list_models(provider, api_key, base_url, **kwargs):
        assert provider == "scripted"
        assert api_key == "test-key"
        return ["model-a", "model-b"], None

    monkeypatch.setattr(model_catalog, "list_models", fake_list_models)
    settings = settings_for(isolated_workspace, api_key="test-key")
    runtime = AgentRuntime(settings, ScriptedProvider([]))
    try:
        result = await runtime.available_models()
        assert result == {"models": ["model-a", "model-b"], "error": None}
    finally:
        await runtime.close()
