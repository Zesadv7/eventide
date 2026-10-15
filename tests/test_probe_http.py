"""Persistent connection-check retries never create a second paid attempt."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.http_control import begin_mutation
from eventide.provider_probe import probe_owner
from eventide.providers import ProviderError, ScriptedProvider
from tests.test_provider_probe import ProbeProvider, ledger, make_host
from tests.test_runtime import settings_for

BODY = {
    "provider": "openai_compatible",
    "model": "candidate-model",
    "api_key": "candidate-secret",
    "base_url": None,
}
HEADERS = {"Idempotency-Key": "probe-retry"}
URL = "/api/config/provider/test"


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 129])
def test_probe_rejects_bad_identity_before_creating_owner(isolated_workspace, monkeypatch, key):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    with TestClient(create_app(host)) as client:
        response = client.post(
            URL, json=BODY, headers={} if key is None else {"Idempotency-Key": key}
        )
        assert response.status_code == 422
        assert not provider.requests
        assert not host.store.facts_by_type(("control.session", "http.mutation_started"))


@pytest.mark.parametrize("failure", [False, True])
def test_probe_replays_exact_result_and_rejects_changed_content(
    isolated_workspace,
    monkeypatch,
    failure,
):
    provider = ProbeProvider(error=ProviderError("401 candidate-secret") if failure else None)
    host = make_host(isolated_workspace, monkeypatch, provider)
    with TestClient(create_app(host)) as client:
        first = client.post(URL, json=BODY, headers=HEADERS)
        repeated = client.post(URL, json=BODY, headers=HEADERS)
        assert first.status_code == repeated.status_code == 200
        assert repeated.json() == first.json()
        for field, value in (
            ("model", "other"),
            ("api_key", "other-key"),
            ("base_url", "https://other.invalid"),
        ):
            assert client.post(URL, json={**BODY, field: value}, headers=HEADERS).status_code == 409
        assert len(provider.requests) == 1
        assert len(host.budget_status()["reservations"]) == 1
        assert "candidate-secret" not in json.dumps(host.store.session_events(probe_owner(host)))


async def test_probe_inflight_retries_are_traceable_without_dispatch(
    isolated_workspace, monkeypatch
):
    provider = ProbeProvider(wait=True)
    host = make_host(isolated_workspace, monkeypatch, provider)
    await host.initialize()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(host)), base_url="http://127.0.0.1"
        ) as client:
            task = asyncio.create_task(client.post(URL, json=BODY, headers=HEADERS))
            await provider.entered.wait()
            duplicate = await client.post(URL, json=BODY, headers=HEADERS)
            assert duplicate.status_code == 409
            run_id = duplicate.headers["X-Eventide-Run-ID"]
            assert host.store.get_run(run_id)["status"] == "running"
            provider.release.set()
            result = await task
            assert result.json()["run_id"] == run_id
            assert (await client.post(URL, json=BODY, headers=HEADERS)).json() == result.json()
            assert len(provider.requests) == 1
    finally:
        await host.close()


async def test_cancelled_http_recovers_terminal_without_repeating_unknown_attempt(
    isolated_workspace,
    monkeypatch,
):
    provider = ProbeProvider(wait=True)
    host = make_host(isolated_workspace, monkeypatch, provider)
    await host.initialize()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(host)), base_url="http://127.0.0.1"
        ) as client:
            task = asyncio.create_task(client.post(URL, json=BODY, headers=HEADERS))
            await provider.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            response = await client.post(URL, json=BODY, headers=HEADERS)
            result = response.json()
            assert response.status_code == 200
            assert result["recovered"] and not result["success"]
            assert result["latency_ms"] is None and result["checked_at"] is None
            assert result["usage"]["unknown_attempts"] == 1
            assert len(provider.requests) == 1
            assert host.budget_status()["reservations"][0]["status"] == "unsettled"
    finally:
        await host.close()


def test_probe_timeout_result_is_persisted_and_never_reissued(isolated_workspace, monkeypatch):
    monkeypatch.setattr("eventide.http_api.probe.PROBE_TIMEOUT", 0.01)
    provider = ProbeProvider(wait=True)
    host = make_host(isolated_workspace, monkeypatch, provider)
    with TestClient(create_app(host)) as client:
        first = client.post(URL, json=BODY, headers=HEADERS)
        assert first.status_code == 200 and first.json()["error_type"] == "timeout"
        assert first.json()["usage"]["unknown_attempts"] == 1
        assert client.post(URL, json=BODY, headers=HEADERS).json() == first.json()
        assert provider.closed and len(provider.requests) == 1


def test_completed_probe_replays_after_restart_and_saved_key_change(
    isolated_workspace, monkeypatch
):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider, api_key="first-saved-key")
    body = {key: value for key, value in BODY.items() if key != "api_key"}
    with TestClient(create_app(host)) as client:
        first = client.post(URL, json=body, headers=HEADERS).json()
        host.settings = replace(host.settings, api_key="second-saved-key")
        assert client.post(URL, json=body, headers=HEADERS).json() == first
    restored = RuntimeHost(
        settings_for(isolated_workspace, api_key="third-saved-key"),
        ScriptedProvider([]),
        enable_mcp=False,
    )
    with TestClient(create_app(restored)) as client:
        assert client.post(URL, json=body, headers=HEADERS).json() == first
        assert len(provider.requests) == 1


def test_missing_http_receipt_recovers_completed_run_without_model_call(
    isolated_workspace,
    monkeypatch,
):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    from eventide.http_api import probe

    original = probe.finish_mutation

    def fail_once(*args):
        monkeypatch.setattr(probe, "finish_mutation", original)
        raise OSError("interrupted before response receipt")

    monkeypatch.setattr(probe, "finish_mutation", fail_once)
    with TestClient(create_app(host), raise_server_exceptions=False) as client:
        assert client.post(URL, json=BODY, headers=HEADERS).status_code == 500
        response = client.post(URL, json=BODY, headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["recovered"] and response.json()["success"]
        assert response.json()["usage"]["reported_totals"] == {
            "input_tokens": 8,
            "output_tokens": 2,
        }
        assert len(provider.requests) == 1


async def test_nonterminal_probe_receipt_remains_unknown_without_redispatch(
    isolated_workspace,
    monkeypatch,
):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    await host.initialize()
    owner = probe_owner(host)
    request = Request({"type": "http", "headers": [(b"idempotency-key", b"probe-retry")]})
    receipt = begin_mutation(
        host, request, owner, "provider.probe", {**BODY, "clear_api_key": False}
    )
    run_id = "probe_" + receipt.identity.removeprefix("mutation_")
    host.store.create_run(run_id, owner)
    host.store.append_event(
        run_id,
        "run.started",
        {
            "source": "connection_probe",
            "billing_scope": "service",
            "project_id": None,
        },
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(host)), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(URL, json=BODY, headers=HEADERS)
            assert response.status_code == 409
            assert response.headers["X-Eventide-Run-ID"] == run_id
            assert not provider.requests
            assert not host.store.facts_by_type(("http.mutation_completed",))
    finally:
        await host.close()


@pytest.mark.parametrize("has_run", [False, True])
async def test_receipt_crash_gap_recovers_without_replaying(
    isolated_workspace, monkeypatch, has_run
):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    await host.initialize()
    owner = probe_owner(host)
    request = Request({"type": "http", "headers": [(b"idempotency-key", b"probe-retry")]})
    receipt = begin_mutation(
        host, request, owner, "provider.probe", {**BODY, "clear_api_key": False}
    )
    run_id = "probe_" + receipt.identity.removeprefix("mutation_")
    if has_run:
        host.store.create_run(run_id, owner)
        host.store.append_event(
            run_id,
            "run.started",
            {
                "source": "connection_probe",
                "billing_scope": "service",
                "project_id": None,
            },
        )
        host.model_budget.ledger.reserve(
            owner,
            run_id,
            "attempt",
            context=host.model_budget.context(run_id)[1],
            input_estimate=8,
            output_estimate=32,
            estimate_method="test",
            attempt_started={
                "request_id": "request",
                "attempt_id": "attempt",
                "kind": "connection_probe",
            },
        )
    await host.close()
    restored = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]), enable_mcp=False)
    await restored.initialize()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(restored)), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(URL, json=BODY, headers=HEADERS)
            assert response.status_code == 200
            result = response.json()
            assert result["recovered"] and not result["success"]
            assert result["run_id"] == (run_id if has_run else None)
            assert result["usage"]["unknown_attempts"] == int(has_run)
            assert not provider.requests
            if has_run:
                assert ledger(restored, run_id)["attempt_count"] == 1
            assert "candidate-secret" not in json.dumps(restored.store.session_events(owner))
    finally:
        await restored.close()
