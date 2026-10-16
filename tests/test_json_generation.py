"""Explicit JSON output is frozen, capability checked and never repaired silently."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from eventide.api import create_app
from eventide.bundled_evaluations import load_builtin_suite
from eventide.evaluation_control import EvaluationController
from eventide.evaluation_quote import evaluation_quote
from eventide.generation import (
    GenerationCapabilities,
    GenerationPatch,
    capabilities,
    resolve_generation,
    wire_parameters,
)
from eventide.host import RuntimeHost
from eventide.models import ModelRequest, RunRequest
from eventide.providers import ScriptedProvider
from eventide.providers.openai_compatible import OpenAICompatibleProvider
from tests.configuration_requests import put_configuration
from tests.test_api import wait_for_run
from tests.test_host import make_repo
from tests.test_provider_streaming import chat_chunk
from tests.test_provider_streaming import provider as streaming_provider
from tests.test_runtime import settings_for

ENDPOINT = "https://open.bigmodel.cn/api/coding/paas/v4/"
DECLARED = GenerationCapabilities(source="declared", response_formats=("text", "json_object"))


def test_json_patch_omission_null_and_chat_wire_intent():
    assert GenerationPatch().patch() == {}
    assert GenerationPatch(response_format=None).patch() == {"response_format": None}
    resolved = resolve_generation(
        [{"response_format": "json_object"}, {"response_format": None}],
        provider="openai_compatible", base_url=ENDPOINT, model="glm-5.3-flash",
    )
    assert resolved == {}
    assert "response_format" not in wire_parameters("openai_compatible", resolved)
    for value in ("text", "json_object"):
        patch = GenerationPatch(response_format=value).patch()
        assert wire_parameters("openai_compatible", patch)["response_format"] == {"type": value}
    with pytest.raises(ValidationError):
        GenerationPatch.model_validate({"response_format": "json_schema"})


@pytest.mark.parametrize("value", ["text", "json_object"])
@pytest.mark.parametrize(
    ("protocol", "endpoint", "model", "declared"),
    [
        ("anthropic", ENDPOINT, "glm-5.3-flash", DECLARED),
        ("openai_responses", ENDPOINT, "glm-5.3-flash", DECLARED),
        ("openai_compatible", "https://other.invalid/v1", "custom", None),
        ("openai_compatible", ENDPOINT, "other-model", None),
        ("openai_compatible", ENDPOINT + "unknown/", "glm-5.3-flash", None),
    ],
)
def test_unsupported_json_protocol_or_capability_is_rejected(
    value, protocol, endpoint, model, declared,
):
    with pytest.raises(ValueError, match="Response format"):
        resolve_generation(
            [{"response_format": value}], provider=protocol, base_url=endpoint,
            model=model, declared=declared,
        )


def test_chat_declared_and_exact_glm_capabilities():
    assert capabilities("openai_compatible", ENDPOINT, "glm-5.3-flash").response_formats == (
        "text", "json_object",
    )
    assert resolve_generation(
        [{"response_format": "json_object"}], provider="openai_compatible",
        base_url="https://custom.invalid/v1", model="custom", declared=DECLARED,
    ) == {"response_format": "json_object"}
    for protocol in ("anthropic", "openai_responses"):
        with pytest.raises(ValueError, match="Response format"):
            wire_parameters(protocol, {"response_format": "json_object"})


async def test_chat_json_parameter_reaches_complete_and_stream():
    instance = object.__new__(OpenAICompatibleProvider)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"done":true}', tool_calls=[]), finish_reason="stop",
        )])

    instance._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    request = ModelRequest(
        "Return JSON", [], [], "model", generation={"response_format": "json_object"},
    )
    assert (await instance.complete(request)).text == '{"done":true}'
    stream, _, stream_calls = streaming_provider("chat", [
        chat_chunk(content='{"done":true}', finish="stop"),
    ])
    events = [event async for event in stream.stream(request)]
    assert events[-1].response.text == '{"done":true}'
    assert calls[0]["response_format"] == stream_calls[0]["response_format"] == {
        "type": "json_object",
    }


@pytest.mark.parametrize("text", [
    '```json\n{"ok":true}\n```', '{"ok":true}\nExplanation', '[]', 'null',
    '{"number":NaN}', '{"number":Infinity}', '{"unfinished":',
])
async def test_invalid_final_json_is_preserved_then_fails_without_retry(isolated_workspace, text):
    settings = replace(
        settings_for(isolated_workspace), provider="openai_compatible",
        base_url=ENDPOINT, model="glm-5.3-flash",
    )
    provider = ScriptedProvider([
        {"text": "draft", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"text": text, "usage": {"input_tokens": 4, "output_tokens": 3}},
    ])
    host = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        result = await host.run(RunRequest("Answer", generation={"response_format": "json_object"}))
        assert result.status == "failed" and "response_format=json_object" in result.output
        assert len(provider.requests) == 2
        assert "one JSON object" in provider.requests[1].system
        events = host.store.run_events(result.run_id)
        response = [event for event in events if event["type"] == "model.response"][-1]
        assert response["payload"]["text"] == text
        assert events.index(response) < len(events) - 1
        assert events[-1]["type"] == "run.failed"
        assert not any(event["type"] == "model.retry" for event in events)
        assert result.usage == {"input_tokens": 5, "output_tokens": 4}
    finally:
        await host.close()


async def test_json_freezes_for_tool_rounds_continue_and_null_reset(isolated_workspace):
    source = make_repo(isolated_workspace / "source")
    settings = replace(
        settings_for(source), provider="openai_compatible", base_url=ENDPOINT,
        model="glm-5.3-flash", max_steps=2, state_dir=isolated_workspace / "state",
    )
    (source / "code.py").write_text("value = 1\n")
    provider = ScriptedProvider([
        {"text": "I will inspect first", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "code.py"}},
        ]},
        {"text": ' {"ok":true} '},
        {"text": ' {"ok":true} '},
        {"text": "ordinary text"},
    ])
    host = RuntimeHost(settings, provider, enable_mcp=False)
    try:
        host.configure_generation(settings.model, {"response_format": "json_object"})
        first = await host.run(RunRequest("Inspect"))
        assert first.status == "interrupted"
        host.configure_generation(settings.model, {"response_format": "text"})
        continued = await host.continue_session(first.session_id)
        assert continued.status == "completed" and continued.output == ' {"ok":true} '
        assert all("response_format" not in request.generation
                   for request in provider.requests[:2])
        assert provider.requests[2].generation["response_format"] == "json_object"
        plain = await host.run(RunRequest("Answer", generation={"response_format": None}))
        assert plain.status == "completed"
        assert "response_format" not in provider.requests[3].generation
    finally:
        await host.close()


async def test_controlled_facts_quote_and_calls_freeze_json_without_changing_repairs(
    isolated_workspace,
):
    source = make_repo(isolated_workspace / "source")
    suite = load_builtin_suite("engineering-v1", split="development")
    script = [entry for case in suite["cases"] for entry in (
        case["script"] + ([case["script"][-1]] if case["grade"]["kind"] == "facts" else [])
    )]
    settings = replace(
        settings_for(source), provider="openai_compatible", base_url=ENDPOINT,
        model="glm-5.3-flash", state_dir=isolated_workspace / "state",
    )
    host = RuntimeHost(settings, ScriptedProvider(script), enable_mcp=False)
    controller = EvaluationController(host)
    try:
        project = host.default_workspace.project_id
        quote = evaluation_quote(host, project)
        assert quote["fact_response_format"] == "json_object"
        await controller.admit(
            project, "json-evaluation", suite_id=suite["id"], suite_sha256=suite["suite_sha256"],
            mode="live", confirmed=True, quote_hash=quote["quote_hash"],
        )
        accepted = next(event["payload"] for event in host.store.run_events("json-evaluation")
                        if event["type"] == "evaluation.accepted")
        assert accepted["fact_response_format"] == accepted["planning_quote"][
            "fact_response_format"
        ]
        host.configure_generation(settings.model, {}, declared=GenerationCapabilities(
            source="declared", response_formats=("text",),
        ))
        assert evaluation_quote(host, project)["fact_response_format"] is None
        assert (await controller.start("json-evaluation")).status == "completed"
        report = controller.report("json-evaluation")
        assert report["passed"] == 12
        assert report["provenance"]["fact_response_format"] == "json_object"
        categories = {case["id"]: case["benchmark"]["category"] for case in suite["cases"]}
        attempts = [event["payload"] for event in host.store.run_events("json-evaluation")
                    if event["type"] == "model.attempt_started"]
        assert attempts
        for attempt in attempts:
            category = categories[attempt["evaluation_origin"]["case_id"]]
            assert attempt["generation"].get("response_format") == (
                "json_object" if category in {"understanding", "diagnosis"}
                and attempt["phase"] == "finalization" else None
            )
    finally:
        await host.close()


def test_json_http_requires_chat_capability_and_accepts_explicit_declaration(isolated_workspace):
    settings = replace(
        settings_for(isolated_workspace), provider="openai_compatible",
        base_url="https://custom.invalid/v1", model="custom",
    )
    provider = ScriptedProvider([{"text": "draft"}, {"text": '{"ok":true}'}])
    host = RuntimeHost(settings, provider, enable_mcp=False)
    session = host.create_session()
    with TestClient(create_app(host)) as client:
        path = f"/api/sessions/{session}/runs"
        rejected = client.post(path, json={
            "prompt": "Answer", "generation": {"response_format": "json_object"},
        }, headers={"Idempotency-Key": "unsupported"})
        assert rejected.status_code == 422
        assert provider.requests == []
        declared = put_configuration(client, "/api/generation/defaults", json={
            "model": "custom", "generation": {"response_format": "json_object"},
            "capabilities": {"response_formats": ["text", "json_object"]},
        })
        assert declared.status_code == 200
        assert declared.json()["capabilities"]["source"] == "declared"
        assert declared.json()["capabilities"]["response_formats"] == ["text", "json_object"]
        accepted = client.post(path, json={"prompt": "Answer"}, headers={"Idempotency-Key": "json"})
        assert accepted.status_code == 202
        assert wait_for_run(client, accepted.json()["run_id"])["status"] == "completed"
        assert "response_format" not in provider.requests[0].generation
        assert provider.requests[1].generation["response_format"] == "json_object"
