"""Generation inheritance, zero/null semantics, wire mapping and request ownership."""

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from eventide.api import create_app
from eventide.generation import GenerationCapabilities, GenerationPatch, resolve_generation
from eventide.host import RuntimeHost
from eventide.models import ModelRequest, ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.providers.openai_compatible import OpenAICompatibleProvider
from eventide.providers.openai_responses import OpenAIResponsesProvider
from tests.configuration_requests import put_configuration
from tests.test_host import make_repo
from tests.test_runtime import settings_for

DECLARED = GenerationCapabilities(source="declared", temperature_max=2, top_p=True)


def test_null_resets_and_zero_survives():
    assert GenerationPatch(temperature=0).patch() == {"temperature": 0.0}
    assert GenerationPatch().patch() == {}
    assert GenerationPatch(temperature=None).patch() == {"temperature": None}
    values = resolve_generation(
        [{"temperature": 0.7, "top_p": 0.9}, {"temperature": None}],
        provider="openai_compatible",
        base_url=None,
        model="custom",
        declared=DECLARED,
    )
    assert values == {"top_p": 0.9}


@pytest.mark.parametrize(
    "patch",
    [
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"temperature": "0"},
        {"temperature": True},
        {"temperature": -0.1},
        {"top_p": 0},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"seed": 1},
    ],
)
def test_invalid_parameters_are_rejected(patch):
    with pytest.raises(ValidationError):
        GenerationPatch.model_validate(patch)


def test_unknown_support_and_deepseek_ineffective_options():
    with pytest.raises(ValueError, match="unknown"):
        resolve_generation(
            [{"temperature": 0}], provider="openai_responses", base_url=None, model="x"
        )
    kwargs = dict(
        provider="openai_responses", base_url="https://api.deepseek.com", model="deepseek-flash"
    )
    with pytest.raises(ValueError, match="no effect"):
        resolve_generation([{"temperature": 0}], **kwargs)
    with pytest.raises(ValueError, match="0.95"):
        resolve_generation([{"top_p": 0.9}], **kwargs)
    assert (
        resolve_generation([{"reasoning_effort": "none", "temperature": 0}], **kwargs)[
            "temperature"
        ]
        == 0
    )
    assert resolve_generation([{"reasoning_effort": "max"}], **kwargs)["reasoning_effort"] == "max"


async def test_session_inheritance_freezes_at_admission_and_continue(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), max_steps=1, state_dir=isolated_workspace / "state")
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "missing"}}]},
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings, provider)
    sid = host.create_session()
    host.configure_generation(settings.model, {"temperature": 0.6}, declared=DECLARED)
    host.configure_generation(
        settings.model,
        {"temperature": 0, "max_output_tokens": 123},
        session_id=sid,
    )
    try:
        run_id = await host.admit_run(RunRequest("read", sid))
        host.configure_generation(settings.model, {"temperature": 0.9}, session_id=sid)
        first = await host.start_admitted_run(run_id)
        assert first.status == "interrupted"
        assert provider.requests[0].generation["temperature"] == 0
        assert provider.requests[0].max_tokens == 123
        assert (await host.continue_session(sid)).status == "completed"
        assert provider.requests[1].generation["temperature"] == 0
        assert provider.requests[1].max_tokens == 123
    finally:
        await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert reopened.generation_settings(settings.model, sid)["session"]["temperature"] == 0.9
    finally:
        await reopened.close()


async def test_requests_for_different_workspaces_do_not_share_parameters(isolated_workspace):
    seen = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class Provider:
        async def complete(self, request):
            seen.append(request.generation)
            if len(seen) == 2:
                entered.set()
            await release.wait()
            return ModelResponse("done")

    host = RuntimeHost(settings_for(isolated_workspace), Provider())
    other = isolated_workspace / "other"
    other.mkdir()
    ws = host.resolve_or_register_workspace(other)
    host.configure_generation(host.settings.model, {}, declared=DECLARED)
    one, two = host.create_session(), host.create_session(workspace_id=ws.workspace_id)
    tasks = [
        asyncio.create_task(host.run(RunRequest("work", sid, generation={"temperature": temp})))
        for sid, temp in [(one, 0), (two, 0.8)]
    ]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert sorted(item["temperature"] for item in seen) == [0, 0.8]
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()


@pytest.mark.parametrize("kind", ["openai_compatible", "openai_responses"])
async def test_adapter_sends_zero_and_maximum_without_mutation(isolated_workspace, kind):
    settings = replace(settings_for(isolated_workspace), provider=kind, api_key="fake")
    cls = OpenAIResponsesProvider if kind == "openai_responses" else OpenAICompatibleProvider
    provider = cls(settings)
    await provider.close()
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        if kind == "openai_responses":
            return SimpleNamespace(output=[], output_text="done", status="completed")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=[]), finish_reason="stop"
                )
            ]
        )

    provider._client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    generation = {"temperature": 0, "max_output_tokens": 123}
    await provider.complete(ModelRequest("s", [], [], "model", generation=generation))
    assert captured["temperature"] == 0
    assert captured["max_output_tokens" if kind == "openai_responses" else "max_tokens"] == 123
    assert generation == {"temperature": 0, "max_output_tokens": 123}


def test_generation_api_declares_capability_and_rejects_unsupported_values(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        model = host.settings.model
        before = client.get("/api/generation/defaults", params={"model": model}).json()
        assert before["capabilities"]["source"] == "unknown"
        assert (
            put_configuration(
                client,
                "/api/generation/defaults",
                json={
                    "model": model,
                    "generation": {"temperature": 0},
                },
            ).status_code
            == 422
        )
        declared = put_configuration(
            client,
            "/api/generation/defaults",
            json={
                "model": model,
                "generation": {"temperature": 0.5},
                "capabilities": {"temperature_max": 1, "top_p": True},
            },
        )
        assert declared.status_code == 200
        assert declared.json()["capabilities"]["source"] == "declared"
        sid = client.post("/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}).json()[
            "session_id"
        ]
        assert (
            put_configuration(
                client,
                f"/api/sessions/{sid}/generation",
                json={
                    "model": model,
                    "generation": {"temperature": 0},
                },
            ).status_code
            == 200
        )
        assert client.get(f"/api/sessions/{sid}/generation").json()["session"] == {"temperature": 0}
        assert (
            client.post(
                f"/api/sessions/{sid}/runs",
                json={
                    "prompt": "work",
                    "generation": {"temperature": 1.5},
                },
            ).status_code
            == 422
        )
        reset = put_configuration(
            client,
            f"/api/sessions/{sid}/generation",
            json={
                "model": model,
                "generation": {"temperature": None},
            },
        )
        assert reset.json()["session"] == {"temperature": None}


async def test_attempts_record_unknown_failures_and_distinct_identities(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"error": "503 busy", "retryable": True},
            {"text": "done", "usage": {"input_tokens": 0, "output_tokens": 3}},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work"))
        attempts = [
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "model.attempt_finished"
        ]
        assert len(attempts) == 2
        assert attempts[0]["usage_source"] == "unknown"
        assert "usage" not in attempts[0]
        assert attempts[1]["usage"] == {"input_tokens": 0, "output_tokens": 3}
        assert attempts[0]["request_id"] == attempts[1]["request_id"]
        assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    finally:
        await host.close()


async def test_session_model_switch_and_default_limit_survive_restart(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(
        settings_for(repo), state_dir=isolated_workspace / "state", max_steps=1, max_tokens=100
    )
    provider = ScriptedProvider(
        [{"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "missing"}}]}]
    )
    host = RuntimeHost(settings, provider)
    session = host.create_session()
    for model, temperature in [("one", 0.1), ("two", 0.8)]:
        host.configure_generation(model, {}, declared=DECLARED)
        host.configure_generation(model, {"temperature": temperature}, session_id=session)
    try:
        assert host.generation_settings("one", session)["session"] == {"temperature": 0.1}
        await host.run(RunRequest("read", session, model="one"))
        assert provider.requests[0].max_tokens == 100
    finally:
        await host.close()
    next_provider = ScriptedProvider([{"text": "done"}])
    reopened = RuntimeHost(replace(settings, max_tokens=200), next_provider)
    try:
        assert reopened.generation_settings("two", session)["session"] == {"temperature": 0.8}
        await reopened.continue_session(session)
        assert next_provider.requests[0].generation["temperature"] == 0.1
        assert next_provider.requests[0].max_tokens == 100
    finally:
        await reopened.close()


async def test_compatible_reasoning_is_replayed_for_tool_roundtrip(isolated_workspace):
    from eventide.providers.openai_compatible import convert_messages

    provider = OpenAICompatibleProvider(replace(settings_for(isolated_workspace), api_key="fake"))
    assert provider._client.max_retries == 0
    await provider.close()

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="next", tool_calls=[], reasoning_content="opaque reasoning"
                    ),
                    finish_reason="stop",
                )
            ]
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    response = await provider.complete(ModelRequest("s", [], [], "m"))
    message = {"role": "assistant", "content": response.content_blocks()}
    assert convert_messages([message], "s")[1]["reasoning_content"] == "opaque reasoning"
    from eventide.providers.openai_responses import convert_response_input

    assert "opaque reasoning" not in str(convert_response_input([message]))
