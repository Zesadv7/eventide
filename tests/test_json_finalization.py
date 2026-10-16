"""JSON finalization shares the original loop, recovery chain and model ledger."""

import asyncio
from dataclasses import replace

import pytest

from eventide.budgets import BudgetScope
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest
from eventide.projections import MessagesProjection
from eventide.providers import ProviderError, ScriptedProvider
from eventide.streaming import ModelStreamEvent
from eventide.usage import build_usage_ledger
from eventide.workspace import digest
from tests.test_host import make_repo
from tests.test_json_generation import ENDPOINT
from tests.test_runtime import settings_for
from tests.test_unknown_outcome import UnknownExecutor


@pytest.fixture
def json_settings(isolated_workspace):
    source = make_repo(isolated_workspace / "source")
    (source / "code.py").write_text("value = 1\n")
    return replace(
        settings_for(source), provider="openai_compatible", base_url=ENDPOINT,
        model="glm-5.3-flash", state_dir=isolated_workspace / "state",
    )


@pytest.mark.parametrize("draft", ['{"value":1}', "The inspected value is 1."])
async def test_json_has_one_finalization_with_real_catalog_and_actual_parameters(
    json_settings, draft,
):
    script = [
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
        {"text": draft}, {"text": '{"value":1}'},
    ]
    provider = ScriptedProvider([
        {**item, "usage": {"input_tokens": 2, "output_tokens": 1}} for item in script
    ])
    host = RuntimeHost(json_settings, provider, enable_mcp=False)
    try:
        result = await host.run(RunRequest(
            "Inspect", generation={"response_format": "json_object"},
        ))
        assert result.status == "completed" and result.output == '{"value":1}'
        assert [request.phase for request in provider.requests] == [
            "tools", "tools", "finalization",
        ]
        assert [request.generation.get("response_format") for request in provider.requests] == [
            None, None, "json_object",
        ]
        assert provider.requests[0].tools and provider.requests[1].tools
        assert provider.requests[2].tools == []
        events = host.store.run_events(result.run_id)
        requests = [event["payload"] for event in events if event["type"] == "model.request"]
        for fact, actual in zip(requests, provider.requests, strict=True):
            assert fact["phase"] == actual.phase
            assert fact["generation"] == actual.generation
            assert fact["tool_catalog_hash"] == digest(actual.tools)
            assert fact["tool_count"] == len(actual.tools)
        assert requests[-1]["tool_catalog_chars"] == 2
        drafts = [event for event in events if event["payload"].get("draft_for") == "json_object"]
        assert len(drafts) == 1 and drafts[0]["payload"]["text"] == draft
        assert host._admitted_intent(result.run_id)["generation"][
            "response_format"
        ] == "json_object"
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert [call["phase"] for call in ledger["calls"]] == ["tools", "tools", "finalization"]
        assert ledger["reported_totals"] == {"input_tokens": 6, "output_tokens": 3}
    finally:
        await host.close()


async def test_text_mode_keeps_original_request_count(json_settings):
    provider = ScriptedProvider([{"text": "done"}])
    host = RuntimeHost(json_settings, provider, enable_mcp=False)
    try:
        result = await host.run(RunRequest("Answer", generation={"response_format": "text"}))
        assert result.status == "completed" and len(provider.requests) == 1
        assert provider.requests[0].generation["response_format"] == "text"
        assert provider.requests[0].phase == "agent"
    finally:
        await host.close()


@pytest.mark.parametrize("truncated", [False, True])
async def test_finalization_tool_hallucination_is_abandoned_and_paired(json_settings, truncated):
    provider = ScriptedProvider([
        {"text": "draft"},
        {"tool_calls": [{"id": "forbidden", "name": "write_file", "arguments": {
            "path": "must-not-exist.py", "content": "bad",
        }}], "stop_reason": "length" if truncated else "tool_calls"},
    ])
    host = RuntimeHost(json_settings, provider, enable_mcp=False)
    try:
        result = await host.run(RunRequest(
            "Inspect", generation={"response_format": "json_object"},
        ))
        assert result.status == "failed" and len(provider.requests) == 2
        assert provider.requests[-1].tools == []
        assert not (json_settings.workdir / "must-not-exist.py").exists()
        events = host.store.run_events(result.run_id)
        assert not any(event["type"] == "tool.prepared" for event in events)
        abandoned = [event["payload"] for event in events if event["type"] == "tool.abandoned"]
        assert len(abandoned) == 1 and abandoned[0]["dispatched"] is False
        assert abandoned[0]["reason"] == (
            "response_truncated" if truncated else "finalization_tools_disabled"
        )
        messages = MessagesProjection.project(host.store.session_events(result.session_id))
        assert messages[-1]["content"][0]["tool_use_id"] == "forbidden"
    finally:
        await host.close()


async def test_retryable_finalization_failure_keeps_unknown_usage_without_retry(json_settings):
    provider = ScriptedProvider([
        {"text": "draft", "usage": {"input_tokens": 2, "output_tokens": 1}},
        {"error": "503 lost request", "retryable": True}, {"text": "must not retry"},
    ])
    host = RuntimeHost(json_settings, provider, enable_mcp=False)
    try:
        result = await host.run(RunRequest("Answer", generation={"response_format": "json_object"}))
        assert result.status == "failed" and len(provider.requests) == 2
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 2 and ledger["unknown_attempts"] == 1
        assert ledger["calls"][-1]["phase"] == "finalization"
        retry = [event["payload"] for event in host.store.run_events(result.run_id)
                 if event["type"] == "model.retry"]
        assert len(retry) == 1 and retry[0]["retryable"] is False
    finally:
        await host.close()


async def test_last_step_draft_restores_only_finalization_after_restart(json_settings):
    settings = replace(json_settings, max_steps=1)
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
        {"text": '{"value":1}'},
    ])
    host = RuntimeHost(settings, provider, enable_mcp=False)
    result = await host.run(RunRequest("Inspect", generation={"response_format": "json_object"}))
    assert result.status == "interrupted" and result.steps == 1
    assert host.store.get_run(result.run_id)["reason"] == "step_budget"
    assert len(provider.requests) == 1
    result = await host.continue_session(result.session_id)
    assert result.status == "interrupted" and len(provider.requests) == 2
    await host.close()
    next_provider = ScriptedProvider([
        {"text": '{"value":1}'}, {"text": "new draft"}, {"text": '{"value":2}'},
    ])
    reopened = RuntimeHost(settings, next_provider, enable_mcp=False)
    try:
        continued = await reopened.continue_session(result.session_id)
        assert continued.status == "completed"
        assert next_provider.requests[0].phase == "finalization"
        assert next_provider.requests[0].tools == []
        assert sum(
            block.get("type") == "tool_result"
            for message in next_provider.requests[0].messages
            if isinstance(message["content"], list)
            for block in message["content"]
        ) == 1
        fresh = await reopened.run(RunRequest(
            "New task", result.session_id, generation={"response_format": "json_object"},
        ))
        assert fresh.status == "interrupted"
        assert next_provider.requests[1].phase == "tools" and next_provider.requests[1].tools
    finally:
        await reopened.close()


async def test_cancel_in_committed_draft_window_restores_phase(json_settings):
    host = RuntimeHost(json_settings, ScriptedProvider([{"text": "draft"}]), enable_mcp=False)
    session = host.create_session()

    class CancelAfterDraft:
        async def emit(self, event):
            if event["type"] == "model.response" and event["payload"].get("draft_for"):
                raise asyncio.CancelledError

    try:
        task = asyncio.create_task(host.run(RunRequest(
            "Inspect", session, generation={"response_format": "json_object"},
        ), sink=CancelAfterDraft()))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.store.latest_run(session)["status"] == "interrupted"
        host.provider = ScriptedProvider([{"text": '{"ok":true}'}])
        result = await host.continue_session(session)
        assert result.status == "completed"
        assert host.provider.requests[0].phase == "finalization"
        assert host.provider.requests[0].tools == []
    finally:
        await host.close()


async def test_budget_refuses_finalization_without_losing_draft(json_settings):
    host = RuntimeHost(json_settings, ScriptedProvider([
        {"text": "draft", "usage": {"input_tokens": 2, "output_tokens": 1}},
        {"text": '{"ok":true}'},
    ]), enable_mcp=False)
    scope = BudgetScope("project", host.default_workspace.project_id)

    class BlockAfterDraft:
        async def emit(self, event):
            if event["type"] == "model.response" and event["payload"].get("draft_for"):
                host.configure_budget(scope, token_limit=0, request_id="block-finalization")

    try:
        result = await host.run(RunRequest(
            "Inspect", generation={"response_format": "json_object"},
        ), sink=BlockAfterDraft())
        assert result.status == "interrupted" and len(host.provider.requests) == 1
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 1
        assert ledger["reported_totals"] == {"input_tokens": 2, "output_tokens": 1}
        host.configure_budget(scope, token_limit=None, request_id="allow-finalization")
        assert (await host.continue_session(result.session_id)).status == "completed"
        assert host.provider.requests[-1].tools == []
    finally:
        await host.close()


async def test_unknown_tool_outcome_stops_before_json_finalization(json_settings):
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "shell", "name": "bash", "arguments": {"command": "work"}}]},
        {"text": "must not finalize"},
    ])
    host = RuntimeHost(json_settings, provider, enable_mcp=False)
    host.executor.command_executor = UnknownExecutor()
    try:
        result = await host.run(RunRequest(
            "Investigate", mode="agent", generation={"response_format": "json_object"},
        ))
        assert result.status == "interrupted" and len(provider.requests) == 1
        assert host.store.get_run(result.run_id)["reason"] == "unknown_tool_outcome"
        assert not any(event["payload"].get("draft_for")
                       for event in host.store.run_events(result.run_id))
        with pytest.raises(ValueError, match="unresolved"):
            await host.continue_session(result.session_id)
    finally:
        await host.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_finalization_stream_failure_or_cancel_never_replays_tools(json_settings, cancel):
    class Stream:
        def __init__(self):
            self.requests = []
            self.waiting = asyncio.Event()
            self.closed = False

        async def stream(self, request):
            self.requests.append(request)
            if request.phase == "tools":
                yield ModelStreamEvent("response_completed", response=ModelResponse(
                    "draft", usage={"input_tokens": 2, "output_tokens": 1},
                ))
                return
            try:
                yield ModelStreamEvent("text_delta", text='{"partial":')
                if cancel:
                    self.waiting.set()
                    await asyncio.Event().wait()
                raise ProviderError("connection lost", retryable=True)
            finally:
                self.closed = True

    provider = Stream()
    host = RuntimeHost(json_settings, provider, enable_mcp=False)
    try:
        run_id = await host.admit_run(RunRequest(
            "Inspect", generation={"response_format": "json_object"},
        ))
        task = host.start_admitted_run(run_id)
        if cancel:
            await asyncio.wait_for(provider.waiting.wait(), 5)
            row = await host.cancel_run(run_id)
            assert row["status"] == "interrupted"
            await asyncio.gather(task, return_exceptions=True)
        else:
            assert (await task).status == "failed"
        assert provider.closed and len(provider.requests) == 2
        assert provider.requests[-1].tools == []
        assert host.previews.get(run_id) is None
        ledger = build_usage_ledger(host.store.usage_events(run_id=run_id))
        assert ledger["attempt_count"] == 2 and ledger["unknown_attempts"] == 1
        assert ledger["calls"][-1]["phase"] == "finalization"
        assert not any(event["type"] == "tool.prepared" for event in host.store.run_events(run_id))
        if cancel:
            session = host.store.get_run_identity(run_id)["session_id"]
            host.provider = ScriptedProvider([{
                "text": '{"ok":true}', "usage": {"input_tokens": 1, "output_tokens": 1},
            }])
            assert (await host.continue_session(session)).status == "completed"
            assert len(host.provider.requests) == 1
            assert host.provider.requests[0].phase == "finalization"
            assert host.provider.requests[0].tools == []
            assert build_usage_ledger(host.store.session_events(session))["unknown_attempts"] == 1
    finally:
        await host.close()
