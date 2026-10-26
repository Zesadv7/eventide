"""MK-14 versioned model capability catalog: identity, sources, frozen refs.

Acceptance focus: same model on different endpoints never inherits
capabilities; canonical endpoint identity; unknown stays null; presets carry
source and version; explicit user declarations stay traceable; an admitted
Run keeps its frozen capability reference against later config changes; a
connection probe outcome never masquerades as tool verification.
"""

import contextlib
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.generation import GenerationCapabilities, capability_preset
from eventide.host import RuntimeHost
from eventide.model_capabilities import (
    CATALOG_VERSION,
    canonical_endpoint,
    capability_record,
    connection_verification,
    endpoint_identity,
)
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.workspace import digest
from tests.configuration_requests import put_configuration
from tests.test_host import make_repo
from tests.test_runtime import settings_for

BIGMODEL = "https://open.bigmodel.cn/api/coding/paas/v4"


def test_same_model_on_different_endpoints_never_inherits():
    documented = capability_record(
        provider="openai_compatible", base_url=BIGMODEL, model="glm-5.3-flash",
        declared=None, model_identity="a", config_saved=False,
    )
    other = capability_record(
        provider="openai_compatible", base_url="https://mirror.example/v4",
        model="glm-5.3-flash",
        declared=None, model_identity="b", config_saved=False,
    )
    assert documented.source == "documented"
    assert documented.preset["key"] == "bigmodel-coding-chat/glm-5.3-flash"
    assert other.source == "unknown"
    assert other.preset is None
    assert documented.capabilities["max_output_tokens"] == 131_072
    assert other.capabilities["max_output_tokens"] is None
    assert (
        documented.identity["identity_hash"] != other.identity["identity_hash"]
    )


def test_canonical_endpoint_identity_collapses_equivalent_forms():
    default = canonical_endpoint("openai_compatible", None)
    assert default == canonical_endpoint("openai_compatible", "https://api.openai.com/v1/")
    assert default == canonical_endpoint("openai_compatible", "https://api.openai.com:443/v1")
    assert default != canonical_endpoint("openai_compatible", "https://api.openai.com/v2")
    assert canonical_endpoint("openai_compatible", BIGMODEL) != canonical_endpoint(
        "openai_compatible", "https://open.bigmodel.cn/api/paas/v4"
    )
    assert endpoint_identity("openai_compatible", BIGMODEL, "m") != endpoint_identity(
        "openai_compatible", BIGMODEL, "other"
    )


def test_unknown_capabilities_stay_null_and_tool_verification_unclaimed():
    record = capability_record(
        provider="openai_compatible", base_url=None, model="custom-model",
        declared=None, model_identity="id", config_saved=False,
    )
    assert capability_preset("openai_compatible", None, "custom-model") is None
    assert record.source == "unknown"
    assert record.capabilities["context_window"] is None
    assert record.capabilities["tools_supported"] is None
    assert record.capabilities["streaming_supported"] is None
    assert record.verification["config_saved"] is False
    assert record.verification["connection"]["status"] == "never_probed"
    assert record.verification["tool_verification"]["status"] == "not_verified"
    assert record.catalog_version == CATALOG_VERSION


def test_documented_presets_carry_source_reference_and_version():
    for provider, base_url, model, key in [
        ("openai_compatible", BIGMODEL, "glm-5.3-flash", "bigmodel-coding-chat/glm-5.3-flash"),
        ("openai_compatible", "https://api.deepseek.com", "deepseek-flash",
         "deepseek/deepseek-flash"),
    ]:
        record = capability_record(
            provider=provider, base_url=base_url, model=model,
            declared=None, model_identity="id", config_saved=False,
        )
        assert record.source == "documented"
        assert record.preset["key"] == key
        assert record.preset["reference"]
        assert record.preset["verified_at"] is None


async def test_declared_capabilities_are_traceable_overrides(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert host.generation_settings(settings.model)["capability"]["source"] == "unknown"
        host.configure_generation(
            settings.model, {"temperature": 0.5},
            declared=GenerationCapabilities(
                source="declared", temperature_max=2, top_p=True,
                context_window=128_000, tools_supported=True,
            ),
        )
        state = host.generation_settings(settings.model)
        capability = state["capability"]
        assert capability["source"] == "declared"
        assert capability["capabilities"]["context_window"] == 128_000
        assert capability["capabilities"]["tools_supported"] is True
        assert capability["capabilities"]["streaming_supported"] is None
        assert capability["overrides"]["declaration_ref"] == state["model_identity"]
        assert capability["verification"]["config_saved"] is False
    finally:
        await host.close()


async def test_admitted_run_freezes_capability_reference(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = replace(settings_for(repo), max_steps=1, state_dir=isolated_workspace / "state")
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "x"}}]},
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings, provider)
    sid = host.create_session()
    host.configure_generation(
        settings.model, {"max_output_tokens": 400},
        declared=GenerationCapabilities(
            source="declared", temperature_max=2, top_p=True, max_output_tokens=500,
        ),
    )
    try:
        run_id = await host.admit_run(RunRequest("read", sid))
        admitted = next(
            e["payload"] for e in host.store.run_events(run_id) if e["type"] == "run.admitted"
        )
        reference = admitted["capability_ref"]
        assert reference["source"] == "declared"
        assert reference["capabilities"]["max_output_tokens"] == 500
        assert reference["identity"]["identity_hash"]
        first = await host.start_admitted_run(run_id)
        assert first.status == "interrupted"

        # Later declarations shrink the limit below the frozen request; the
        # admitted Run and its frozen reference must not be rewritten.
        host.configure_generation(
            settings.model, {},
            declared=GenerationCapabilities(
                source="declared", temperature_max=2, top_p=True, max_output_tokens=300,
            ),
        )
        still = next(
            e["payload"] for e in host.store.run_events(run_id) if e["type"] == "run.admitted"
        )
        assert still["capability_ref"] == reference
        assert still["generation"]["max_output_tokens"] == 400

        continued = await host.continue_session(sid)
        assert continued.status == "completed"
        assert provider.requests[1].max_tokens == 400
        events = host.store.run_events(continued.run_id)
        continue_ref = next(
            e["payload"]["capability_ref"] for e in events if e["type"] == "run.admitted"
        )
        assert continue_ref == reference
    finally:
        await host.close()


async def test_connection_probe_outcomes_never_claim_tool_verification(
    isolated_workspace, monkeypatch,
):
    settings = replace(
        settings_for(isolated_workspace), provider="openai_compatible",
        state_dir=isolated_workspace / "state",
    )
    host = RuntimeHost(settings, ScriptedProvider([]))
    try:
        model = settings.model
        assert host.model_connection_verification(model)["status"] == "never_probed"
        with contextlib.suppress(Exception):
            await host.probe_provider(
                provider=settings.provider, api_key=None, base_url=None, model=model,
            )
        failed = host.model_connection_verification(model)
        assert failed["status"] == "failed"
        assert failed["error_type"] == "missing_api_key"
        assert failed["run_id"]
        state = host.generation_settings(model)
        assert state["capability"]["verification"]["tool_verification"]["status"] == (
            "not_verified"
        )

        # A real completed probe Run derives as passed, still without any
        # claim that real tool calls were verified against the endpoint.
        monkeypatch.setattr(
            "eventide.host.build_provider",
            lambda _settings: ScriptedProvider([{"text": "ok"}]),
        )
        await host.probe_provider(
            provider=settings.provider, api_key="probe-key", base_url=None, model=model,
        )
        passed = host.model_connection_verification(model)
        assert passed["status"] == "passed"
        assert passed["run_id"]
        assert host.generation_settings(model)["capability"]["verification"][
            "tool_verification"
        ]["status"] == "not_verified"

        # A different endpoint sharing the model name is not verified by this.
        assert connection_verification(
            host.store, settings.provider, "https://other.example/v1", model
        )["status"] == "never_probed"
    finally:
        await host.close()


def test_generation_api_exposes_card_and_verification_outside_config_hash(isolated_workspace):
    settings = replace(settings_for(isolated_workspace), provider="openai_compatible")
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        model = host.settings.model
        before = client.get("/api/generation/defaults", params={"model": model}).json()
        capability = before["capability"]
        assert capability["identity"]["provider"] == settings.provider
        assert capability["identity"]["canonical_endpoint"] == "https://api.openai.com/v1"
        assert capability["verification"]["connection"]["status"] == "never_probed"
        assert capability["verification"]["tool_verification"]["status"] == "not_verified"

        owner = host.create_session(f"probe_catalog_{uuid.uuid4().hex}")
        run_id = f"probe_{uuid.uuid4().hex}"
        host.store.create_run(run_id, owner)
        host.store.append_event(run_id, "run.started", {
            "source": "connection_probe", "provider": host.settings.provider, "model": model,
            "endpoint_hash": digest(
                host.settings.base_url
            ),
        })
        host.store.append_event(run_id, "run.failed", {
            "source": "connection_probe", "reason": "authentication", "error": "boom",
        })
        after = client.get("/api/generation/defaults", params={"model": model}).json()
        assert after["capability"]["verification"]["connection"]["status"] == "failed"
        assert after["capability"]["verification"]["connection"]["error_type"] == (
            "authentication"
        )
        # Verification evidence changed while the editable configuration
        # (and therefore its hash) did not.
        assert after["config_hash"] == before["config_hash"]
        assert put_configuration(
            client, "/api/generation/defaults",
            json={"model": model, "generation": {}},
        ).status_code in {200, 422}


def test_endpoint_identity_preserves_routing_without_exposing_credentials():
    one = endpoint_identity(
        "openai_compatible", "https://user:secret@host.test/v1?route=a&key=s", "m",
    )
    two = endpoint_identity("openai_compatible", "https://host.test/v1?route=b", "m")
    assert one["identity_hash"] != two["identity_hash"]
    assert one["canonical_endpoint"] == "https://host.test/v1"
    assert "secret" not in str(one) and "key=s" not in str(one)
    assert canonical_endpoint("openai_compatible", "https://[::1]:443/v1/") == "https://[::1]/v1"


@pytest.mark.parametrize("reference", [None, {}, {"capabilities": "invalid"}])
async def test_malformed_frozen_reference_refuses_continue(isolated_workspace, reference):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(repo), ScriptedProvider([]))
    sid = host.create_session()
    try:
        intent = host._run_intent(RunRequest("read", sid))
        intent["capability_ref"] = reference
        host.store.admit_run("broken", sid, [("run.admitted", intent, "system", "runtime")])
        with pytest.raises(ValueError, match="Frozen capability reference is invalid"):
            host._run_intent(None, source_id="broken", session_id=sid)
    finally:
        await host.close()
