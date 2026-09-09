"""Large tool results stay durable while model input and retrieval stay bounded."""

import json

from eventide.context_builder import ContextBuilder
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.providers import ScriptedProvider
from eventide.tool_results import INLINE_RESULT_CHARS, PAGE_MAX_CHARS, reader
from tests.test_runtime import settings_for


def _append_result(host, session, run_id, content):
    host.store.create_run(run_id, session)
    host.store.append_message(session, {"role": "user", "content": "produce output"})
    host.store.append_fact(
        session,
        "model.response",
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "large", "name": "bash", "input": {}}
                ],
            }
        },
        run_id=run_id,
    )
    host.store.append_fact(
        session,
        "tool.completed",
        {"call_id": "large", "name": "bash", "content": content, "is_error": False},
        run_id=run_id,
    )


async def test_context_previews_large_result_without_changing_event_log(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        session = host.create_session()
        content = "HEAD" + "x" * (INLINE_RESULT_CHARS * 2) + "TAIL"
        _append_result(host, session, "source", content)
        before = host.store.session_events(session)

        messages, compacted, trimmed = await ContextBuilder(host.store).build(
            session,
            provider=ScriptedProvider([]),
            provider_name="scripted",
            model="scripted",
            budget=20_000,
        )

        block = messages[-1]["content"][0]
        assert compacted is None
        assert trimmed == {
            "previewed_call_ids": ["large"],
            "preview_omitted_chars": len(content) - 10_000,
        }
        assert block["content"].startswith("HEAD")
        assert block["content"].endswith("TAIL")
        assert 'run_id="source"' in block["content"]
        assert 'call_id="large"' in block["content"]
        assert len(json.dumps(messages, ensure_ascii=False)) < 20_000
        assert host.store.session_events(session) == before
        assert host.store.load_messages(session)[-1]["content"][0]["content"] == content
    finally:
        await host.close()


async def test_runtime_can_page_prior_result_with_session_scoped_readonly_tool(
    isolated_workspace,
):
    content = "0123456789" * 3_000
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                assert "read_tool_result" in {tool["name"] for tool in request.tools}
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "page",
                            "read_tool_result",
                            {"run_id": "source", "call_id": "large", "offset": 123, "limit": 50},
                        ),
                    )
                )
            return ModelResponse("done")

    host = RuntimeHost(settings_for(isolated_workspace, context_limit=24_000), Provider())
    try:
        session = host.create_session()
        _append_result(host, session, "source", content)
        host.store.finish_run("source", status="completed", output="stored")

        result = await host.run(RunRequest("inspect exact output", session_id=session))

        assert result.status == "completed"
        first_preview = next(
            block["content"]
            for message in requests[0].messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result" and block.get("tool_use_id") == "large"
        )
        assert "Tool output preview" in first_preview
        completed = [
            event
            for event in host.store.run_events(result.run_id)
            if event["type"] == "tool.completed"
        ][0]
        assert completed["payload"]["content"].endswith(content[123:173])
        prepared = next(
            event
            for event in host.store.run_events(result.run_id)
            if event["type"] == "tool.prepared"
        )
        assert prepared["payload"]["readonly"] is True
        trimmed = next(
            event
            for event in host.store.run_events(result.run_id)
            if event["type"] == "context.trimmed"
        )
        assert trimmed["payload"]["previewed_call_ids"] == ["large"]
        assert host.store.get_tool_result(session, "source", "large")["content"] == content
    finally:
        await host.close()


async def test_runtime_pages_a_large_result_created_in_the_current_run(isolated_workspace):
    content = "abcdefghij" * 3_000
    requests = []

    class Provider:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(tool_calls=(ToolCall("large", "large_output", {}),))
            if len(requests) == 2:
                projected = request.messages[-1]["content"][0]["content"]
                assert "Tool output preview" in projected
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "page",
                            "read_tool_result",
                            {"run_id": "live", "call_id": "large", "offset": 20_000, "limit": 25},
                        ),
                    )
                )
            return ModelResponse("done")

    large_tool = {
        "name": "large_output",
        "description": "Return test output.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
    host = RuntimeHost(
        settings_for(isolated_workspace, context_limit=24_000),
        Provider(),
        tools=[large_tool],
        handlers={"large_output": lambda: content},
    )
    try:
        result = await host.run(RunRequest("run", run_id="live"))
        assert result.status == "completed"
        page = next(
            event["payload"]["content"]
            for event in host.store.run_events("live")
            if event["type"] == "tool.completed" and event["payload"]["call_id"] == "page"
        )
        assert page.endswith(content[20_000:20_025])
        assert host.store.get_tool_result(result.session_id, "live", "large")["content"] == content
    finally:
        await host.close()


async def test_tool_result_reader_validates_pages_and_session_identity(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace))
    try:
        first = host.create_session()
        second = host.create_session()
        _append_result(host, first, "source", "abcdef")

        read_first = reader(host.store, first)
        read_second = reader(host.store, second)
        assert read_first("source", "large", 2, 3).endswith("cde")
        assert read_second("source", "large", 0, 3).startswith("Error:")
        assert read_first("source", "large", -1, 3).startswith("Error:")
        assert read_first("source", "large", 0, PAGE_MAX_CHARS + 1).startswith("Error:")
    finally:
        await host.close()
