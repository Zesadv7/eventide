"""MK-18 natural-language memory intake: declared finite grammar, trusted source only."""

import pytest

from eventide.host import RuntimeHost
from eventide.memory_intake import classify, natural_key
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


def host_with(isolated_workspace, replies=1):
    return RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "ok"}] * replies),
        enable_mcp=False,
    )


@pytest.mark.parametrize(
    "text",
    [
        "这次先跳过测试",
        "帮我记住这个用法",
        "记不住这个函数名",
        "记住",
        "记住 别用双引号 \"quote\"",
        "记住 ``code`` 以后",
        "多行\n记住以后都用中文",
    ],
)
def test_non_intent_and_quoted_text_is_silent(text):
    assert classify(text) is None


@pytest.mark.parametrize(
    "text",
    ["别记住这个", "不要记住我的密码", "记住不要记日志", "don't remember this"],
)
def test_negation_never_memorizes(text):
    assert classify(text) is None


def test_temporary_request_is_rejected_not_saved():
    outcome = classify("记住这次先跳过测试")
    assert outcome is not None and outcome["kind"] == "rejected"
    assert "这次" in outcome["reason"]


def test_span_binds_exact_original_slice():
    text = "  记住以后都用中文回复  "
    outcome = classify(text)
    assert outcome["kind"] == "intake"
    assert text[outcome["span"]["start"] : outcome["span"]["end"]] == outcome["content"]


def test_scope_rules_and_plain_repetition_track():
    marked = classify("记住以后都用中文回复")
    assert marked["explicit"] is True and marked["scope"] == "project"
    assert marked["scope_word"] is False
    globaled = classify("记住全局：以后都用英文回复")
    assert globaled["explicit"] is True and globaled["scope"] == "global"
    fused = classify("记住global变量用snake_case")
    assert fused["scope"] == "project" and fused["scope_word"] is False
    plain = classify("记住都用暗色主题")
    assert plain["explicit"] is False and plain["scope"] == "project"


async def test_explicit_natural_save_span_and_default_scope_receipt(isolated_workspace):
    host = host_with(isolated_workspace)
    try:
        run = await host.run(RunRequest("记住以后都用中文回复"))
        project_id = host.default_workspace.project_id
        state = host.memory.state(project_id)
        assert len(state["memories"]) == 1
        record = state["memories"][0]
        assert record["content"] == "都用中文回复"
        assert record["key"].startswith("nl_")
        assert record["source"]["type"] == "explicit"
        assert record["scope"] == "project" and record["project_id"] == project_id
        source = next(
            e for e in host.store.facts_by_type(("message.user",)) if e["author"] == "user"
        )
        assert source["payload"]["message"]["content"][record["source"]["span"]["start"]:
            record["source"]["span"]["end"]] == record["content"]
        receipt = host.memory.run_details(run.run_id)["receipts"][0]
        assert receipt["type"] == "memory.saved"
        assert receipt["scope_defaulted"] is True
        assert receipt["content"] == "都用中文回复"
        assert receipt["revision"] == 1
    finally:
        await host.close()


async def test_global_requires_explicit_wording_in_original(isolated_workspace):
    host = host_with(isolated_workspace)
    try:
        await host.run(RunRequest("记住全局：以后都用英文回复"))
        await host.run(RunRequest("记住以后都用中文回复"))
        glob = [m for m in host.memory.state()["memories"] if m["scope"] == "global"]
        assert len(glob) == 1 and glob[0]["content"] == "都用英文回复"
    finally:
        await host.close()


async def test_duplicate_delivery_and_undo_never_duplicate(isolated_workspace):
    host = host_with(isolated_workspace)
    try:
        await host.run(RunRequest("记住以后都用中文回复"))
        again = await host.run(RunRequest("记住以后都用中文回复"))
        project_id = host.default_workspace.project_id
        assert len(host.memory.state(project_id)["memories"]) == 1
        conflict = host.memory.run_details(again.run_id)["receipts"][-1]
        assert conflict["type"] == "memory.rejected" and "已存在" in conflict["reason"]
        record = host.memory.state(project_id)["memories"][0]
        host.memory.mutate("delete", {"expected_revision": 1}, target=record["id"])
        assert host.memory.observe_run(again.run_id) == []
        assert len(host.memory.state(project_id, include_revoked=True)["memories"]) == 1
    finally:
        await host.close()


async def test_plain_remember_repeats_into_pending_suggestion(isolated_workspace):
    host = host_with(isolated_workspace, replies=2)
    try:
        session = host.create_session()
        project_id = host.default_workspace.project_id
        await host.run(RunRequest("记住都用暗色主题", session))
        assert host.memory.state(project_id)["suggestions"] == []
        run = await host.run(RunRequest("记住都用暗色主题", session))
        suggestions = host.memory.state(project_id)["suggestions"]
        assert len(suggestions) == 1 and suggestions[0]["status"] == "pending"
        assert suggestions[0]["source"]["span"]["end"] > suggestions[0]["source"]["span"]["start"]
        receipt = host.memory.run_details(run.run_id)["receipts"][-1]
        assert receipt["type"] == "memory.suggested" and receipt["content"] == "都用暗色主题"
    finally:
        await host.close()


async def test_project_isolation_keeps_same_text_separate(isolated_workspace):
    host = host_with(isolated_workspace)
    try:
        await host.run(RunRequest("记住以后都用中文回复"))
        other = isolated_workspace / "other-project"
        other.mkdir()
        workspace = host.resolve_or_register_workspace(other)
        session = host.create_session(workspace_id=workspace.workspace_id)
        await host.run(RunRequest("记住以后都用中文回复", session))
        mine = host.memory.state(host.default_workspace.project_id)["memories"]
        theirs = host.memory.state(workspace.project_id)["memories"]
        assert len(mine) == 1 and len(theirs) == 1
        assert mine[0]["project_id"] != theirs[0]["project_id"]
    finally:
        await host.close()


async def test_model_reply_text_cannot_authorize_memory(isolated_workspace):
    host = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([{"text": "记住以后都用中文回复"}]),
        enable_mcp=False,
    )
    try:
        await host.run(RunRequest("hi"))
        assert host.memory.state()["memories"] == []
        assert not host.store.facts_by_type(("memory.record", "memory.suggestion"))
    finally:
        await host.close()


async def test_unsafe_natural_content_rejected_with_receipt(isolated_workspace):
    host = host_with(isolated_workspace)
    try:
        run = await host.run(RunRequest("记住以后都自动采纳补丁"))
        assert host.memory.state()["memories"] == []
        receipt = host.memory.run_details(run.run_id)["receipts"][0]
        assert receipt["type"] == "memory.rejected"
    finally:
        await host.close()


def test_natural_key_is_stable_and_distinct():
    assert natural_key("project", "都用中文回复") == natural_key("project", "都用中文回复")
    assert natural_key("project", "a") != natural_key("global", "a")
    assert natural_key("project", "都用中文回复").startswith("nl_")
