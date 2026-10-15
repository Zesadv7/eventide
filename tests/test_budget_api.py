"""Budget mutations and whole-history fee views retain exact accounting evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.budget_prices import PriceVersion
from eventide.budget_views import control_owner
from eventide.budgets import BudgetContext
from eventide.host import RuntimeHost
from eventide.http_control import MutationReceipt, fail_mutation
from eventide.providers import ScriptedProvider
from eventide.workspace import digest
from tests.test_delivery_runtime import delivery_host
from tests.test_projects import linked_repo
from tests.test_runtime import settings_for

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
OUTCOME_HEADER = "X-Eventide-Mutation-Outcome"


def headers(key="change", **other):
    return {"Idempotency-Key": key, **other}


def price_body(**changes):
    return {
        "version_id": "input-v1",
        "provider": "scripted",
        "model": "scripted",
        "category": "input_tokens",
        "currency": "CNY",
        "price_per_million": "1.20",
        "effective_at": "2020-01-01T08:00:00+08:00",
        **changes,
    }


def new_run(host, run="run", *, session=None, parent=None, delivery=None, workspace=None):
    workspace = workspace or host.default_workspace
    session = session or host.create_session(workspace_id=workspace.workspace_id)
    payload = {"project_id": workspace.project_id, "workspace_id": workspace.workspace_id}
    if parent:
        payload["parent_run_id"] = parent
    if delivery:
        payload["execution_config"] = {"writer_delivery_id": delivery}
    host.store.admit_run(run, session, [("run.admitted", payload, "system", "user")])
    return session


def record(
    host,
    attempt="attempt",
    *,
    run="run",
    session=None,
    parent=None,
    delivery=None,
    workspace=None,
    at=NOW,
    usage=None,
    source="reported",
    finish=True,
    reserve=True,
):
    workspace = workspace or host.default_workspace
    session = new_run(
        host, run, session=session, parent=parent, delivery=delivery, workspace=workspace
    )
    identity = {
        "attempt_id": attempt,
        "request_id": f"req-{attempt}",
        "attempt": 1,
        "provider": "scripted",
        "model": "scripted",
        "kind": "worker" if parent else "agent",
    }
    if reserve:
        host.model_budget.ledger.reserve(
            session,
            run,
            attempt,
            context=BudgetContext(workspace.project_id, parent or run, delivery),
            input_estimate=20,
            output_estimate=30,
            estimate_method="test_estimate",
            at=at,
            attempt_started=identity,
        )
    else:
        host.store.append_event(run, "model.attempt_started", identity)
    if finish:
        finished = {**identity, "usage": usage or {}, "usage_source": source, "status": "completed"}
        if reserve:
            host.model_budget.ledger.settle(
                session,
                attempt,
                usage,
                source=source,
                run_id=run,
                attempt_finished=finished,
            )
            host.model_budget.costs.estimate_cost(
                session,
                attempt,
                provider="scripted",
                model="scripted",
                estimate_id=f"fee-{attempt}",
            )
        else:
            host.store.append_event(run, "model.attempt_finished", finished)
    return session


@pytest.fixture
def runtime(isolated_workspace):
    return RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))


def test_budget_config_zero_disable_hash_and_durable_replay(runtime):
    scope = f"/api/budgets/project/{runtime.default_workspace.project_id}"
    with TestClient(create_app(runtime)) as client:
        initial = client.get(scope).json()
        assert initial["timezone"] == "UTC"
        original_hash = initial["budgets"][0]["configuration_hash"]
        body = {"token_limit": 0, "expected_configuration_hash": original_hash}
        first = client.put(scope, json=body, headers=headers()).json()
        assert first["budgets"][0]["blocked"] is True
        conflict = client.put(scope, json={"token_limit": 100}, headers=headers())
        assert conflict.status_code == 409
        assert OUTCOME_HEADER not in conflict.headers
        stale = client.put(scope, json=body, headers=headers("stale"))
        assert stale.status_code == 409
        assert stale.headers[OUTCOME_HEADER] == "rejected"
        assert "changed" in stale.json()["message"]
        disabled = client.put(scope, json={"token_limit": None}, headers=headers("disable"))
        assert disabled.status_code == 200
        assert disabled.json()["budgets"][0]["blocked"] is False
        assert disabled.json()["budgets"][0]["remaining_tokens"] is None
        assert client.put(scope, json=body, headers=headers()).json() == first
        assert len(runtime.store.facts_by_type(("budget.configured",))) == 2


@pytest.mark.parametrize("value", [True, "1", 1.5, -1, 1_000_000_000_001])
def test_budget_counts_are_strict_and_do_not_echo_input(runtime, value):
    with TestClient(create_app(runtime)) as client:
        result = client.put(
            "/api/budgets/day/2026-10-16", json={"token_limit": value}, headers=headers()
        )
        assert result.status_code == 422
        assert '"input"' not in result.text
        assert runtime.store.facts_by_type(("budget.configured",)) == []


def test_budget_scope_validation_and_required_idempotency(runtime):
    new_run(runtime)
    with TestClient(create_app(runtime)) as client:
        for scope in ("run/run", "day/2026-10-16"):
            url = f"/api/budgets/{scope}"
            assert client.put(url, json={"token_limit": 10}).status_code == 422
            assert (
                client.put(url, json={"token_limit": 10}, headers=headers(scope)).status_code == 200
            )
        for scope in ("run/missing", "project/missing", "delivery/missing"):
            assert client.get(f"/api/budgets/{scope}").status_code == 404
        for scope in ("day/2026-1-01", "day/2026-02-30", "session/run"):
            assert client.get(f"/api/budgets/{scope}").status_code == 422
        assert len(client.get("/api/budgets").json()["budgets"]) == 2


async def test_real_delivery_budget_is_available_via_http(isolated_workspace):
    host = delivery_host(isolated_workspace, [])
    prepared = await host.manage_delivery("prepare", name="budget-http")
    with TestClient(create_app(host)) as client:
        result = client.put(
            f"/api/budgets/delivery/{prepared['delivery_id']}",
            json={"token_limit": 0},
            headers=headers(),
        )
        assert result.status_code == 200, result.text
        assert result.json()["budgets"][0]["blocked"] is True


@pytest.mark.parametrize("boundary", ["origin", "remote"])
def test_all_control_mutations_enforce_local_same_origin(runtime, boundary):
    record(runtime, source="unknown")
    kwargs = {"client": ("203.0.113.10", 50000)} if boundary == "remote" else {}
    guarded = headers(Origin="https://attacker.invalid") if boundary == "origin" else headers()
    with TestClient(create_app(runtime), **kwargs) as client:
        assert (
            client.put(
                "/api/budgets/day/2026-10-16", json={"token_limit": 1}, headers=guarded
            ).status_code
            == 403
        )
        assert client.post("/api/prices", json=price_body(), headers=guarded).status_code == 403
        assert (
            client.post(
                "/api/budgets/attempts/attempt/resolve",
                json={"action": "keep_unknown", "reason": "reviewed"},
                headers=guarded,
            ).status_code
            == 403
        )
        assert (
            runtime.store.facts_by_type(
                ("budget.configured", "price.registered", "budget.unknown_acknowledged")
            )
            == []
        )


def test_config_replay_survives_restart_with_different_default_workspace(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        first = client.put(
            "/api/budgets/day/2026-10-16", json={"token_limit": 20}, headers=headers()
        ).json()
    other = isolated_workspace / "other"
    other.mkdir()
    restored = RuntimeHost(replace(settings, workdir=other), ScriptedProvider([]))
    with TestClient(create_app(restored)) as client:
        assert (
            client.put(
                "/api/budgets/day/2026-10-16", json={"token_limit": 20}, headers=headers()
            ).json()
            == first
        )
        assert len(restored.store.facts_by_type(("budget.configured",))) == 1


def test_ambiguous_config_mutation_never_reexecutes(runtime):
    owner = control_owner(runtime)
    payload = {
        "kind": "day",
        "identity": "2026-10-16",
        "token_limit": 1,
        "expected_configuration_hash": None,
    }
    runtime.store.append_fact(
        owner,
        "http.mutation_started",
        {
            "identity": "mutation_" + digest([owner, "budget.configure", "change"]),
            "operation": "budget.configure",
            "fingerprint": digest(payload),
        },
    )
    with TestClient(create_app(runtime)) as client:
        result = client.put(
            "/api/budgets/day/2026-10-16", json={"token_limit": 1}, headers=headers()
        )
        assert result.status_code == 409
        assert "unknown" in result.json()["message"]
        assert OUTCOME_HEADER not in result.headers
        assert runtime.store.facts_by_type(("budget.configured",)) == []


def test_prices_exact_decimals_versions_filters_and_old_estimates(runtime):
    with TestClient(create_app(runtime)) as client:
        first = client.post("/api/prices", json=price_body(), headers=headers())
        assert first.status_code == 200
        assert first.json()["effective_at"] == "2020-01-01T00:00:00+00:00"
        assert first.json()["price_per_million"] == "1.20"
        assert (
            client.post("/api/prices", json=price_body(), headers=headers()).json() == first.json()
        )
        assert (
            client.post(
                "/api/prices", json=price_body(price_per_million="2"), headers=headers()
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/prices", json=price_body(price_per_million="2"), headers=headers("other")
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/prices", json=price_body(version_id="duplicate"), headers=headers("duplicate")
            ).status_code
            == 409
        )
        record(runtime, usage={"input_tokens": 100, "output_tokens": 10})
        old = client.get("/api/costs").json()
        assert old["known_costs"] == {"CNY": "0.00012"}
        assert old["unknown_attempts"] == 1  # Output has no effective price.
        result = client.post(
            "/api/prices",
            json=price_body(
                version_id="input-v2",
                price_per_million="5",
                effective_at="2021-01-01T00:00:00Z",
            ),
            headers=headers("new"),
        )
        assert result.status_code == 200
        assert client.get("/api/costs").json() == old
        assert (
            len(
                client.get("/api/prices?provider=scripted&model=scripted&currency=CNY").json()[
                    "prices"
                ]
            )
            == 2
        )
        assert client.get("/api/prices?currency=USD").json() == {"prices": []}


@pytest.mark.parametrize(
    "changes",
    [
        {"price_per_million": 1.2},
        {"price_per_million": "NaN"},
        {"price_per_million": "-1"},
        {"currency": "cny"},
        {"effective_at": "2026-10-16T12:00:00"},
        {"category": "total"},
    ],
)
def test_invalid_prices_never_create_versions(runtime, changes):
    with TestClient(create_app(runtime)) as client:
        assert (
            client.post("/api/prices", json=price_body(**changes), headers=headers()).status_code
            == 422
        )
        assert client.get("/api/prices").json() == {"prices": []}


def prices(host):
    for currency in ("CNY", "USD"):
        for category in ("input_tokens", "output_tokens"):
            host.register_price(
                PriceVersion(
                    f"{currency}-{category}",
                    "scripted",
                    "scripted",
                    category,
                    currency,
                    "1",
                    datetime(2020, 1, 1, tzinfo=timezone.utc),
                )
            )


def test_cost_scopes_include_children_continue_projects_and_utc_days(runtime, tmp_path):
    prices(runtime)
    session = record(
        runtime,
        "root-call",
        run="root",
        delivery="delivery",
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    runtime.store.append_fact(session, "delivery.prepared", {"delivery_id": "delivery"})
    record(
        runtime,
        "child-call",
        run="child",
        parent="root",
        delivery="delivery",
        usage={"input_tokens": 3, "output_tokens": 1},
    )
    record(
        runtime,
        "continue-call",
        run="continue",
        session=session,
        delivery="delivery",
        at=NOW + timedelta(days=1),
        usage={"input_tokens": 5, "output_tokens": 1},
    )
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    other = runtime.resolve_or_register_workspace(other_path)
    record(
        runtime,
        "other-call",
        run="other",
        workspace=other,
        usage={"input_tokens": 100, "output_tokens": 10},
    )
    runtime.store.append_event(
        "root",
        "run.completed",
        {
            "usage": {"input_tokens": 999999},
            "output": "terminal totals are not billing facts",
        },
    )
    with TestClient(create_app(runtime)) as client:

        def cost(kind, identity, **params):
            response = client.get(
                "/api/costs", params={"scope_kind": kind, "scope_id": identity, **params}
            )
            assert response.status_code == 200, response.text
            return response.json()

        root = cost("run", "root")
        assert root["attempt_count"] == 2
        assert root["usage"]["reported_totals"] == {"input_tokens": 13, "output_tokens": 3}
        assert root["known_costs"] == {"CNY": "0.000016", "USD": "0.000016"}
        assert root["complete"] is True
        assert root["usage"]["calls"][0]["workspace_id"] == runtime.default_workspace.workspace_id
        assert cost("run", "root", include_children="false")["attempt_count"] == 1
        assert cost("session", session)["attempt_count"] == 3
        assert cost("session", session, include_children="false")["attempt_count"] == 2
        assert cost("project", runtime.default_workspace.project_id)["attempt_count"] == 3
        assert cost("project", other.project_id)["attempt_count"] == 1
        assert cost("delivery", "delivery")["attempt_count"] == 3
        assert cost("day", "2026-10-17")["attempt_count"] == 1
        assert (
            cost(
                "project",
                runtime.default_workspace.project_id,
                date_from="2026-10-16",
                date_to="2026-10-16",
            )["attempt_count"]
            == 2
        )
        assert (
            cost(
                "project",
                runtime.default_workspace.project_id,
                date_from="2026-10-16",
                date_to="2026-10-17",
            )["attempt_count"]
            == 3
        )


@pytest.mark.parametrize(
    "query,status",
    [
        ("scope_kind=run", 422),
        ("scope_id=orphan", 422),
        ("scope_kind=unknown&scope_id=x", 422),
        ("scope_kind=run&scope_id=absent", 404),
        ("scope_kind=session&scope_id=absent", 404),
        ("date_from=2026-1-01", 422),
        ("date_from=2026-10-17&date_to=2026-10-16", 422),
    ],
)
def test_cost_filter_validation(runtime, query, status):
    with TestClient(create_app(runtime)) as client:
        assert client.get(f"/api/costs?{query}").status_code == status


def test_full_session_history_does_not_depend_on_navigation_page(runtime):
    session = runtime.create_session()
    for index in range(105):
        run = f"history-{index}"
        new_run(runtime, run, session=session)
        runtime.store.append_event(
            run,
            "model.response",
            {
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "model": "historical",
            },
        )
        runtime.store.append_event(run, "run.completed", {"output": "done"})
    with TestClient(create_app(runtime)) as client:
        result = client.get(
            "/api/costs", params={"scope_kind": "session", "scope_id": session}
        ).json()
        assert result["usage"]["attempt_count"] == 105
        assert result["usage"]["reported_totals"] == {"input_tokens": 210, "output_tokens": 105}
        assert result["unknown_attempts"] == 105
        assert len(result["historical_unreserved_attempt_ids"]) == 105
        assert result["known_costs"] == {}
        assert result["complete"] is False


def test_historical_attempt_dedup_precedes_midnight_filter(runtime, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("eventide.store.time.time", lambda: NOW.timestamp())
        session = record(runtime, reserve=False, usage={"input_tokens": 10, "output_tokens": 2})
        runtime.store.append_fact(session, "delivery.prepared", {"delivery_id": "old"})
        runtime.store.append_event(
            "run", "run.started", {"execution_config": {"writer_delivery_id": "old"}}
        )
        record(
            runtime,
            "old-child",
            run="old-child",
            parent="run",
            reserve=False,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    with monkeypatch.context() as patch:
        patch.setattr("eventide.store.time.time", lambda: (NOW + timedelta(days=1)).timestamp())
        runtime.store.append_event("run", "model.response", {"usage": {"input_tokens": 10}})
    with TestClient(create_app(runtime)) as client:
        assert (
            client.get("/api/costs?scope_kind=day&scope_id=2026-10-17").json()["attempt_count"] == 0
        )
        day = client.get("/api/costs?scope_kind=day&scope_id=2026-10-16").json()
        assert day["attempt_count"] == 2
        assert day["unknown_attempts"] == 2
        assert (
            client.get("/api/costs?scope_kind=delivery&scope_id=old").json()["attempt_count"] == 2
        )


def test_estimated_usage_costs_are_separate_and_unknown_usage_is_not_free(runtime):
    prices(runtime)
    record(runtime, usage={"input_tokens": 6, "output_tokens": 2}, source="estimated")
    record(runtime, "unknown", run="unknown", source="unknown")
    with TestClient(create_app(runtime)) as client:
        result = client.get("/api/costs").json()
        assert result["known_costs"] == {}
        assert Decimal(result["estimated_usage_costs"]["CNY"]) == Decimal("0.000008")
        assert result["estimated_attempts"] == 1
        assert result["unknown_attempts"] == 1
        assert result["usage"]["estimated_totals"] == {"input_tokens": 6, "output_tokens": 2}
        assert result["complete"] is False


def test_unknown_acknowledgment_retains_allowance_and_manual_known_does_not_invent_usage(runtime):
    record(runtime, source="unknown")
    url = "/api/budgets/attempts/attempt/resolve"
    with TestClient(create_app(runtime)) as client:
        body = {"action": "keep_unknown", "reason": "Provider cannot recover the request"}
        acknowledged = client.post(url, json=body, headers=headers())
        assert acknowledged.status_code == 200, acknowledged.text
        attempt = acknowledged.json()["attempt"]
        assert attempt["status"] == "unsettled"
        assert attempt["actual_tokens"] is None
        assert all(bucket["reserved_tokens"] == 50 for bucket in attempt["buckets"])
        assert attempt["audit"][0]["author"] == "user"
        assert client.post(url, json=body, headers=headers()).json() == acknowledged.json()
        assert len(runtime.store.facts_by_type(("budget.unknown_acknowledged",))) == 1
        known = client.post(
            url,
            json={
                "action": "record_known",
                "actual_tokens": 13,
                "reason": "Provider support confirmed total",
            },
            headers=headers("known"),
        )
        assert known.status_code == 200, known.text
        assert known.json()["attempt"]["status"] == "manually_resolved"
        assert known.json()["attempt"]["actual_tokens"] == 13
        assert len(known.json()["attempt"]["audit"]) == 2
        assert all(bucket["reserved_tokens"] == 0 for bucket in known.json()["attempt"]["buckets"])
        assert client.post(url, json=body, headers=headers("again")).status_code == 409
        costs = client.get("/api/costs").json()
        assert costs["known_costs"] == {}
        assert costs["unknown_attempts"] == 1
        assert costs["usage"]["calls"][0]["usage"] == {}
        assert costs["usage"]["calls"][0]["usage_source"] == "unknown"


def test_active_attempt_reconciliation_requires_terminal_evidence(runtime):
    record(runtime, finish=False)
    with TestClient(create_app(runtime)) as client:
        url = "/api/budgets/attempts/attempt/resolve"
        body = {"action": "record_known", "actual_tokens": 0, "reason": "Checked provider"}
        denied = client.post(url, json=body, headers=headers())
        assert denied.status_code == 409
        assert denied.headers[OUTCOME_HEADER] == "rejected"
        failure = runtime.store.facts_by_type(("http.mutation_failed",))[0]
        assert failure["payload"]["outcome"] == "rejected"
        runtime.store.append_event("run", "run.interrupted", {"reason": "cancelled"})
        replay = client.post(url, json=body, headers=headers())
        assert replay.status_code == 409
        assert replay.headers[OUTCOME_HEADER] == "rejected"
        assert runtime.store.facts_by_type(("budget.resolved",)) == []
        assert client.post(url, json=body, headers=headers("after-stop")).status_code == 200


def test_rejected_failure_header_survives_restart(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    record(host, finish=False)
    url = "/api/budgets/attempts/attempt/resolve"
    body = {"action": "keep_unknown", "reason": "Checked provider"}
    with TestClient(create_app(host)) as client:
        assert client.post(url, json=body, headers=headers()).headers[OUTCOME_HEADER] == "rejected"
    restored = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(restored)) as client:
        replay = client.post(url, json=body, headers=headers())
        assert replay.status_code == 409
        assert replay.headers[OUTCOME_HEADER] == "rejected"
        assert client.post(url, json=body, headers=headers("after-restart")).status_code == 200


def test_failure_is_not_classified_as_rejected_without_opt_in(runtime):
    owner = control_owner(runtime)
    identity = "mutation_" + digest([owner, "budget.configure", "change"])
    payload = {
        "kind": "day",
        "identity": "2026-10-16",
        "token_limit": 1,
        "expected_configuration_hash": None,
    }
    runtime.store.append_fact(
        owner,
        "http.mutation_started",
        {
            "identity": identity,
            "operation": "budget.configure",
            "fingerprint": digest(payload),
        },
    )
    fail_mutation(runtime, MutationReceipt(identity, owner), "Previous operation failed", 409)
    assert "outcome" not in runtime.store.facts_by_type(("http.mutation_failed",))[0]["payload"]
    with TestClient(create_app(runtime)) as client:
        response = client.put(
            "/api/budgets/day/2026-10-16", json={"token_limit": 1}, headers=headers()
        )
        assert response.status_code == 409
        assert OUTCOME_HEADER not in response.headers


def test_unexpected_budget_write_failure_retains_unknown_outcome(runtime, monkeypatch):
    def unknown_failure(*_args, **_kwargs):
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(runtime, "configure_budget", unknown_failure)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        first = client.put(
            "/api/budgets/day/2026-10-16", json={"token_limit": 1}, headers=headers()
        )
        assert first.status_code == 500
        assert OUTCOME_HEADER not in first.headers
        repeated = client.put(
            "/api/budgets/day/2026-10-16", json={"token_limit": 1}, headers=headers()
        )
        assert repeated.status_code == 409
        assert OUTCOME_HEADER not in repeated.headers


def test_partial_reported_lower_bound_cannot_be_erased(runtime):
    record(runtime, usage={"input_tokens": 100})
    with TestClient(create_app(runtime)) as client:
        url = "/api/budgets/attempts/attempt/resolve"
        body = {"action": "record_known", "actual_tokens": 99, "reason": "Unverified total"}
        assert client.post(url, json=body, headers=headers()).status_code == 422
        assert (
            client.post(
                url, json={**body, "actual_tokens": 100}, headers=headers("corrected")
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "body",
    [
        {"action": "record_known", "reason": "missing amount"},
        {"action": "record_known", "actual_tokens": True, "reason": "bad amount"},
        {"action": "keep_unknown", "actual_tokens": None, "reason": "not allowed"},
        {"action": "keep_unknown", "reason": "   "},
        {"action": "release", "reason": "not allowed"},
    ],
)
def test_unknown_resolution_requires_explicit_strict_decision(runtime, body):
    with TestClient(create_app(runtime)) as client:
        assert (
            client.post(
                "/api/budgets/attempts/missing/resolve", json=body, headers=headers()
            ).status_code
            == 422
        )
        assert runtime.store.facts_by_type(("budget.resolved",)) == []


def test_project_costs_span_real_git_execution_copies(isolated_workspace):
    source, copy = linked_repo(isolated_workspace)
    host = RuntimeHost(settings_for(source), ScriptedProvider([]))
    workspace = host.resolve_or_register_workspace(copy)
    record(host, "source-call", run="source", usage={"input_tokens": 2, "output_tokens": 1})
    record(
        host,
        "copy-call",
        run="copy",
        workspace=workspace,
        usage={"input_tokens": 3, "output_tokens": 1},
    )
    with TestClient(create_app(host)) as client:
        view = client.get(
            "/api/costs",
            params={"scope_kind": "project", "scope_id": host.default_workspace.project_id},
        ).json()
        assert view["attempt_count"] == 2
        assert view["usage"]["reported_totals"] == {"input_tokens": 5, "output_tokens": 2}
        assert len({call["workspace_id"] for call in view["usage"]["calls"]}) == 2


def test_resolution_restart_replay_and_other_attempt_conflict(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    record(host, source="unknown")
    record(host, "other", run="other", source="unknown")
    url = "/api/budgets/attempts/attempt/resolve"
    body = {"action": "record_known", "actual_tokens": 7, "reason": "Checked provider receipt"}
    with TestClient(create_app(host)) as client:
        first = client.post(url, json=body, headers=headers())
        assert first.status_code == 200
    restored = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(restored)) as client:
        assert client.post(url, json=body, headers=headers()).json() == first.json()
        assert len(restored.store.facts_by_type(("budget.resolved",))) == 1
        assert (
            client.post(
                "/api/budgets/attempts/other/resolve", json=body, headers=headers()
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/budgets/attempts/missing/resolve", json=body, headers=headers("missing")
            ).status_code
            == 404
        )


def test_budget_configuration_hash_does_not_change_with_spending(runtime):
    scope = f"/api/budgets/project/{runtime.default_workspace.project_id}"
    with TestClient(create_app(runtime)) as client:
        before = client.put(scope, json={"token_limit": 100}, headers=headers()).json()
        record(runtime, usage={"input_tokens": 2, "output_tokens": 1})
        after = client.get(scope).json()
        assert after["budgets"][0]["actual_tokens"] == 3
        assert (
            before["budgets"][0]["configuration_hash"] == after["budgets"][0]["configuration_hash"]
        )
