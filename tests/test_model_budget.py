"""Production dispatch cannot escape durable, shared request reservations."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eventide.budget_prices import PriceVersion
from eventide.budgets import BudgetExceeded, BudgetScope
from eventide.host import RuntimeHost
from eventide.model_budget import estimate_request
from eventide.models import ModelRequest, ModelResponse, RunRequest
from eventide.providers import ProviderError, ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_delivery_runtime import delivery_host
from tests.test_readonly_workers import WorkerProvider, worker_host
from tests.test_runtime import settings_for


def project_scope(host):
    return BudgetScope("project", host.default_workspace.project_id)


def admitted(host, run_id="budget-run"):
    session_id = host.create_session()
    host.store.admit_run(run_id, session_id, [("run.admitted", {
        "project_id": host.default_workspace.project_id,
        "workspace_id": host.default_workspace.workspace_id,
    }, "system", "user")])
    return run_id


def test_estimator_is_explicit_and_never_claims_tokenizer_precision():
    request = ModelRequest("中文", [{"role": "user", "content": "😺"}], [], "test", 17)
    inputs, outputs, method = estimate_request(request)
    assert inputs > len(request.system) + len(str(request.messages))
    assert outputs == 17
    assert method == "serialized_utf8_bytes_v1"


async def test_budget_blocks_before_provider_dispatch_and_parks(isolated_workspace):
    provider = ScriptedProvider([{"text": "must not send"}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        scope = project_scope(host)
        host.configure_budget(scope, token_limit=1, request_id="tiny")
        result = await host.run(RunRequest("work"))
        assert result.status == "interrupted"
        assert host.store.get_run(result.run_id)["reason"] == "token_budget"
        assert provider.requests == []
        assert host.budget_status()["reservations"] == []
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 0
        assert any(e["type"] == "budget.blocked" for e in host.store.run_events(result.run_id))
    finally:
        await host.close()


async def test_response_settles_once_and_prices_exact_provider_usage(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"text": "done", "usage": {"input_tokens": 100, "output_tokens": 20}},
    ]))
    try:
        for category in ("input_tokens", "output_tokens"):
            host.register_price(PriceVersion(
                category, "scripted", "scripted", category, "CNY", "1",
                datetime(2020, 1, 1, tzinfo=timezone.utc),
            ))
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        reservation = host.budget_status()["reservations"][0]
        assert reservation["actual_tokens"] == 120
        assert reservation["status"] == "settled"
        assert host.cost_status()["known_costs"] == {"CNY": "0.00012"}
        events = host.store.run_events(result.run_id)
        kinds = [e["type"] for e in events]
        assert kinds[kinds.index("budget.reserved") + 1] == "model.attempt_started"
        assert kinds[kinds.index("budget.settled") + 1] == "model.attempt_finished"
        finish = next(e for e in events if e["type"] == "model.attempt_finished")
        host.model_budget.finish(result.run_id, finish["payload"])
        assert len(host.cost_status()["history"]) == 1
        assert len(host.budget_status()["reservations"]) == 1
    finally:
        await host.close()


async def test_failed_attempt_holds_budget_and_stops_retry(isolated_workspace):
    provider = ScriptedProvider([{"error": "503", "retryable": True}, {"text": "unexpected"}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    request = ModelRequest("small", [], [], "scripted", 10)
    try:
        inputs, outputs, _ = estimate_request(request)
        host.configure_budget(project_scope(host), token_limit=inputs + outputs, request_id="one")
        run_id = admitted(host)
        with pytest.raises(BudgetExceeded, match="configured Token budget"):
            await host._complete_with_retry(request, run_id, None)
        assert len(provider.requests) == 1
        budget = host.budget_status()["budgets"][0]
        assert budget["reserved_tokens"] == inputs + outputs
        assert budget["unsettled_attempts"] == 1
        assert host.cost_status()["unknown_attempts"] == 1
    finally:
        await host.close()


async def test_successful_retry_does_not_release_unknown_first_attempt(isolated_workspace):
    provider = ScriptedProvider([
        {"error": "503", "retryable": True},
        {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 2}},
    ])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        calls = host.budget_status()["reservations"]
        assert len(calls) == 2
        assert calls[0]["status"] == "unsettled"
        assert calls[1]["actual_tokens"] == 5
        scope = host.budget_status(scopes=(project_scope(host),))["budgets"][0]
        assert scope["actual_tokens"] == 5
        assert scope["reserved_tokens"] == calls[0]["reserved_tokens"] > 0
    finally:
        await host.close()


@pytest.mark.parametrize("failure", ["missing", "partial", "cancel", "timeout", "stream"])
async def test_unknown_attempts_survive_restart(isolated_workspace, failure):
    entered = asyncio.Event()

    class Provider:
        async def complete(self, request):
            entered.set()
            if failure in {"cancel", "timeout"}:
                await asyncio.Event().wait()
            return ModelResponse("done", usage={"input_tokens": 20} if failure == "partial" else {})

    class StreamingProvider:
        async def stream(self, request):
            yield SimpleNamespace(kind="text_delta", text="partial", response=None)
            raise ProviderError("disconnected", retryable=True)

    settings = settings_for(isolated_workspace, model_timeout=.01 if failure == "timeout" else 30)
    host = RuntimeHost(settings, StreamingProvider() if failure == "stream" else Provider())
    task = asyncio.create_task(host.run(RunRequest("work", run_id="unknown")))
    try:
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 2)
            await host.cancel_run("unknown")
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        before = host.budget_status()
        assert before["reservations"]
        assert all(r["status"] == "unsettled" for r in before["reservations"])
        assert all(r["reserved_tokens"] > 0 for r in before["reservations"])
        assert len(before["reservations"]) == (3 if failure == "timeout" else 1)
    finally:
        await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert restored.budget_status() == before
    finally:
        await restored.close()


async def test_crash_between_dispatch_and_result_retains_reservation(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    request = ModelRequest("system", [], [], "scripted", 10)
    run_id = admitted(host)
    identity = {"attempt_id": "crashed", "request_id": "request", "attempt": 1,
                "provider": "scripted", "model": "scripted", "kind": "agent"}
    host.model_budget.begin(run_id, request, identity)
    await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]))
    try:
        reservation = restored.budget_status()["reservations"][0]
        assert reservation["status"] == "unsettled"
        assert restored.store.get_run(run_id)["status"] == "interrupted"
        ledger = build_usage_ledger(restored.store.usage_events(run_id=run_id))
        assert ledger["unknown_attempts"] == 1
        assert restored.cost_status()["unpriced_attempt_ids"] == ["crashed"]
    finally:
        await restored.close()


async def test_worker_calls_share_root_and_project_once(isolated_workspace):
    host = worker_host(isolated_workspace, WorkerProvider())
    try:
        result = await asyncio.wait_for(host.run(RunRequest("work")), 5)
        assert result.status == "completed", result.output
        status = host.budget_status(scopes=(BudgetScope("run", result.run_id), project_scope(host)))
        assert len(status["reservations"]) == 6
        assert all(bucket["actual_tokens"] == 30 for bucket in status["budgets"])
        usage = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert sum(call["kind"] == "worker" for call in usage["calls"]) == 4
    finally:
        await host.close()


async def test_delivery_and_continue_keep_project_and_delivery_limit(isolated_workspace):
    host = delivery_host(isolated_workspace, [{"text": "done"}])
    try:
        prepared = await host.manage_delivery("prepare", name="budget")
        scope = BudgetScope("delivery", prepared["delivery_id"])
        host.configure_budget(scope, token_limit=0, request_id="stop-delivery")
        result = await host.manage_delivery("run", delivery_id=scope.identity, goal="work")
        assert result["status"] == "interrupted"
        continued = await host.continue_session(result["worker_session_id"])
        assert continued.status == "interrupted"
        assert host.provider.requests == []
        session, context = host.model_budget.context(continued.run_id)
        assert context.delivery_id == scope.identity
        assert context.project_id == host.default_workspace.project_id
        assert context.root_run_id == continued.run_id
        assert session == result["worker_session_id"]
    finally:
        await host.close()


async def test_summary_usage_is_settled_even_when_not_adopted(isolated_workspace, monkeypatch):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"text": "truncated", "stop_reason": "max_tokens",
         "usage": {"input_tokens": 7, "output_tokens": 2}},
        {"text": "done", "usage": {"input_tokens": 3, "output_tokens": 1}},
    ]))

    async def build(session_id, *, complete_summary, **kwargs):
        response = await complete_summary(ModelRequest("summary", [], [], "scripted", 100))
        assert response.stop_reason == "max_tokens"
        return [{"role": "user", "content": "work"}], None, None

    monkeypatch.setattr(host.context_builder, "build", build)
    try:
        result = await host.run(RunRequest("work"))
        assert result.status == "completed"
        scope = host.budget_status(scopes=(project_scope(host),))["budgets"][0]
        assert scope["actual_tokens"] == 13
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert {call["kind"] for call in ledger["calls"]} == {"agent", "context_summary"}
    finally:
        await host.close()


async def test_concurrent_model_dispatch_competes_for_one_shared_allowance(isolated_workspace):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowProvider:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            entered.set()
            await release.wait()
            return ModelResponse("done", usage={"input_tokens": 2, "output_tokens": 1})

    provider = SlowProvider()
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    request = ModelRequest("small", [], [], "scripted", 10)
    inputs, outputs, _ = estimate_request(request)
    host.configure_budget(project_scope(host), token_limit=inputs + outputs, request_id="one")
    first, second = admitted(host, "first"), admitted(host, "second")
    task = asyncio.create_task(host._complete_with_retry(request, first, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(BudgetExceeded, match="configured Token budget"):
            await host._complete_with_retry(request, second, None)
        assert provider.calls == 1
        release.set()
        await task
        assert host.budget_status()["budgets"][0]["actual_tokens"] == 3
        assert host.budget_status()["budgets"][0]["reserved_tokens"] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


async def test_attempt_and_budget_facts_rollback_together(isolated_workspace, monkeypatch):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    run_id = admitted(host)
    identity = {"attempt_id": "atomic", "request_id": "request", "attempt": 1,
                "provider": "scripted", "model": "scripted", "kind": "agent"}
    original = host.store._append_fact_locked
    fail = "model.attempt_started"

    def append(session, event_type, payload, **kwargs):
        if event_type == fail:
            raise RuntimeError("simulated persistence failure")
        return original(session, event_type, payload, **kwargs)

    monkeypatch.setattr(host.store, "_append_fact_locked", append)
    try:
        request = ModelRequest("small", [], [], "scripted", 10)
        with pytest.raises(RuntimeError, match="simulated"):
            host.model_budget.begin(run_id, request, identity)
        assert host.budget_status()["reservations"] == []
        fail = "model.attempt_finished"
        host.model_budget.begin(run_id, request, identity)
        with pytest.raises(RuntimeError, match="simulated"):
            host.model_budget.finish(run_id, {
                **identity, "status": "completed", "usage_source": "reported",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            })
        assert host.budget_status()["reservations"][0]["status"] == "unsettled"
        assert not any(e["type"] == "budget.settled" for e in host.store.run_events(run_id))
    finally:
        await host.close()


async def test_budget_configuration_rejects_forged_owners(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        for kind in ("project", "run", "delivery"):
            with pytest.raises(ValueError):
                host.configure_budget(BudgetScope(kind, "missing"), token_limit=1,
                                      request_id=kind)
        assert host.budget_status()["budgets"] == []
        run_id = admitted(host)
        host.store.append_event(run_id, "run.started", {"project_id": "forged"})
        with pytest.raises(ValueError, match="project identity mismatch"):
            host.model_budget.context(run_id)
    finally:
        await host.close()


async def test_summary_budget_refusal_is_not_swallowed_as_optional_failure(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace, context_limit=5000), ScriptedProvider([]))
    try:
        session = host.create_session()
        host.store.create_run("prior", session)
        host.store.append_event("prior", "message.user", {
            "message": {"role": "user", "content": "previous large history " * 1000},
        })
        host.store.append_event("prior", "run.completed", {"output": "done"})
        host.configure_budget(project_scope(host), token_limit=0, request_id="stop-summary")
        result = await host.run(RunRequest("continue investigating", session_id=session))
        assert result.status == "interrupted", result.output
        events = host.store.run_events(result.run_id)
        assert any(e["type"] == "context.summary_requested" for e in events)
        assert not any(e["type"] == "model.request" for e in events)
        assert host.provider.requests == []
    finally:
        await host.close()


async def test_actual_overshoot_is_kept_and_blocks_the_next_run(isolated_workspace):
    provider = ScriptedProvider([
        {"text": "done", "usage": {"input_tokens": 60_000, "output_tokens": 2}},
        {"text": "must not send"},
    ])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        host.configure_budget(project_scope(host), token_limit=50_000, request_id="cap")
        first = await host.run(RunRequest("work"))
        assert first.status == "completed"
        budget = host.budget_status()["budgets"][0]
        assert budget["actual_tokens"] == 60_002
        assert budget["blocked"] is True
        assert any(e["type"] == "budget.warning" for e in host.store.run_events(first.run_id))
        second = await host.run(RunRequest("more work"))
        assert second.status == "interrupted"
        assert len(provider.requests) == 1
    finally:
        await host.close()


@pytest.mark.parametrize("kind", ["run", "day"])
async def test_run_and_day_limits_apply_to_real_dispatch(isolated_workspace, kind):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "unexpected"}]))
    try:
        run_id = await host.admit_run(RunRequest("work"))
        scope = BudgetScope(kind, run_id if kind == "run" else
                            datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        host.configure_budget(scope, token_limit=0, request_id="deny")
        result = await host.start_admitted_run(run_id)
        assert result.status == "interrupted"
        assert host.provider.requests == []
    finally:
        await host.close()
