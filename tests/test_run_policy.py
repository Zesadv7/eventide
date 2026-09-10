"""Run mode policy: normalization, plan catalog restriction, agent approval."""

from eventide.run_policy import (
    PLAN_READONLY_TOOLS,
    filter_tool_catalog,
    mode_system_note,
    normalize_mode,
    should_auto_approve,
)


def test_normalize_mode_falls_back_to_auto():
    assert normalize_mode(None) == "auto"
    assert normalize_mode("") == "auto"
    assert normalize_mode("yolo") == "auto"
    assert normalize_mode("PLAN") == "auto"
    assert normalize_mode("Agent") == "auto"
    assert normalize_mode(["plan"]) == "auto"
    assert normalize_mode("auto") == "auto"
    assert normalize_mode("plan") == "plan"
    assert normalize_mode("agent") == "agent"


def test_filter_tool_catalog_keeps_only_the_plan_whitelist_without_mutating():
    tools = [
        {"name": "read_file", "description": "r"},
        {"name": "bash", "description": "b"},
        {"name": "write_file", "description": "w"},
        {"name": "glob", "description": "g"},
        {"name": "mcp__docs__search", "description": "m"},
        {"name": "compact", "description": "c"},
        {"name": "read_tool_result", "description": "p"},
        {"name": "todo_write", "description": "t"},
        {"name": "load_skill", "description": "s"},
        {"name": "edit_file", "description": "e"},
    ]
    snapshot = [dict(tool) for tool in tools]
    visible = filter_tool_catalog(tools, "plan")
    # Only the read-only whitelist survives, in the original catalog order.
    assert [tool["name"] for tool in visible] == [
        "read_file",
        "glob",
        "compact",
        "read_tool_result",
        "todo_write",
        "load_skill",
    ]
    assert set(PLAN_READONLY_TOOLS) == {tool["name"] for tool in visible}
    # The caller's list is untouched, and other modes get a copy of everything.
    assert tools == snapshot
    assert visible is not tools
    for mode in ("auto", "agent", "whatever"):
        full = filter_tool_catalog(tools, mode)
        assert full == tools
        assert full is not tools


def test_mode_system_note_only_for_plan():
    assert mode_system_note("plan")
    assert mode_system_note("auto") == ""
    assert mode_system_note("agent") == ""
    assert mode_system_note("PLAN") == ""


def test_should_auto_approve_only_agent():
    assert should_auto_approve("agent") is True
    assert should_auto_approve("auto") is False
    assert should_auto_approve("plan") is False
    assert should_auto_approve("AGENT") is False
