"""Delivery limits survive new roots and include auxiliary/child actual attempts."""

import time
from pathlib import Path

import pytest

from eventide.budgets import BudgetExceeded
from eventide.delivery_limits import delivery_limit_reason, delivery_usage
from eventide.models import ModelRequest
from tests.test_budget_api import new_run
from tests.test_delivery_runtime import delivery_host


@pytest.fixture
async def flow(tmp_path):
    host = delivery_host(tmp_path)
    prepared = await host.manage_delivery("prepare", name="limit-fixture")
    delivery = prepared["delivery_id"]
    workspace = host.resolve_or_register_workspace(Path(prepared["receipt"]["checkout"]))
    control_session = new_run(host, "control")
    host.store.append_event("control", "run.started", {})
    host.store.append_fact(
        control_session,
        "delivery.repair_started",
        {
            "delivery_id": delivery,
            "control_run_id": "control",
            "deadline": time.time() + 900,
            "limits": {"model_attempts": 36, "observed_tokens": 60_000, "seconds": 900},
        },
        run_id="control",
    )
    new_run(host, "writer", workspace=workspace, delivery=delivery)
    new_run(host, "next-writer", workspace=workspace, delivery=delivery)
    new_run(host, "child", workspace=workspace, parent="writer")
    try:
        yield host, delivery
    finally:
        await host.close()


def attempt(host, run, number, kind="agent", usage=None):
    identity = {
        "attempt_id": f"attempt-{number}",
        "request_id": f"request-{number}",
        "attempt": 1,
        "kind": kind,
        "provider": "scripted",
        "model": "fixture",
    }
    host.model_budget.begin(run, ModelRequest("", [], [], "fixture", 1), identity)
    host.model_budget.finish(
        run,
        {
            **identity,
            "status": "completed",
            "usage": usage
            or {
                "input_tokens": 1,
                "output_tokens": 1,
            },
        },
    )


async def test_thirty_six_attempts_include_summary_workers_retries_and_new_runs(flow):
    host, delivery = flow
    for number in range(36):
        run = ("writer", "next-writer", "child")[number % 3]
        kind = ("agent", "summary", "worker", "query_rewrite")[number % 4]
        attempt(host, run, number, kind)
    assert delivery_usage(host.store, delivery)["attempt_count"] == 36
    assert delivery_limit_reason(host.store, delivery) == "delivery_step_limit"
    # Exactly-at-limit candidate may still undergo independent verification.
    assert delivery_limit_reason(host.store, delivery, admission=False) is None
    with pytest.raises(BudgetExceeded) as error:
        attempt(host, "next-writer", 37)
    assert error.value.violations[0]["reason"] == "delivery_step_limit"
    assert delivery_usage(host.store, delivery)["attempt_count"] == 36


async def test_embedding_usage_counts_toward_total_but_not_generation_steps(flow):
    host, delivery = flow
    identity = {
        "attempt_id": "embedding",
        "request_id": "embedding-request",
        "attempt": 1,
        "kind": "embedding",
        "provider": "openai_compatible",
        "model": "embedding",
        "usage_basis": "input_only",
    }
    host.model_budget.begin_embedding("child", ["input"], identity)
    host.model_budget.finish(
        "child", {**identity, "status": "completed", "usage": {"input_tokens": 60_000}}
    )
    assert delivery_limit_reason(host.store, delivery) == "delivery_token_limit"
    with pytest.raises(BudgetExceeded):
        attempt(host, "next-writer", 1)
    assert delivery_usage(host.store, delivery)["reported_totals"] == {"input_tokens": 60_000}


async def test_deadline_and_stopped_owner_survive_fresh_attempt_id(flow, monkeypatch):
    host, delivery = flow
    now = time.time()
    monkeypatch.setattr("eventide.delivery_limits.time.time", lambda: now + 901)
    with pytest.raises(BudgetExceeded) as error:
        attempt(host, "writer", 1)
    assert error.value.violations[0]["reason"] == "delivery_time_limit"
    monkeypatch.undo()
    host.store.append_event("control", "run.interrupted", {"reason": "host_stopped"})
    with pytest.raises(BudgetExceeded) as error:
        attempt(host, "next-writer", 2)
    assert error.value.violations[0]["reason"] == "delivery_owner_stopped"


async def test_finished_delivery_blocks_manual_continue_even_below_budget(flow):
    host, delivery = flow
    host.store.append_event(
        "control",
        "delivery.repair_finished",
        {
            "delivery_id": delivery,
            "reason": "passed",
        },
    )
    with pytest.raises(BudgetExceeded) as error:
        attempt(host, "child", 1)
    assert error.value.violations[0]["reason"] == "delivery_closed"
