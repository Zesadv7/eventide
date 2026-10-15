"""Connection probes are real audited service calls, never user Agent runs."""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.budget_prices import PriceVersion
from eventide.budget_views import cost_view
from eventide.budgets import BudgetContext, BudgetExceeded, BudgetScope
from eventide.host import RuntimeHost
from eventide.models import ModelResponse
from eventide.provider_probe import ProbeError, probe_owner
from eventide.providers import ProviderError, ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_runtime import settings_for


class ProbeProvider:
    def __init__(self, response=None, error=None, wait=False):
        self.response = response or ModelResponse(
            "ok", usage={"input_tokens": 8, "output_tokens": 2}
        )
        self.error = error
        self.wait = wait
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def complete(self, request):
        self.requests.append(request)
        self.entered.set()
        if self.wait:
            await self.release.wait()
        if self.error:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


def make_host(path, monkeypatch, provider, **settings):
    host = RuntimeHost(settings_for(path, **settings), ScriptedProvider([]), enable_mcp=False)
    monkeypatch.setattr("eventide.host.build_provider", lambda _settings: provider)
    return host


def call(host, **kwargs):
    return host.probe_provider(
        provider="openai", api_key="candidate-secret", base_url="https://example.invalid/v1",
        model="candidate-model", **kwargs,
    )


def control_run(host, run_id="probe", **started):
    owner = probe_owner(host)
    host.store.create_run(run_id, owner)
    host.store.append_event(run_id, "run.started", {
        "source": "connection_probe", "billing_scope": "service", "project_id": None,
        **started,
    })
    return owner


def ledger(host, run_id):
    return build_usage_ledger(host.store.usage_events(run_id=run_id))


async def test_probe_uses_control_run_day_budget_and_candidate_price(
    isolated_workspace, monkeypatch,
):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    original = host.settings
    try:
        host.configure_budget(
            BudgetScope("project", host.default_workspace.project_id), token_limit=0,
            request_id="unrelated-project",
        )
        for category in ("input_tokens", "output_tokens"):
            host.register_price(PriceVersion(
                category, "openai_compatible", "candidate-model", category, "CNY", "1",
                datetime(2020, 1, 1, tzinfo=timezone.utc),
            ))
        response = await call(host, run_id="probe")
        assert response.usage == {"input_tokens": 8, "output_tokens": 2}
        assert host.settings is original
        record = host.store.get_run("probe")
        assert record["status"] == "completed"
        assert host.is_control_session(record["session_id"])
        assert host.store.get_session(record["session_id"])["archived"]
        assert host.store.load_messages(record["session_id"]) == []
        assert host.dispatch.scheduler.entries() == ()
        assert not host.runtime_busy()
        with pytest.raises(ValueError, match="Worker sessions"):
            await host.admit_continue(record["session_id"], "no-continue")
        usage = ledger(host, "probe")
        assert usage["attempt_count"] == 1
        assert usage["calls"][0]["kind"] == "connection_probe"
        assert usage["reported_totals"] == response.usage
        reservation = host.budget_status()["reservations"][0]
        assert reservation["actual_tokens"] == 10
        facts = host.store.facts_by_type(("budget.reserved",))
        assert facts[0]["payload"]["context"]["project_id"] is None
        assert {scope["kind"] for scope in facts[0]["payload"]["scopes"]} == {"run", "day"}
        assert Decimal(cost_view(host)["known_costs"]["CNY"]) == Decimal("0.00001")
        assert cost_view(host, kind="project", identity=host.default_workspace.project_id)[
            "attempt_count"
        ] == 0
        day = facts[0]["payload"]["reserved_at"][:10]
        assert cost_view(host, kind="day", identity=day)["attempt_count"] == 1
        assert provider.closed
    finally:
        await host.close()


async def test_probe_day_zero_blocks_without_request_or_attempt(isolated_workspace, monkeypatch):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    try:
        host.configure_budget(
            BudgetScope("day", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            token_limit=0, request_id="no-probes",
        )
        with pytest.raises(BudgetExceeded):
            await call(host, run_id="blocked")
        assert not provider.requests
        assert not host.budget_status()["reservations"]
        assert ledger(host, "blocked")["attempt_count"] == 0
        assert host.store.get_run("blocked")["status"] == "interrupted"
        assert provider.closed
    finally:
        await host.close()


@pytest.mark.parametrize("failure", ["missing", "provider", "timeout", "cancel"])
async def test_probe_unknown_attempts_remain_reserved(
    isolated_workspace, monkeypatch, failure,
):
    provider = ProbeProvider(
        response=ModelResponse("ok") if failure == "missing" else None,
        error=(ProviderError("503 candidate-secret", retryable=True)
               if failure == "provider" else None),
        wait=failure in {"timeout", "cancel"},
    )
    host = make_host(isolated_workspace, monkeypatch, provider, model_timeout=0.03)
    try:
        if failure == "cancel":
            task = asyncio.create_task(call(host, run_id="probe"))
            await provider.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure in {"provider", "timeout"}:
            with pytest.raises(ProbeError) as caught:
                await call(host, run_id="probe")
            assert "candidate-secret" not in str(caught.value)
            assert caught.value.__suppress_context__
        else:
            await call(host, run_id="probe")
        assert len(provider.requests) == 1
        assert ledger(host, "probe")["unknown_attempts"] == 1
        assert host.budget_status()["reservations"][0]["status"] == "unsettled"
        assert host.cost_status()["unknown_attempts"] == 1
        assert provider.closed
        assert not host._active_tasks and not host._run_tasks
        assert "candidate-secret" not in json.dumps(host.store.session_events(probe_owner(host)))
    finally:
        await host.close()


async def test_slot_wait_cancellation_records_no_call(isolated_workspace, monkeypatch):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    try:
        async with host.dispatch.scheduler.model_slot(), host.dispatch.scheduler.model_slot():
            task = asyncio.create_task(call(host, run_id="waiting"))
            for _ in range(100):
                if host.dispatch.scheduler.counts()["waiting_models"]:
                    break
                await asyncio.sleep(0)
            assert host.dispatch.scheduler.counts()["waiting_models"] == 1
            host.configure_budget(BudgetScope("run", "waiting"), token_limit=0, request_id="run")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not provider.requests
        assert not host.budget_status()["reservations"]
        assert ledger(host, "waiting")["attempt_count"] == 0
        assert host.store.get_run("waiting")["status"] == "interrupted"
        assert provider.closed
    finally:
        await host.close()


async def test_probe_run_budget_is_enforced_after_slot_wait(isolated_workspace, monkeypatch):
    provider = ProbeProvider()
    host = make_host(isolated_workspace, monkeypatch, provider)
    try:
        async with host.dispatch.scheduler.model_slot(), host.dispatch.scheduler.model_slot():
            task = asyncio.create_task(call(host, run_id="waiting"))
            while not host.dispatch.scheduler.counts()["waiting_models"]:
                await asyncio.sleep(0)
            host.configure_budget(BudgetScope("run", "waiting"), token_limit=0, request_id="run")
        with pytest.raises(BudgetExceeded):
            await task
        assert not provider.requests
    finally:
        await host.close()


async def test_repeated_cancel_does_not_finish_before_client_cleanup(
    isolated_workspace, monkeypatch,
):
    provider = ProbeProvider(wait=True)
    closing = asyncio.Event()
    release_close = asyncio.Event()

    async def close():
        closing.set()
        await release_close.wait()
        provider.closed = True

    provider.close = close
    host = make_host(isolated_workspace, monkeypatch, provider)
    task = asyncio.create_task(call(host, run_id="probe"))
    await provider.entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not host.store.run_is_terminal("probe")
    assert not task.done()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed and host.store.run_is_terminal("probe")
    await host.close()


@pytest.mark.parametrize("bad", ["ordinary", "missing_control", "wrong_control", "admitted",
                                  "parent", "delivery", "continuation", "wrong_source"])
async def test_null_project_cannot_bypass_normal_budget_ownership(isolated_workspace, bad):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        owner = host.create_session() if bad == "ordinary" else probe_owner(host)
        if bad in {"missing_control", "wrong_control"}:
            owner = host.create_session()
            if bad == "wrong_control":
                host.store.append_fact(owner, "control.session", {"kind": "budgets"})
        prior = None
        if bad == "continuation":
            host.store.create_run("prior", owner)
            prior = "prior"
        host.store.create_run("bad", owner, continuation_of=prior)
        started = {"source": "connection_probe", "billing_scope": "service", "project_id": None}
        if bad == "parent":
            started["parent_run_id"] = "parent"
        elif bad == "delivery":
            started["execution_config"] = {"writer_delivery_id": "delivery"}
        elif bad == "wrong_source":
            started["source"] = "agent"
        host.store.append_event("bad", "run.started", started)
        if bad == "admitted":
            host.store.append_event("bad", "run.admitted", {})
        with pytest.raises(ValueError, match="Only a standalone"):
            host.model_budget.ledger.reserve(
                owner, "bad", "attempt", context=BudgetContext(None, "bad"),
                input_estimate=1, output_estimate=1, estimate_method="test",
            )
        assert not host.budget_status()["reservations"]
    finally:
        await host.close()


async def test_restart_parks_probe_without_reading_source_and_keeps_unknown(
    isolated_workspace, monkeypatch,
):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    owner = control_run(host)
    host.model_budget.ledger.reserve(
        owner, "probe", "attempt", context=BudgetContext(None, "probe"),
        input_estimate=30, output_estimate=32, estimate_method="test",
        attempt_started={"attempt_id": "attempt", "request_id": "request", "attempt": 1,
                         "kind": "connection_probe"},
    )
    await host.close()

    def forbidden(*_args):
        raise AssertionError("a connection probe has no source checkpoint to recover")

    monkeypatch.setattr(RuntimeHost, "_crash_gap_evidence", forbidden)
    restored = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert probe_owner(restored) == owner
        assert restored.store.get_run("probe")["status"] == "interrupted"
        assert restored.budget_status()["reservations"][0]["status"] == "unsettled"
        assert ledger(restored, "probe")["unknown_attempts"] == 1
        assert not restored.store.facts_by_type(("workspace.checkpoint",))
    finally:
        await restored.close()


def test_probe_http_returns_run_evidence_and_unknown_can_be_reconciled(
    isolated_workspace, monkeypatch,
):
    provider = ProbeProvider(error=ProviderError("401 candidate-secret", retryable=True))
    host = make_host(isolated_workspace, monkeypatch, provider)
    with TestClient(create_app(host)) as client:
        response = client.post("/api/config/provider/test", json={
            "provider": "openai_compatible", "model": "candidate-model",
            "api_key": "candidate-secret",
        })
        assert response.status_code == 200
        body = response.json()
        assert not body["success"] and body["error_type"] == "authentication"
        assert body["run_id"]
        assert body["usage"]["unknown_attempts"] == 1
        assert "candidate-secret" not in response.text
        attempt = body["usage"]["calls"][0]["attempt_id"]
        result = client.post(f"/api/budgets/attempts/{attempt}/resolve", headers={
            "Idempotency-Key": "review-probe",
        }, json={"action": "keep_unknown", "reason": "reviewed"})
        assert result.status_code == 200
        assert result.json()["attempt"]["context"]["project_id"] is None


async def test_candidate_secret_is_not_exposed_by_response_metadata_or_traceback(
    isolated_workspace, monkeypatch,
):
    import traceback

    provider = ProbeProvider(response=ModelResponse(
        "candidate-secret", stop_reason="candidate-secret"
    ))
    host = make_host(isolated_workspace, monkeypatch, provider)
    try:
        response = await host.probe_provider(
            provider="openai", api_key="candidate-secret", base_url=None,
            model="candidate-secret-model", run_id="echo",
        )
        assert "candidate-secret" not in repr(response)
        assert "candidate-secret" not in json.dumps(host.store.run_events("echo"))
        provider.error = RuntimeError("candidate-secret")
        with pytest.raises(ProbeError) as caught:
            await call(host)
        rendered = "".join(traceback.format_exception(caught.value))
        assert "candidate-secret" not in rendered
    finally:
        await host.close()


async def test_host_close_waits_for_probe_and_preserves_unknown(isolated_workspace, monkeypatch):
    provider = ProbeProvider(wait=True)
    host = make_host(isolated_workspace, monkeypatch, provider)
    task = asyncio.create_task(call(host, run_id="probe"))
    await provider.entered.wait()
    await host.close()
    assert task.cancelled()
    assert provider.closed
    restored = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        assert restored.store.get_run("probe")["status"] == "interrupted"
        assert restored.budget_status()["reservations"][0]["status"] == "unsettled"
    finally:
        await restored.close()


@pytest.mark.parametrize("source", ["provider", "factory"])
async def test_custom_probe_error_is_redacted_everywhere(isolated_workspace, monkeypatch, source):
    import traceback

    error = ProbeError("candidate-secret-kind", "candidate-secret host-secret")
    provider = ProbeProvider(error=error)
    host = make_host(isolated_workspace, monkeypatch, provider, api_key="host-secret")
    if source == "factory":
        def fail_factory(_settings):
            raise error
        monkeypatch.setattr("eventide.host.build_provider", fail_factory)
    try:
        with pytest.raises(ProbeError) as caught:
            await call(host, run_id="custom-error")
        evidence = json.dumps(host.store.run_events("custom-error"))
        evidence += "".join(traceback.format_exception(caught.value)) + caught.value.category
        assert "candidate-secret" not in evidence
        assert "host-secret" not in evidence
    finally:
        await host.close()
