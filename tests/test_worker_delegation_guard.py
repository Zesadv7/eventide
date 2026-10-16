"""Duplicate investigation uses admissions, never cached or invented worker output."""

import asyncio
import json

import pytest

from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.normalization import normalize
from eventide.tool_results import reader
from eventide.usage import build_usage_ledger
from eventide.workers import delegation_fingerprint, delegation_history
from tests.test_readonly_workers import report, worker_host


def delegate(call_id, *tasks):
    return ModelResponse(
        tool_calls=(ToolCall(call_id, "delegate_readonly", {"tasks": list(tasks)}),),
        usage={"input_tokens": 2, "output_tokens": 1},
    )


def goal(text="locate value", role="researcher"):
    return {"role": role, "goal": text}


class DelegatingProvider:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.worker_requests = []

    async def complete(self, request):
        self.requests.append(request)
        if "You are a read-only" in request.messages[0]["content"]:
            self.worker_requests.append(request)
            return ModelResponse(text=report(), usage={"input_tokens": 3, "output_tokens": 1})
        return next(
            self.responses,
            ModelResponse(text="done", usage={"input_tokens": 2, "output_tokens": 1}),
        )


def delegation_results(host, run_id):
    return {
        event["payload"]["call_id"]: event["payload"]
        for event in host.store.run_events(run_id)
        if event["type"] == "tool.completed" and event["payload"]["name"] == "delegate_readonly"
    }


async def test_same_run_blocks_duplicate_and_keeps_original_result_and_accounting(
    isolated_workspace,
):
    provider = DelegatingProvider(delegate("first", goal()), delegate("duplicate", goal()))
    host = worker_host(isolated_workspace, provider)
    try:
        result = await host.run(RunRequest("investigate"))
        assert result.status == "completed"
        assert len(provider.worker_requests) == 1
        results = delegation_results(host, result.run_id)
        assert not results["first"]["is_error"]
        assert results["duplicate"]["is_error"]
        content = results["duplicate"]["content"]
        assert "no workers started" in content
        assert f'"run_id": "{result.run_id}"' in content
        assert '"call_id": "first"' in content
        assert "read source" not in content  # no worker answer cache
        existing = reader(host.store, result.session_id)(result.run_id, "first")
        assert "read source" in existing
        usage = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert usage["attempt_count"] == 4  # 3 parent, 1 actual worker
        assert usage["reported_totals"] == {"input_tokens": 9, "output_tokens": 4}
        assert not list((host.state_dir / "workers").iterdir())
    finally:
        await host.close()


@pytest.mark.parametrize("existing", [False, True])
async def test_entire_batch_checked_before_any_child_is_admitted(isolated_workspace, existing):
    responses = [delegate("first", goal())] if existing else []
    responses += [
        delegate("batch", goal(), goal("different") if existing else goal()),
        delegate("next", goal("different")),
    ]
    provider = DelegatingProvider(*responses)
    host = worker_host(isolated_workspace, provider)
    try:
        result = await host.run(RunRequest("investigate"))
        assert result.status == "completed"
        assert len(provider.worker_requests) == (2 if existing else 1)
        results = delegation_results(host, result.run_id)
        assert results["batch"]["is_error"]
        assert not results["next"]["is_error"]
        admissions = host.store.worker_admissions(result.run_id)
        assert all(e["payload"]["delegation"]["parent_call_id"] != "batch" for e in admissions)
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["role", "goal", "source"])
async def test_real_source_progress_or_exact_different_task_allows_new_worker(
    isolated_workspace, change
):
    changed_goal = (
        goal(role="reviewer")
        if change == "role"
        else goal("different")
        if change == "goal"
        else goal()
    )
    responses = [delegate("first", goal())]
    if change == "source":
        responses.append(
            ModelResponse(
                tool_calls=(
                    ToolCall("write", "write_file", {"path": "code.py", "content": "value = 2\n"}),
                )
            )
        )
    responses.append(delegate("next", changed_goal))
    provider = DelegatingProvider(*responses)
    host = worker_host(isolated_workspace, provider)
    try:
        result = await host.run(RunRequest("investigate", mode="agent"))
        assert result.status == "completed"
        assert len(provider.worker_requests) == 2
        assert all(
            not item["is_error"] for item in delegation_results(host, result.run_id).values()
        )
        admissions = host.store.worker_admissions(result.run_id)
        snapshots = [event["payload"]["snapshot_id"] for event in admissions]
        assert (snapshots[0] != snapshots[1]) is (change == "source")
    finally:
        await host.close()


@pytest.mark.parametrize("restart", [False, True])
async def test_explicit_continue_retains_admission_guard_after_restart(isolated_workspace, restart):
    provider = DelegatingProvider(delegate("first", goal()), delegate("duplicate", goal()))
    host = worker_host(isolated_workspace, provider, max_steps=1)
    try:
        first = await host.run(RunRequest("investigate"))
        assert first.status == "interrupted"
        if restart:
            settings = host.settings
            await host.close()
            host = RuntimeHost(settings, provider)
        continued = await host.continue_session(first.session_id)
        assert continued.status == "interrupted"
        assert len(provider.worker_requests) == 1
        results = delegation_results(host, continued.run_id)
        assert results["duplicate"]["is_error"]
        assert first.run_id in results["duplicate"]["content"]
        assert host.store.worker_admissions(continued.run_id) == []
    finally:
        await host.close()


async def test_ordinary_new_run_and_other_session_do_not_share_guard_or_result_access(
    isolated_workspace,
):
    provider = DelegatingProvider(
        delegate("first", goal()),
        ModelResponse(text="done"),
        delegate("second", goal()),
        ModelResponse(text="done"),
        delegate("third", goal()),
    )
    host = worker_host(isolated_workspace, provider)
    try:
        first = await host.run(RunRequest("first"))
        second = await host.run(RunRequest("new independent intent", first.session_id))
        third = await host.run(RunRequest("other Session"))
        assert all(result.status == "completed" for result in [first, second, third])
        assert len(provider.worker_requests) == 3
        assert not delegation_results(host, second.run_id)["second"]["is_error"]
        assert not delegation_results(host, third.run_id)["third"]["is_error"]
        assert first.run_id not in str(delegation_history(host.store, third.run_id))
        assert reader(host.store, third.session_id)(first.run_id, "first").startswith("Error:")
    finally:
        await host.close()


async def test_rejected_duplicate_does_not_consume_four_worker_allowance(isolated_workspace):
    provider = DelegatingProvider(
        delegate("first", goal("one"), goal("two")),
        delegate("duplicate", goal("one")),
        delegate("remaining", goal("three"), goal("four")),
        delegate("over_limit", goal("five")),
    )
    host = worker_host(isolated_workspace, provider)
    try:
        result = await host.run(RunRequest("investigate"))
        assert result.status == "completed"
        assert len(provider.worker_requests) == 4
        results = delegation_results(host, result.run_id)
        assert results["duplicate"]["is_error"]
        assert not results["remaining"]["is_error"]
        assert results["over_limit"]["is_error"]
        assert "At most four" in results["over_limit"]["content"]
        assert len(host.store.worker_admissions(result.run_id)) == 4
    finally:
        await host.close()


async def test_old_missing_fingerprint_is_not_guessed_from_redacted_messages(isolated_workspace):
    host = worker_host(isolated_workspace, DelegatingProvider())
    try:
        session = host.create_session()
        host.store.create_run("old-parent", session)
        host.store.append_event(
            "old-parent",
            "tool.prepared",
            {
                "call_id": "old-call",
                "name": "delegate_readonly",
            },
        )
        child_session = host.create_session()
        host.store.admit_run(
            "old-child",
            child_session,
            [
                (
                    "run.admitted",
                    {
                        "parent_run_id": "old-parent",
                        "worker_role": "researcher",
                        "snapshot_id": "old-snapshot",
                    },
                    "system",
                    "runtime",
                )
            ],
        )
        assert delegation_history(host.store, "old-parent") == {}
    finally:
        await host.close()


async def test_invalid_continue_relation_does_not_read_foreign_session_facts(
    isolated_workspace, monkeypatch
):
    host = worker_host(isolated_workspace, DelegatingProvider())
    try:
        foreign = host.create_session()
        host.store.create_run("foreign-parent", foreign)
        local = host.create_session()
        host.store.create_run("local-run", local, continuation_of="foreign-parent")
        reads = []
        original = host.store.run_events

        def observed(run_id, **kwargs):
            reads.append(run_id)
            return original(run_id, **kwargs)

        monkeypatch.setattr(host.store, "run_events", observed)
        with pytest.raises(ValueError, match="Session boundary"):
            delegation_history(host.store, "local-run")
        assert reads == ["local-run"]
    finally:
        await host.close()


async def test_raw_secret_goal_hashes_differ_without_persisting_secret_copy(isolated_workspace):
    secret = "fixture-private-key"
    first_goal, second_goal = goal(f"Inspect {secret}"), goal("Inspect [REDACTED]")
    assert normalize(first_goal, (secret,)) == second_goal
    assert delegation_fingerprint(first_goal, "snapshot") != delegation_fingerprint(
        second_goal, "snapshot"
    )
    provider = DelegatingProvider(delegate("first", first_goal), delegate("next", second_goal))
    host = worker_host(isolated_workspace, provider, api_key=secret)
    try:
        result = await host.run(RunRequest("investigate"))
        assert len(provider.worker_requests) == 2
        admissions = host.store.worker_admissions(result.run_id)
        fingerprints = [event["payload"]["delegation"]["fingerprint"] for event in admissions]
        assert len(set(fingerprints)) == 2
        for admission in admissions:
            assert set(admission["payload"]["delegation"]) == {"fingerprint", "parent_call_id"}
        assert secret not in json.dumps(
            host.store.facts_by_type(
                ("run.admitted", "message.user", "model.response", "tool.prepared")
            )
        )
    finally:
        await host.close()


async def test_admission_survives_missing_worker_started_and_parent_result(
    isolated_workspace, monkeypatch
):
    provider = DelegatingProvider(delegate("first", goal()), delegate("duplicate", goal()))
    host = worker_host(isolated_workspace, provider, max_steps=1)
    original = host._emit

    async def cancel_before_started(run_id, kind, *args, **kwargs):
        if kind == "worker.started":
            raise asyncio.CancelledError
        return await original(run_id, kind, *args, **kwargs)

    monkeypatch.setattr(host, "_emit", cancel_before_started)
    try:
        with pytest.raises(asyncio.CancelledError):
            await host.run(RunRequest("investigate"))
        parent = next(
            e["run_id"]
            for e in host.store.facts_by_type(("run.admitted",))
            if not e["payload"].get("parent_run_id")
        )
        session = host.store.get_run_identity(parent)["session_id"]
        assert host.store.worker_admissions(parent)
        assert not any(e["type"] == "worker.started" for e in host.store.run_events(parent))
        settings = host.settings
        await host.close()
        host = RuntimeHost(settings, provider)
        continued = await host.continue_session(session)
        result = delegation_results(host, continued.run_id)["duplicate"]
        assert result["is_error"] and parent in result["content"]
        assert host.store.worker_admissions(continued.run_id) == []
        assert not list((host.state_dir / "workers").iterdir())
    finally:
        await host.close()
