"""Ephemeral previews cannot leak split credentials or dispatch partial tools."""

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.previews import PreviewStore, SafeTextBuffer
from eventide.providers import ProviderError
from eventide.usage import build_usage_ledger
from tests.test_runtime import settings_for


def test_known_secret_is_redacted_at_every_split():
    secret = "sk-stream-private-credential"
    text = "prefix " + secret + " suffix " * 10
    for index in range(1, len(text)):
        safe = SafeTextBuffer((secret,), lambda s: s.replace(secret, "[REDACTED]"))
        output = safe.feed(text[:index]) + safe.feed(text[index:]) + safe.feed("", final=True)
        assert secret not in output
        assert output == text.replace(secret, "[REDACTED]")


def test_preview_limits_and_disposal():
    store = PreviewStore(limit=5, active_limit=2)
    store.append("a", "r", "1234567")
    assert store.get("a")["text"] == "34567"
    assert store.get("a")["omitted_chars"] == 2
    store.append("b", "r", "x")
    store.append("c", "r", "y")
    assert store.get("a") is None
    store.clear("b")
    assert store.get("b") is None


async def test_partial_stream_fails_without_retry_or_tool_execution(isolated_workspace):
    class PartialProvider:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            yield SimpleNamespace(kind="text_delta", text="partial answer", response=None)
            raise ProviderError("connection lost", retryable=True)

    provider = PartialProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert provider.calls == 1
        assert host.previews.get(result.run_id) is None
        assert not any(e["type"] == "tool.prepared" for e in host.store.run_events(result.run_id))
        assert (
            build_usage_ledger(host.store.usage_events(run_id=result.run_id))["unknown_attempts"]
            == 1
        )
    finally:
        await host.close()


async def test_stream_timeout_never_retries(isolated_workspace):
    class SlowProvider:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            yield SimpleNamespace(kind="text_delta", text="waiting", response=None)
            await asyncio.Event().wait()

    provider = SlowProvider()
    host = RuntimeHost(replace(settings_for(isolated_workspace), model_timeout=0.05), provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "failed"
        assert provider.calls == 1
    finally:
        await host.close()


async def test_complete_stream_executes_only_finished_tools_and_preserves_usage(isolated_workspace):
    class Provider:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield SimpleNamespace(kind="text_delta", text="checking files", response=None)
                yield SimpleNamespace(
                    kind="response_completed",
                    text="",
                    response=ModelResponse(
                        "",
                        (ToolCall("read", "read_file", {"path": "missing"}),),
                        "tool_use",
                        {"input_tokens": 10, "output_tokens": 2},
                    ),
                )
            else:
                yield SimpleNamespace(
                    kind="response_completed",
                    text="",
                    response=ModelResponse("done", usage={"input_tokens": 12, "output_tokens": 3}),
                )

    host = RuntimeHost(settings_for(isolated_workspace), Provider())
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        events = host.store.run_events(result.run_id)
        assert len([e for e in events if e["type"] == "tool.prepared"]) == 1
        assert not any(e["type"] == "model.preview" for e in events)
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["reported_totals"] == {"input_tokens": 22, "output_tokens": 5}
        assert ledger["complete"] is True
    finally:
        await host.close()


def test_preview_sse_has_no_persistent_id_and_is_not_written_to_log(isolated_workspace):
    text = "visible preview " * 10

    class Provider:
        async def stream(self, request):
            yield SimpleNamespace(kind="text_delta", text=text, response=None)
            await asyncio.sleep(0.6)
            yield SimpleNamespace(
                kind="response_completed",
                text="",
                response=ModelResponse(text, usage={"input_tokens": 1, "output_tokens": 2}),
            )

    host = RuntimeHost(settings_for(isolated_workspace), Provider())
    with TestClient(create_app(host)) as client:
        sid = client.post("/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}).json()[
            "session_id"
        ]
        run = client.post(
            f"/api/sessions/{sid}/runs",
            json={"prompt": "work"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        response = client.get(f"/api/runs/{run}/events?include_preview=true")
        assert response.status_code == 200
        frames = response.text.split("\n\n")
        previews = [frame for frame in frames if "event: model.preview\n" in frame]
        assert previews
        assert all(not frame.startswith("id:") for frame in previews)
        assert not any(e["type"] == "model.preview" for e in host.store.run_events(run))
        assert client.get(f"/api/runs/{run}").json()["status"] == "completed"


async def test_truncated_response_never_dispatches_valid_looking_write(isolated_workspace):
    from eventide.providers import ScriptedProvider

    host = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider(
            [
                {
                    "stop_reason": "max_output_tokens",
                    "tool_calls": [
                        {
                            "id": "truncated",
                            "name": "write_file",
                            "arguments": {
                                "path": "must-not-exist",
                                "content": "looks complete",
                            },
                        }
                    ],
                    "usage": {"input_tokens": 7, "output_tokens": 100},
                }
            ]
        ),
    )
    try:
        result = await host.run(RunRequest("work", mode="agent"))
        assert result.status == "failed"
        assert not (isolated_workspace / "must-not-exist").exists()
        events = host.store.run_events(result.run_id)
        assert not any(event["type"] == "tool.prepared" for event in events)
        assert any(event["type"] == "tool.abandoned" for event in events)
        assert host.store.load_messages(result.session_id)
        assert build_usage_ledger(host.store.usage_events(run_id=result.run_id))[
            "reported_totals"
        ] == {
            "input_tokens": 7,
            "output_tokens": 100,
        }
    finally:
        await host.close()
