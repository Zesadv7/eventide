"""Input-only billing never fabricates output tokens or weakens text-call budgets."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from eventide.budget_prices import PriceVersion
from eventide.budgets import BudgetContext, BudgetExceeded, BudgetLedger, BudgetScope
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from eventide.usage import build_usage_ledger, usage_snapshot, usage_total_fields
from tests.test_model_budget import admitted, project_scope
from tests.test_runtime import settings_for


def identity(attempt="embedding-1"):
    return {
        "attempt_id": attempt, "request_id": f"request-{attempt}", "attempt": 1,
        "kind": "embedding", "provider": "openai_compatible", "model": "embedding-model",
        "usage_basis": "input_only",
    }


@pytest.mark.parametrize("value", [0, 17])
def test_input_only_completeness_preserves_reported_input_without_output(value):
    usage = {"input_tokens": value}
    assert usage_snapshot(usage, basis="input_only") == {
        "usage": usage, "source": "reported", "complete": True,
    }
    assert usage_snapshot(usage)["complete"] is False


@pytest.mark.parametrize("usage,source", [(None, "reported"), ({}, "reported"),
                                         ({"input_tokens": 4}, "estimated")])
def test_missing_or_estimated_embedding_usage_is_not_complete(usage, source):
    assert usage_snapshot(usage, source, basis="input_only")["complete"] is False


@pytest.mark.parametrize("field", ["output_tokens", "reasoning_tokens"])
def test_embedding_output_counters_rejected_even_when_zero(field):
    with pytest.raises(ValueError, match="output counters"):
        usage_snapshot({"input_tokens": 2, field: 0}, basis="input_only")


def test_unknown_usage_basis_rejected():
    with pytest.raises(ValueError, match="basis"):
        usage_total_fields("anything")


async def test_embedding_settlement_prices_input_once_and_survives_reopen(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    path = host.store.path
    try:
        run = admitted(host)
        host.register_price(PriceVersion(
            "embedding-input", "openai_compatible", "embedding-model", "input_tokens", "CNY",
            "2", datetime(2020, 1, 1, tzinfo=timezone.utc),
        ))
        receipt = host.model_budget.begin_embedding(run, ["secret-input-中文"], identity())
        assert receipt.actual_tokens is None
        assert receipt.reserved_tokens > len("secret-input-中文")
        finished = {**identity(), "status": "completed", "usage_source": "reported",
                    "usage": {"input_tokens": 7}}
        settled = host.model_budget.finish(run, finished)
        assert settled.status == "settled"
        assert settled.actual_tokens == 7
        assert host.model_budget.finish(run, finished) == settled
        assert host.cost_status()["known_costs"] == {"CNY": "0.000014"}
        assert host.cost_status()["complete"] is True
        events = host.store.usage_events(run_id=run)
        assert "secret-input-中文" not in str(events)
        assert all("output_tokens" not in e["payload"].get("usage", {}) for e in events)
        ledger = build_usage_ledger([*events, *events])
        assert ledger["attempt_count"] == 1
        assert ledger["reported_totals"] == {"input_tokens": 7}
        assert ledger["complete"] is True
        assert ledger["calls"][0]["usage_basis"] == "input_only"
        assert ledger["calls"][0]["input_composition"] is None
        assert len(host.cost_status()["history"]) == 1
        history = deepcopy(host.cost_status()["history"])
    finally:
        await host.close()
    store = RuntimeStore(path)
    try:
        assert BudgetLedger(store).snapshot()["reservations"][0]["actual_tokens"] == 7
        assert build_usage_ledger(store.usage_events(run_id=run)) == ledger
        assert len(store.facts_by_type(("cost.estimated",))) == len(history)
    finally:
        store.close()


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
async def test_missing_embedding_usage_keeps_reservation(isolated_workspace, status):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        run = admitted(host)
        before = host.model_budget.begin_embedding(run, ["query"], identity())
        after = host.model_budget.finish(run, {
            **identity(), "status": status, "usage_source": "unknown",
        })
        assert after.status == "unsettled"
        assert after.reserved_tokens == before.reserved_tokens
        assert after.actual_tokens is None
        ledger = build_usage_ledger(host.store.usage_events(run_id=run))
        assert ledger["unknown_attempts"] == 1
        assert ledger["reported_totals"] == {}
    finally:
        await host.close()


async def test_estimated_embedding_tokens_do_not_release_budget(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        run = admitted(host)
        before = host.model_budget.begin_embedding(run, ["query"], identity())
        session = host.store.get_run_identity(run)["session_id"]
        result = host.model_budget.ledger.settle(
            session, "embedding-1", {"input_tokens": 3}, source="estimated", run_id=run,
            attempt_finished={**identity(), "status": "completed", "usage_source": "estimated",
                              "usage": {"input_tokens": 3}},
        )
        assert result.actual_tokens is None
        assert result.reserved_tokens == before.reserved_tokens
        ledger = build_usage_ledger(host.store.usage_events(run_id=run))
        assert ledger["reported_totals"] == {}
        assert ledger["estimated_totals"] == {"input_tokens": 3}
        assert ledger["complete"] is False
    finally:
        await host.close()


async def test_embedding_request_obeys_shared_project_and_child_budget(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        root = admitted(host)
        host.model_budget.begin_embedding(root, ["query"], identity())
        host.model_budget.finish(root, {
            **identity(), "status": "completed", "usage": {"input_tokens": 7},
        })
        child_session = host.create_session()
        host.store.admit_run("child", child_session, [("run.admitted", {
            "project_id": host.default_workspace.project_id, "parent_run_id": root,
        }, "system", "runtime")])
        host.configure_budget(project_scope(host), token_limit=8, request_id="limit")
        with pytest.raises(BudgetExceeded):
            host.model_budget.begin_embedding("child", ["query"], identity("child-attempt"))
        assert host.store.usage_events(run_id="child") == []
        assert len(host.budget_status()["reservations"]) == 1
        host.configure_budget(project_scope(host), token_limit=None, request_id="disable")
        receipt = host.model_budget.begin_embedding("child", ["query"], identity("child-attempt"))
        assert {bucket["identity"] for bucket in receipt.buckets if bucket["kind"] == "run"} == {
            root, "child",
        }
        # Unknown child usage continues to consume the same root bucket.
        host.configure_budget(BudgetScope("run", root), token_limit=8, request_id="root-limit")
        with pytest.raises(BudgetExceeded):
            host.model_budget.begin_embedding(root, ["query"], identity("next"))
    finally:
        await host.close()


async def test_usage_basis_cannot_change_at_retry_or_settlement(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        run = admitted(host)
        host.model_budget.begin_embedding(run, ["query"], identity())
        with pytest.raises(ValueError, match="usage basis"):
            host.model_budget.finish(run, {
                **identity(), "usage_basis": "input_output", "usage": {"input_tokens": 1},
            })
        with pytest.raises(ValueError, match="usage basis"):
            host.model_budget.ledger.reserve(
                host.store.get_run_identity(run)["session_id"], run, "embedding-1",
                context=BudgetContext(host.default_workspace.project_id, run),
                input_estimate=20, output_estimate=0, estimate_method="fake",
            )
        assert host.budget_status()["reservations"][0]["status"] == "unsettled"
        events = host.store.run_events(run)
        start = next(e for e in events if e["type"] == "model.attempt_started")
        finish = {**start, "type": "model.attempt_finished", "payload": {
            **start["payload"], "usage_basis": "input_output", "usage": {"input_tokens": 1},
        }}
        with pytest.raises(ValueError, match="usage basis"):
            build_usage_ledger([start, finish])
    finally:
        await host.close()


@pytest.mark.parametrize("changes,outputs", [({"kind": "agent"}, 0), ({}, 1),
                                          ({"usage_basis": "input_output"}, 0)])
async def test_only_explicit_embedding_attempt_can_reserve_input_only(
    isolated_workspace, changes, outputs,
):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        run = admitted(host)
        with pytest.raises(ValueError):
            host.model_budget.ledger.reserve(
                host.store.get_run_identity(run)["session_id"], run, "embedding-1",
                context=BudgetContext(host.default_workspace.project_id, run),
                input_estimate=1, output_estimate=outputs, estimate_method="fake",
                usage_basis="input_only", attempt_started={**identity(), **changes},
            )
        assert host.budget_status()["reservations"] == []
    finally:
        await host.close()


@pytest.mark.parametrize("texts", [[], [42]])
async def test_invalid_embedding_input_never_reserves(isolated_workspace, texts):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    try:
        with pytest.raises(ValueError, match="non-empty list of strings"):
            host.model_budget.begin_embedding(admitted(host), texts, identity())
        assert host.budget_status()["reservations"] == []
    finally:
        await host.close()
