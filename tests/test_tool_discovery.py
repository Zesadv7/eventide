"""MK-16 tool discovery: deterministic catalog, load receipts, execution boundary."""

import json

import pytest

from eventide.executor import ToolContext, ToolExecutor
from eventide.models import ToolCall, ToolOutput
from eventide.policy import PolicyEngine
from eventide.tool_discovery import (
    DISCOVER_TOOL,
    LOAD_TOOL,
    ToolDiscovery,
    ToolDiscoveryError,
)


def _bulk_tools(count: int) -> list[dict]:
    return [
        {
            "name": f"mcp__bulk__tool_{index:03d}",
            "description": f"Bulk tool number {index} for catalog paging",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
            },
        }
        for index in range(count)
    ]


async def _noop_handler(**_arguments):
    return "executed"


def _discovery(tool_count: int = 120) -> ToolDiscovery:
    on_demand = _bulk_tools(tool_count)
    handlers = {tool["name"]: _noop_handler for tool in on_demand}
    return ToolDiscovery(
        [
            {
                "name": "read_file",
                "description": "base reader",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        on_demand,
        handlers,
        readonly_names={"mcp__bulk__tool_000"},
    )


async def test_catalog_hundred_tools_search_pagination_and_empty_feedback():
    discovery = _discovery()
    catalog = discovery.catalog
    assert len(catalog) == 121

    page = catalog.search("", 0, 50)
    assert page["total"] == 121
    assert len(page["items"]) == 50
    assert page["items"][0]["name"] < page["items"][1]["name"]

    second = catalog.search("", 50, 50)
    assert second["total"] == 121
    assert second["items"][0]["name"] > page["items"][-1]["name"]
    assert catalog.search("", 121, 50)["items"] == []

    hit = catalog.search("tool_007", 0, 50)
    assert hit["total"] == 1
    assert hit["items"][0]["name"] == "mcp__bulk__tool_007"
    assert hit["items"][0]["source"] == "mcp:bulk"
    assert hit["items"][0]["provided"] is False

    missing = catalog.search("does-not-exist-anywhere", 0, 50)
    assert missing["total"] == 0 and missing["items"] == []

    base = catalog.detail("read_file")
    assert base["source"] == "builtin" and base["provided"] is True
    assert base["input_schema"]["type"] == "object"

    for item in catalog.search("", 0, 200)["items"]:
        assert item["status"] == "available" and item["disabled_reason"] is None
        assert item["environment"] == "local"
        expected_effect = "readonly" if item["name"] == "mcp__bulk__tool_000" else "effect"
        assert item["effect"] == expected_effect


async def test_catalog_version_fingerprint_is_stable_and_drifts():
    first = _discovery().catalog
    second = _discovery().catalog
    assert first.catalog_version == second.catalog_version

    drifted_tools = _bulk_tools(120)
    drifted_tools[7]["input_schema"]["properties"]["value"] = {"type": "string"}
    drifted = ToolDiscovery(
        [{"name": "read_file", "description": "base reader", "input_schema": {"type": "object"}}],
        drifted_tools,
        {tool["name"]: _noop_handler for tool in drifted_tools},
    ).catalog
    assert drifted.catalog_version != first.catalog_version

    renamed = ToolDiscovery(
        [
            {
                "name": "read_file",
                "description": "changed description",
                "input_schema": {"type": "object"},
            }
        ],
        _bulk_tools(120),
        {},
    ).catalog
    assert renamed.catalog_version not in {first.catalog_version, drifted.catalog_version}


async def test_load_receipt_version_check_and_boundaries():
    discovery = _discovery()
    version = discovery.catalog.catalog_version

    receipt = discovery.load("mcp__bulk__tool_005", version)
    assert receipt["tool"] == "mcp__bulk__tool_005"
    assert receipt["catalog_version"] == version
    assert receipt["schema_digest"]
    assert receipt["input_schema"]["type"] == "object"
    assert discovery.load("mcp__bulk__tool_005") == receipt

    with pytest.raises(ToolDiscoveryError) as drift:
        discovery.load("mcp__bulk__tool_006", "0" * 64)
    assert drift.value.code == "catalog_version_mismatch"

    with pytest.raises(ToolDiscoveryError) as unknown:
        discovery.load("mcp__bulk__missing")
    assert unknown.value.code == "tool_not_found"

    with pytest.raises(ToolDiscoveryError) as provided:
        discovery.load("read_file")
    assert provided.value.code == "already_provided"

    assert discovery.loaded.keys() == {"mcp__bulk__tool_005"}


async def test_executor_rejects_unloaded_tool_and_policy_still_gates_loaded(isolated_workspace):
    discovery = _discovery(2)
    executor = ToolExecutor(
        discovery.handlers(),
        tools=[
            {"name": DISCOVER_TOOL, "input_schema": {}},
            {"name": LOAD_TOOL, "input_schema": {}},
        ],
    )
    discovery.bind_executor(executor)
    context = ToolContext(isolated_workspace, "run", PolicyEngine())

    unloaded = await executor.execute(ToolCall("1", "mcp__bulk__tool_001", {"value": 1}), context)
    assert unloaded.is_error and "Unknown tool" in unloaded.content

    discover_result = await executor.execute(
        ToolCall("2", DISCOVER_TOOL, {"query": "tool_001"}), context
    )
    assert not discover_result.is_error
    page = json.loads(discover_result.content)
    assert page["total"] == 1 and page["items"][0]["name"] == "mcp__bulk__tool_001"

    load_result = await executor.execute(
        ToolCall("3", LOAD_TOOL, {"name": "mcp__bulk__tool_001"}), context
    )
    assert not load_result.is_error
    receipt = json.loads(load_result.content)
    assert receipt["tool"] == "mcp__bulk__tool_001"

    # Loading only unlocks the schema; the effect tool still needs approval.
    loaded_result = await executor.execute(
        ToolCall("4", "mcp__bulk__tool_001", {"value": 1}), context
    )
    assert loaded_result.is_error
    assert loaded_result.metadata["code"] == "permission_denied"

    bad_arguments = await executor.execute(
        ToolCall("5", "mcp__bulk__tool_001", {"value": "not-an-int"}), context
    )
    assert bad_arguments.is_error and "invalid_arguments" in bad_arguments.metadata["code"]

    load_error = await executor.execute(
        ToolCall(
            "6", LOAD_TOOL, {"name": "mcp__bulk__tool_000", "expected_catalog_version": "0" * 64}
        ),
        context,
    )
    assert load_error.is_error
    assert load_error.metadata["code"] == "catalog_version_mismatch"

    readonly_load = await executor.execute(
        ToolCall("7", LOAD_TOOL, {"name": "mcp__bulk__tool_000"}), context
    )
    assert not readonly_load.is_error
    readonly_call = await executor.execute(
        ToolCall("8", "mcp__bulk__tool_000", {"value": 1}), context
    )
    assert not readonly_call.is_error and readonly_call.content == "executed"

    # Re-loading the same tool is idempotent and returns the same receipt.
    effect_load = await executor.execute(
        ToolCall("9", LOAD_TOOL, {"name": "mcp__bulk__tool_001"}), context
    )
    assert not effect_load.is_error
    assert json.loads(effect_load.content) == receipt


async def test_model_tools_and_handlers_follow_loaded_state():
    discovery = _discovery(2)
    # On-demand entries exist, so discovery/load are advertised before any load.
    assert [tool["name"] for tool in discovery.model_tools()] == [DISCOVER_TOOL, LOAD_TOOL]
    assert set(discovery.handlers()) == {DISCOVER_TOOL, LOAD_TOOL}

    empty = ToolDiscovery(
        [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}], [], {}
    )
    assert empty.model_tools() == [] and empty.handlers() == {}

    discovery.load("mcp__bulk__tool_001")
    names = [tool["name"] for tool in discovery.model_tools()]
    assert names == [DISCOVER_TOOL, LOAD_TOOL, "mcp__bulk__tool_001"]
    assert set(discovery.handlers()) == {DISCOVER_TOOL, LOAD_TOOL, "mcp__bulk__tool_001"}

    output = await discovery._handle_discover(query="", offset=0, limit=1)
    assert isinstance(output, ToolOutput) and output.metadata["code"] == "tool_catalog_page"
