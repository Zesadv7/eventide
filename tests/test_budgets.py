"""Shared budget admission, unknown usage and fee evidence are never reset silently."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eventide.budget_prices import CostLedger, PriceVersion
from eventide.budgets import (
    BUDGET_FACTS,
    BudgetContext,
    BudgetExceeded,
    BudgetLedger,
    BudgetScope,
)
from eventide.store import RuntimeStore

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


@pytest.fixture
def ledger(isolated_workspace):
    store = RuntimeStore(isolated_workspace / "budget.sqlite")
    workspace = store.register_workspace(isolated_workspace, None)
    for session in ("s", "s2", "control"):
        store.create_session(session, workspace.workspace_id)
    for run, session in (("root", "s"), ("second", "s2")):
        store.create_run(run, session)
    yield BudgetLedger(store)
    store.close()


def reserve(ledger, attempt="a", *, run="root", session="s", project="p", delivery=None,
            root=None, inputs=30, outputs=50, at=NOW):
    return ledger.reserve(
        session, run, attempt,
        context=BudgetContext(project, root or run, delivery),
        input_estimate=inputs, output_estimate=outputs, estimate_method="test tokenizer v1",
        at=at,
    )


def configure(ledger, limit, *, kind="project", identity="p", request="cfg"):
    return ledger.configure(
        "control", scope=BudgetScope(kind, identity), token_limit=limit, request_id=request,
    )


def bucket(ledger, kind="project", identity="p"):
    return ledger.snapshot(scopes=(BudgetScope(kind, identity),))["budgets"][0]


def test_atomic_reservation_warning_settlement_and_replay(ledger):
    configure(ledger, 100)
    receipt = reserve(ledger)
    assert receipt.status == "unsettled"
    assert receipt.actual_tokens is None
    assert receipt.warnings[0]["kind"] == "project"
    assert bucket(ledger)["remaining_tokens"] == 20
    assert reserve(ledger) == receipt
    with pytest.raises(ValueError, match="different content"):
        reserve(ledger, inputs=31)
    with pytest.raises(BudgetExceeded) as blocked:
        reserve(ledger, "b")
    assert blocked.value.violations[0]["requested_tokens"] == 80
    assert len(ledger.snapshot()["reservations"]) == 1
    settled = ledger.settle("s", "a", {"input_tokens": 10, "output_tokens": 5})
    assert settled.actual_tokens == 15
    assert settled.status == "settled"
    assert bucket(ledger)["reserved_tokens"] == 0
    assert bucket(ledger)["actual_tokens"] == 15
    assert ledger.settle("s", "a", {"input_tokens": 10, "output_tokens": 5}) == settled
    with pytest.raises(ValueError, match="Conflicting"):
        ledger.settle("s", "a", {"input_tokens": 10, "output_tokens": 6})
    assert reserve(ledger, "b").reserved_tokens == 80
    assert len(ledger.store.facts_by_type(("budget.settled",))) == 1


def test_zero_budget_disabled_budget_and_configuration_idempotency(ledger):
    assert configure(ledger, 0)["blocked"] is True
    with pytest.raises(BudgetExceeded):
        reserve(ledger, inputs=0, outputs=0)
    assert configure(ledger, 0)["blocked"] is True
    with pytest.raises(ValueError, match="different content"):
        configure(ledger, None)
    configure(ledger, None, request="disable")
    assert reserve(ledger).warnings == ()
    assert bucket(ledger)["remaining_tokens"] is None
    assert len(ledger.store.facts_by_type(("budget.configured",))) == 2


@pytest.mark.parametrize("scope", [
    BudgetScope("run", "root"), BudgetScope("project", "p"),
    BudgetScope("delivery", "d"), BudgetScope("day", "2026-10-16"),
])
def test_each_scope_independently_blocks_new_requests(ledger, scope):
    ledger.configure("control", scope=scope, token_limit=79, request_id="set")
    with pytest.raises(BudgetExceeded) as blocked:
        reserve(ledger, delivery="d")
    assert blocked.value.violations[0]["kind"] == scope.kind
    assert ledger.snapshot()["reservations"] == []


def test_daily_scope_uses_utc_and_pending_reservations_do_not_move_at_midnight(ledger):
    configure(ledger, 100, kind="day", identity="2026-10-16")
    configure(ledger, 50, kind="day", identity="2026-10-17", request="tomorrow")
    local = datetime(2026, 10, 17, 1, tzinfo=timezone(timedelta(hours=8)))
    receipt = reserve(ledger, at=local)
    assert receipt.buckets[-1]["identity"] == "2026-10-16"
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "tomorrow", at=NOW + timedelta(days=1))
    assert bucket(ledger, "day", "2026-10-16")["reserved_tokens"] == 80
    assert bucket(ledger, "day", "2026-10-17")["reserved_tokens"] == 0


@pytest.mark.parametrize("usage,source", [
    (None, "unknown"), ({}, "reported"), ({"input_tokens": 12}, "reported"),
    ({"input_tokens": 12, "output_tokens": 5}, "estimated"),
])
def test_missing_and_estimated_usage_keeps_reservation_after_restart(ledger, usage, source):
    configure(ledger, 100)
    reserve(ledger)
    ledger.settle("s", "a", usage, source=source)
    path = ledger.store.path
    ledger.store.close()
    ledger.store = RuntimeStore(path)
    assert bucket(ledger)["reserved_tokens"] == 80
    assert bucket(ledger)["actual_tokens"] == 0
    assert bucket(ledger)["unsettled_attempts"] == 1
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "retry")
    receipt = ledger.resolve_unknown(
        "s", "a", actual_tokens=7, reason="User checked provider usage", request_id="resolved",
    )
    assert receipt.actual_tokens == 7
    assert receipt.status == "manually_resolved"
    assert ledger.resolve_unknown(
        "s", "a", actual_tokens=7, reason="User checked provider usage", request_id="resolved",
    ) == receipt
    assert bucket(ledger)["remaining_tokens"] == 93
    with pytest.raises(ValueError, match="already settled"):
        ledger.resolve_unknown("s", "a", actual_tokens=0, reason="different", request_id="other")


def test_reported_partial_larger_than_estimate_keeps_observed_lower_bound(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {"input_tokens": 100})
    assert bucket(ledger)["reserved_tokens"] == 100
    ledger.settle("s", "a", {"input_tokens": 100, "output_tokens": 10})
    assert bucket(ledger)["actual_tokens"] == 110
    assert bucket(ledger)["reserved_tokens"] == 0


def test_actual_overshoot_blocks_subsequent_admission_and_is_not_clamped(ledger):
    configure(ledger, 100)
    reserve(ledger)
    ledger.settle("s", "a", {"input_tokens": 100, "output_tokens": 20})
    assert bucket(ledger)["actual_tokens"] == 120
    assert bucket(ledger)["remaining_tokens"] == 0
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "next", inputs=0, outputs=0)


def test_child_runs_share_root_delivery_project_and_day_without_double_counting(ledger):
    ledger.store.create_session("child")
    ledger.store.create_run("child-run", "child")
    ledger.store.append_fact(
        "child", "run.admitted", {"parent_run_id": "root"}, run_id="child-run",
    )
    configure(ledger, 150, kind="run", identity="root")
    reserve(ledger, delivery="d")
    child = reserve(ledger, "child-attempt", run="child-run", session="child", root="root",
                    delivery="d", inputs=10, outputs=20)
    assert bucket(ledger, "run", "root")["reserved_tokens"] == 110
    assert bucket(ledger, "run", "child-run")["reserved_tokens"] == 30
    assert bucket(ledger, "delivery", "d")["reserved_tokens"] == 110
    assert bucket(ledger)["reserved_tokens"] == 110
    assert child.actual_tokens is None
    with pytest.raises(ValueError, match="ownership"):
        reserve(ledger, "escaped", run="child-run", session="child", root="root", project="q")
    with pytest.raises(ValueError, match="actual root"):
        reserve(ledger, "escaped2", run="child-run", session="child", root="child-run")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "second-root-call", delivery="d")
    ledger.settle("child", "child-attempt", {"input_tokens": 5, "output_tokens": 2})
    assert bucket(ledger, "run", "root")["committed_tokens"] == 87


def test_continue_does_not_reset_shared_delivery_or_project(ledger):
    configure(ledger, 100, kind="delivery", identity="d")
    reserve(ledger, delivery="d")
    ledger.store.finish_run("root", status="interrupted", output="")
    ledger.store.create_run("continue", "s", continuation_of="root")
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "continue-attempt", run="continue", delivery="d")
    # Settlement remains valid after a terminal, in its Session's control facts.
    ledger.settle("s", "a", {"input_tokens": 50, "output_tokens": 30})
    assert bucket(ledger, "delivery", "d")["actual_tokens"] == 80


def test_multiple_store_connections_serialize_competing_reservations(ledger):
    configure(ledger, 100)
    peer_store = RuntimeStore(ledger.store.path)
    peer = BudgetLedger(peer_store)

    def call(index):
        owner = ledger if index % 2 else peer
        try:
            reserve(owner, f"attempt-{index}", inputs=6, outputs=4)
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(30)))
    assert sum(results) == 10
    assert bucket(ledger)["committed_tokens"] == 100
    assert len(ledger.store.facts_by_type(("budget.reserved",))) == 10
    peer_store.close()


def test_store_fact_transaction_rolls_back_all_additions(ledger):
    with pytest.raises(TypeError):
        ledger.store.transact_facts("s", (), lambda events: (None, [
            ("first", {}), ("bad", {"cannot_serialize": set()}),
        ]))
    assert ledger.store.facts_by_type(("first", "bad")) == []
    with pytest.raises(ValueError, match="Session not found"):
        ledger.store.transact_facts("missing", (), lambda events: (None, []))


def test_cross_session_ownership_and_unknown_resolution_ids(ledger):
    reserve(ledger)
    with pytest.raises(ValueError, match="Run/session"):
        reserve(ledger, "wrong", session="s2")
    with pytest.raises(ValueError, match="Attempt/session"):
        ledger.settle("s2", "a", None)
    with pytest.raises(ValueError, match="Unknown attempt"):
        ledger.settle("s", "missing", None)
    ledger.resolve_unknown("s", "a", actual_tokens=0, reason="Confirmed uncharged", request_id="x")
    reserve(ledger, "b")
    with pytest.raises(ValueError, match="reused"):
        ledger.resolve_unknown("s", "b", actual_tokens=0, reason="Confirmed", request_id="x")


@pytest.mark.parametrize("bad", [True, -1, 1.2, "10"])
def test_token_counts_are_strict(ledger, bad):
    with pytest.raises(ValueError, match="nonnegative integer"):
        configure(ledger, bad)
    with pytest.raises(ValueError, match="nonnegative integer"):
        reserve(ledger, inputs=bad)
    reserve(ledger)
    with pytest.raises(ValueError, match="nonnegative integer"):
        ledger.settle("s", "a", {"input_tokens": bad})


def register(costs, category, rate, *, currency="USD", version=None,
             effective=NOW - timedelta(days=2)):
    return costs.register_price("control", PriceVersion(
        version or f"{category}-{currency}-{rate}", "provider", "model", category,
        currency, rate, effective,
    ))


def estimate(costs, attempt="a", name="first", **kwargs):
    return costs.estimate_cost(
        "s", attempt, provider="provider", model="model", estimate_id=name, **kwargs,
    )


def test_price_versions_freeze_original_estimate_and_revaluation_is_new_evidence(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {"input_tokens": 1000, "output_tokens": 100})
    costs = CostLedger(ledger.store)
    old = register(costs, "input_tokens", "1")
    register(costs, "output_tokens", "2")
    first = estimate(costs)
    assert Decimal(first["estimates"]["USD"]["known_amount"]) == Decimal("0.0012")
    assert first["complete"] is True
    register(costs, "input_tokens", "3", effective=NOW - timedelta(days=1))
    assert estimate(costs) == first
    with pytest.raises(ValueError, match="explicit reevaluation"):
        estimate(costs, name="other")
    revised = estimate(costs, name="revised", reevaluation_of="first")
    assert Decimal(revised["estimates"]["USD"]["known_amount"]) == Decimal("0.0032")
    assert costs.snapshot()["history"] == [first, revised]
    assert len(costs.snapshot()["latest"]) == 1
    assert costs.register_price("control", PriceVersion(
        old["version_id"], "provider", "model", "input_tokens", "USD", "1",
        NOW - timedelta(days=2),
    )) == old
    with pytest.raises(ValueError, match="identity was reused"):
        costs.register_price("control", PriceVersion(
            old["version_id"], "provider", "model", "input_tokens", "USD", "5", NOW,
        ))


def test_cache_and_reasoning_prices_partition_total_usage_once(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {
        "input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 100, "reasoning_tokens": 20,
        "uncached_input_tokens": 400,
    })
    costs = CostLedger(ledger.store)
    for category, rate in (
        ("input_tokens", "1"), ("cache_read_input_tokens", "0.1"),
        ("cache_creation_input_tokens", "2"), ("output_tokens", "2"),
        ("reasoning_tokens", "3"),
    ):
        register(costs, category, rate)
    result = estimate(costs)["estimates"]["USD"]
    assert sum(line["quantity"] for line in result["lines"]) == 1100
    assert Decimal(result["known_amount"]) == Decimal("0.00087")
    assert result["complete"] is True


def test_no_price_missing_usage_and_unknown_categories_are_never_free(ledger):
    reserve(ledger)
    costs = CostLedger(ledger.store)
    result = estimate(costs)
    assert result["estimates"] == {}
    assert result["unknown_reason"] == "no_effective_price"
    assert costs.snapshot()["known_costs"] == {}
    register(costs, "input_tokens", "1")
    unknown = estimate(costs, name="priced", reevaluation_of="first")
    assert unknown["complete"] is False
    assert unknown["estimates"]["USD"]["known_amount"] is None
    ledger.settle("s", "a", {"input_tokens": 1000, "output_tokens": 100})
    partial = estimate(costs, name="partial", reevaluation_of="priced")
    assert partial["estimates"]["USD"]["unknown_categories"] == ["output_tokens"]
    assert Decimal(partial["estimates"]["USD"]["known_amount"]) == Decimal("0.001")
    assert costs.snapshot()["unknown_attempts"] == 1


def test_different_currencies_and_future_prices_are_not_mixed(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {"input_tokens": 1000, "output_tokens": 100})
    costs = CostLedger(ledger.store)
    for currency, multiplier in (("USD", 1), ("CNY", 7)):
        register(costs, "input_tokens", str(multiplier), currency=currency)
        register(costs, "output_tokens", str(2 * multiplier), currency=currency)
    register(costs, "input_tokens", "100", version="future", effective=NOW + timedelta(days=1))
    estimate(costs)
    assert {key: Decimal(value) for key, value in costs.snapshot()["known_costs"].items()} == {
        "USD": Decimal("0.0012"), "CNY": Decimal("0.0084"),
    }


def test_expected_cache_or_reasoning_detail_missing_keeps_fee_incomplete(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {"input_tokens": 1000, "output_tokens": 100})
    costs = CostLedger(ledger.store)
    for category in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "reasoning_tokens",
    ):
        register(costs, category, "1")
    result = estimate(costs)
    assert result["complete"] is False
    assert result["estimates"]["USD"]["known_amount"] is None
    assert "cache_read_input_tokens" in result["estimates"]["USD"]["unknown_categories"]


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-1", "invalid", "1e-19", "1e13", 1.0])
def test_price_validation_rejects_imprecise_and_nonfinite_numbers(ledger, rate):
    with pytest.raises(ValueError):
        register(CostLedger(ledger.store), "input_tokens", rate)


def test_budget_and_cost_facts_survive_replay_and_unknown_upgrade(ledger):
    reserve(ledger)
    ledger.settle("s", "a", None)
    ledger.settle("s", "a", {"input_tokens": 0, "output_tokens": 0})
    costs = CostLedger(ledger.store)
    register(costs, "input_tokens", "0")
    register(costs, "output_tokens", "1")
    first = estimate(costs)
    assert first["complete"] is True
    assert Decimal(first["estimates"]["USD"]["known_amount"]) == 0
    facts = ledger.store.facts_by_type(BUDGET_FACTS)
    for event in facts:
        ledger.store.append_fact("control", event["type"], event["payload"])
    assert bucket(ledger)["actual_tokens"] == 0
    assert len(ledger.snapshot()["reservations"]) == 1


def test_cost_summary_keeps_calls_without_an_estimate_unknown(ledger):
    reserve(ledger)
    costs = CostLedger(ledger.store)
    assert costs.snapshot()["unknown_attempts"] == 1
    assert costs.snapshot()["unpriced_attempt_ids"] == ["a"]
    assert costs.snapshot()["complete"] is False
    ledger.settle("s", "a", {"input_tokens": 0, "output_tokens": 0})
    register(costs, "input_tokens", "1")
    result = estimate(costs)
    assert result["complete"] is False
    assert result["estimates"]["USD"]["unknown_categories"] == ["output_tokens"]


def test_old_configuration_and_unknown_settlement_retries_do_not_restore_old_state(ledger):
    configure(ledger, 100)
    configure(ledger, None, request="disabled")
    assert configure(ledger, 100)["token_limit"] is None
    reserve(ledger)
    ledger.settle("s", "a", None)
    ledger.settle("s", "a", {"input_tokens": 10, "output_tokens": 5})
    assert ledger.settle("s", "a", None).actual_tokens == 15
    assert len(ledger.store.facts_by_type(("budget.settled",))) == 2


@pytest.mark.parametrize("kind,identity", [
    ("unknown", "p"), ("run", ""), ("day", "2026-2-3"), ("day", "bad"),
])
def test_invalid_scope_rejected(kind, identity):
    with pytest.raises(ValueError):
        BudgetScope(kind, identity)


def test_naive_time_unknown_source_and_bad_counter_semantics_rejected(ledger):
    with pytest.raises(ValueError, match="UTC offset"):
        reserve(ledger, at=datetime(2026, 10, 16))
    reserve(ledger)
    with pytest.raises(ValueError, match="Unknown usage source"):
        ledger.settle("s", "a", None, source="unsupported")
    with pytest.raises(ValueError, match="Unknown usage cannot"):
        ledger.settle("s", "a", {"input_tokens": 1}, source="unknown")
    costs = CostLedger(ledger.store)
    with pytest.raises(ValueError, match="Unknown attempt"):
        estimate(costs, attempt="not-real")
    with pytest.raises(ValueError, match="Attempt/session"):
        costs.estimate_cost("s2", "a", provider="provider", model="model", estimate_id="bad")
    with pytest.raises(ValueError, match="same attempt"):
        estimate(costs, reevaluation_of="missing")


def test_price_category_currency_effective_time_and_estimate_identity_rejected(ledger):
    costs = CostLedger(ledger.store)
    with pytest.raises(ValueError, match="pricing category"):
        register(costs, "not-token-billing", "1")
    with pytest.raises(ValueError, match="Currency"):
        register(costs, "input_tokens", "1", currency="usd")
    register(costs, "input_tokens", "1")
    with pytest.raises(ValueError, match="already has a version"):
        register(costs, "input_tokens", "2")
    reserve(ledger)
    estimate(costs)
    with pytest.raises(ValueError, match="identity was reused"):
        costs.estimate_cost("s", "a", provider="other", model="model", estimate_id="first")


def test_cost_replay_preserves_attempt_ownership(ledger):
    reserve(ledger)
    costs = CostLedger(ledger.store)
    estimate(costs)
    with pytest.raises(ValueError, match="Attempt/session"):
        costs.estimate_cost(
            "s2", "a", provider="provider", model="model", estimate_id="first",
        )


def test_malformed_subset_is_not_charged_as_known_usage(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {
        "input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 5,
    })
    costs = CostLedger(ledger.store)
    for category in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
        register(costs, category, "1")
    result = estimate(costs)["estimates"]["USD"]
    assert not result["complete"]
    assert result["unknown_categories"] == ["input_tokens"]
    assert [line["category"] for line in result["lines"]] == ["output_tokens"]


def test_estimated_token_costs_stay_separate_from_reported_costs(ledger):
    reserve(ledger)
    ledger.settle("s", "a", {"input_tokens": 100, "output_tokens": 20}, source="estimated")
    costs = CostLedger(ledger.store)
    register(costs, "input_tokens", "1")
    register(costs, "output_tokens", "2")
    estimate(costs)
    result = costs.snapshot()
    assert result["known_costs"] == {}
    assert Decimal(result["estimated_usage_costs"]["USD"]) == Decimal("0.00014")
    assert result["estimated_attempts"] == 1
    assert not result["complete"]
    assert bucket(ledger)["reserved_tokens"] == 80


def test_budget_facts_follow_append_order_when_wall_clock_moves_backwards(ledger, monkeypatch):
    import eventide.store as store_module

    monkeypatch.setattr(store_module.time, "time", lambda: 100.0)
    configure(ledger, 100)
    monkeypatch.setattr(store_module.time, "time", lambda: 50.0)
    configure(ledger, 0, request="stop")
    assert bucket(ledger)["token_limit"] == 0
    with pytest.raises(BudgetExceeded):
        reserve(ledger)
    assert all(e["author"] == "user" for e in ledger.store.facts_by_type(("budget.configured",)))
