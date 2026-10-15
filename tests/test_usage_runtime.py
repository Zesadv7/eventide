"""Production attempt facts, recursive worker scope and full-history usage APIs."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.usage import build_usage_ledger
from tests.test_runtime import settings_for


async def test_real_attempt_ledger_records_retries_without_legacy_double_count(isolated_workspace):
    host = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider(
            [
                {"error": "503", "retryable": True},
                {"text": "done", "usage": {"input_tokens": 0, "output_tokens": 8}},
            ]
        ),
    )
    try:
        result = await host.run(RunRequest("work"))
        facts = host.store.usage_events(run_id=result.run_id)
        ledger = build_usage_ledger(facts)
        assert ledger["attempt_count"] == 2
        assert ledger["reported_totals"] == {"input_tokens": 0, "output_tokens": 8}
        assert ledger["unknown_attempts"] == 1
        assert ledger["complete"] is False
        assert build_usage_ledger(facts + facts) == ledger
    finally:
        await host.close()


async def test_cancelled_request_stays_unknown(isolated_workspace):
    entered = asyncio.Event()

    class SlowProvider:
        async def complete(self, request):
            entered.set()
            await asyncio.Event().wait()
            return ModelResponse("unreachable")

    host = RuntimeHost(settings_for(isolated_workspace), SlowProvider())
    task = asyncio.create_task(host.run(RunRequest("work", run_id="cancelled-call")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await host.cancel_run("cancelled-call")
        with pytest.raises(asyncio.CancelledError):
            await task
        ledger = build_usage_ledger(host.store.usage_events(run_id="cancelled-call"))
        assert ledger["unknown_attempts"] == 1
        assert ledger["reported_totals"] == {}
        assert ledger["calls"][0]["status"] == "cancelled"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


def test_usage_api_includes_descendants_once_and_covers_all_history(isolated_workspace):
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    session = host.create_session()
    # More than the default Run history page. Unknown legacy retry completeness
    # is retained even though each historical response reports numeric totals.
    for index in range(105):
        run = f"old-{index}"
        host.store.create_run(run, session)
        host.store.append_event(
            run,
            "model.response",
            {
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
        host.store.append_event(
            run, "run.completed", {"usage": {"input_tokens": 1, "output_tokens": 1}}
        )
    child = host.create_session()
    host.store.create_run("child", child)
    host.store.append_event("child", "run.started", {"parent_run_id": "old-0"})
    identity = {"request_id": "child-request", "attempt_id": "child-attempt", "attempt": 1}
    host.store.append_event("child", "model.attempt_started", identity)
    host.store.append_event(
        "child",
        "model.attempt_finished",
        {
            **identity,
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "usage_source": "reported",
            "status": "completed",
        },
    )
    host.store.append_event(
        "child", "run.completed", {"usage": {"input_tokens": 3, "output_tokens": 2}}
    )
    with TestClient(create_app(host)) as client:
        response = client.get(f"/api/sessions/{session}/usage")
        assert response.status_code == 200
        ledger = response.json()
        assert ledger["reported_totals"] == {"input_tokens": 108, "output_tokens": 107}
        assert ledger["attempt_count"] == 106
        without = client.get(f"/api/sessions/{session}/usage?include_children=false").json()
        assert without["reported_totals"] == {"input_tokens": 105, "output_tokens": 105}
        assert client.get("/api/runs/old-0/usage").json()["reported_totals"] == {
            "input_tokens": 4,
            "output_tokens": 3,
        }
        assert client.get("/api/runs/missing/usage").status_code == 404
        assert client.get(f"/api/sessions/{session}/usage").json() == ledger
    reopened = RuntimeHost(settings, ScriptedProvider([]))
    try:
        assert build_usage_ledger(reopened.store.usage_events(session_id=session)) == ledger
    finally:
        asyncio.run(reopened.close())
