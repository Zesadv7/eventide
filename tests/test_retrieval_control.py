"""Python retrieval experiments share Host identity, ownership and authoritative usage."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.budgets import BudgetScope
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.benchmark import run_query_rewrite_benchmark
from eventide.rag.embedding import HashEmbedder
from eventide.rag.evaluation import _profile
from eventide.usage import build_usage_ledger
from tests.test_evaluation_control import host_for, spin_until
from tests.test_host import make_repo


async def benchmark(host, **kwargs):
    return await run_query_rewrite_benchmark(
        host,
        project_id=host.default_workspace.project_id,
        embedder=HashEmbedder(16),
        profile=_profile(16),
        confirmed=True,
        repeats=1,
        **kwargs,
    )


async def test_all_forty_queries_use_distinct_control_roots_and_one_attempt(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    host.provider = ScriptedProvider(
        [
            {
                "text": "credentials validation lifecycle",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                },
            }
        ]
        * 40
    )
    source = Path(host.default_workspace.path)
    before = host._workspace_checkpoint(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("Ranking evaluation cannot format or parse model tool display")

    monkeypatch.setattr("eventide.retrieval_rewrite.format_results", forbidden)
    try:
        report = await benchmark(host, split="all", output_path=tmp_path / "report.json")
        assert report["status"] == "completed"
        assert report["comparisons"]["query_rewrite"]["total"] == 40
        assert report["comparisons"]["original"]["total"] == 40
        assert len(report["evidence"]["run_ids"]) == 40
        assert len(set(report["evidence"]["run_ids"])) == 40
        assert report["remaining"] == []
        assert host._workspace_checkpoint(source) == before
        assert not (source / ".eventide/index.sqlite").exists()
        assert report["evidence"]["database"] == str(host.store.path)
        assert report["evidence"]["usage"]["reported_totals"] == {
            "input_tokens": 120,
            "output_tokens": 80,
        }
        assert len(host.provider.requests) == 40
        assert all(r.tools == [] and r.generation == {} for r in host.provider.requests)
        split_counts = {
            name: sum(
                row["split"] == name for row in report["comparisons"]["query_rewrite"]["results"]
            )
            for name in ("development", "holdout")
        }
        assert split_counts == {"development": 20, "holdout": 20}
        for run in report["evidence"]["run_ids"]:
            facts = host.store.run_events(run)
            admitted = facts[0]["payload"]
            assert admitted["source"] == admitted["mode"] == "python_experiment"
            assert host.run_execution(run)["purpose"] == "retrieval_benchmark"
            assert host._is_python_experiment_run(run)
            assert host.store.get_session(facts[0]["session_id"])["archived"]
            assert sum(e["type"] == "retrieval.rewrite_claimed" for e in facts) == 1
            assert sum(e["type"] == "model.attempt_started" for e in facts) == 1
            assert not any(e["type"].startswith("tool.") for e in facts)
            assert facts[-1]["type"] == "run.completed"
            assert host.model_budget.context(run)[1].root_run_id == run
            assert host.model_budget.context(run)[1].project_id == host.default_workspace.project_id
        rows = report["comparisons"]["query_rewrite"]["results"]
        assert all(
            row["retrieval_duration_ms"] >= 0 and row["model_duration_ms"] >= 0 for row in rows
        )
        assert report["build_duration_ms"] >= 0
        assert all(
            row["returned_characters"]
            == sum(len(hit["chunk"]["text"]) for hit in row["ranking"]["results"])
            for row in rows
        )
        assert json.loads((tmp_path / "report.json").read_text()) == json.loads(json.dumps(report))
        assert not report["promotion"]["promote"]
    finally:
        await host.close()


@pytest.mark.parametrize(
    "response",
    [
        {"error": "private protocol error"},
        {"error": "private transport error", "retryable": True},
    ],
)
async def test_failure_keeps_original_candidates_unknown_usage_and_stops_batch(tmp_path, response):
    host = host_for(tmp_path)
    host.provider = ScriptedProvider([response, {"text": "must not dispatch"}])
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted"
        candidate = report["comparisons"]["query_rewrite"]
        assert candidate["total"] == candidate["failed"] == 1
        assert candidate["unattempted"] == len(report["remaining"]) == 19
        assert (
            candidate["results"][0]["ranking"]["results"]
            == report["comparisons"]["original"]["results"][0]["ranking"]["results"]
        )
        assert len(host.provider.requests) == 1
        assert "private protocol" not in json.dumps(report)
        if "error" in response:
            assert report["evidence"]["usage"]["unknown_attempts"] == 1
            assert report["evidence"]["usage"]["reported_totals"] == {}
        run = report["evidence"]["run_ids"][0]
        assert host.store.get_run(run)["status"] == "failed"
        assert not (Path(host.default_workspace.path) / "escape.txt").exists()
    finally:
        await host.close()


@pytest.mark.parametrize(
    "response",
    [
        {"text": "bad\nformat"},
        {"text": ""},
        {"text": "partial", "stop_reason": "length"},
        {
            "tool_calls": [
                {
                    "id": "write",
                    "name": "write_file",
                    "arguments": {
                        "path": "escape.txt",
                        "content": "injected",
                    },
                }
            ]
        },
    ],
)
async def test_invalid_model_outputs_keep_fallback_and_continue_remaining_queries(
    tmp_path, response
):
    host = host_for(tmp_path)
    host.provider = ScriptedProvider(
        [
            {
                **response,
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                },
            }
        ]
        * 20
    )
    try:
        report = await benchmark(host)
        assert report["status"] == "completed"
        candidate = report["comparisons"]["query_rewrite"]
        assert candidate["total"] == candidate["degraded"] == candidate["invalid_outputs"] == 20
        assert candidate["failed"] == candidate["unattempted"] == 0
        assert candidate["successful_rewrites"] == 0
        assert len(host.provider.requests) == 20
        assert report["remaining"] == []
        assert report["evidence"]["usage"]["attempt_count"] == 20
        assert report["evidence"]["usage"]["reported_totals"] == {
            "input_tokens": 60,
            "output_tokens": 40,
        }
        for original, rewritten in zip(
            report["comparisons"]["original"]["results"],
            candidate["results"],
            strict=True,
        ):
            assert rewritten["ranking"]["results"] == original["ranking"]["results"]
            assert rewritten["status"] == "degraded" and not rewritten["rewrite_succeeded"]
            assert host.store.get_run(rewritten["run_id"])["status"] == "completed"
        assert not (Path(host.default_workspace.path) / "escape.txt").exists()
        assert not report["promotion"]["promote"]
    finally:
        await host.close()


async def test_project_budget_blocks_without_dispatch_and_keeps_original_ranking(tmp_path):
    host = host_for(tmp_path)
    host.configure_budget(
        BudgetScope("project", host.default_workspace.project_id), token_limit=0, request_id="zero"
    )
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted"
        assert report["evidence"]["usage"]["attempt_count"] == 0
        assert host.provider.requests == []
        candidate = report["comparisons"]["query_rewrite"]["results"][0]
        assert candidate["status"] == "interrupted"
        assert (
            candidate["ranking"]["results"]
            == report["comparisons"]["original"]["results"][0]["ranking"]["results"]
        )
        assert len(report["remaining"]) == 19
        assert host.store.get_run(candidate["run_id"])["status"] == "interrupted"
    finally:
        await host.close()


async def test_queued_cancel_never_executes_and_control_session_rejects_agent_continue(tmp_path):
    host = host_for(tmp_path)
    seen = []

    async def execute(run):
        seen.append(run)

    try:
        for _ in range(4):
            await host.admit_run(
                RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
            )
        task = asyncio.create_task(
            host.run_control_operation(
                kind="retrieval_benchmark",
                project_id=host.default_workspace.project_id,
                run_id="queued_experiment",
                accepted={"query": "frozen"},
                execute=execute,
            )
        )
        await spin_until(lambda: host.store.get_run_identity("queued_experiment"))
        run = host.store.get_run("queued_experiment")
        assert host.dispatch.snapshot()["counts"]["active_roots"] == 4
        assert host.dispatch.snapshot()["queued"][0]["run_id"] == "queued_experiment"
        with pytest.raises(ValueError, match="busy"):
            await host.run_control_operation(
                kind="retrieval_benchmark",
                project_id=host.default_workspace.project_id,
                run_id="other",
                accepted={},
                execute=execute,
            )
        await host.cancel_run("queued_experiment")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen == [] and host.provider.requests == []
        assert host.store.get_run("queued_experiment")["status"] == "interrupted"
        with pytest.raises(ValueError, match="Control|control|ordinary"):
            await host.admit_run(RunRequest("cannot be agent", run["session_id"]))
        for operation in (host.continue_session, host.abandon_interruption):
            with pytest.raises(ValueError, match="Control|control|ordinary"):
                await operation(run["session_id"])
        with pytest.raises(ValueError, match="already used"):
            await host.run_control_operation(
                kind="retrieval_benchmark",
                project_id=host.default_workspace.project_id,
                run_id="queued_experiment",
                accepted={},
                execute=execute,
            )
    finally:
        await host.close()


async def test_repeat_cancellation_waits_real_provider_cleanup_and_exports_partial(tmp_path):
    host = host_for(tmp_path)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Provider:
        async def complete(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

    host.provider = Provider()
    task = asyncio.create_task(benchmark(host, output_path=tmp_path / "partial.json"))
    try:
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert host.dispatch.scheduler.counts()["active_models"] == 1
        assert host.dispatch.scheduler.counts()["active_roots"] == 1
        assert len(host._run_tasks) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        report = json.loads((tmp_path / "partial.json").read_text())
        assert report["status"] == "aborted" and len(report["remaining"]) == 19
        assert report["evidence"]["usage"]["attempt_count"] == 1
        assert report["evidence"]["usage"]["unknown_attempts"] == 1
        row = report["comparisons"]["query_rewrite"]["results"][0]
        assert row["status"] == "interrupted" and row["ranking"]["results"]
        assert host.store.get_run(row["run_id"])["status"] == "interrupted"
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        release.set()
        await host.close()


async def test_restart_parks_started_experiment_without_source_checkpoint_or_replay(tmp_path):
    host = host_for(tmp_path)
    original_settings = host.settings
    # Simulate abrupt process death by persisting the accepted/started facts only.
    owner = host.create_session("python_retrieval_benchmark_" + host.default_workspace.project_id)
    host.store.update_session(owner, archived=True)
    host.store.append_fact(
        owner,
        "control.session",
        {
            "kind": "python_experiment",
            "purpose": "retrieval_benchmark",
            "project_id": host.default_workspace.project_id,
        },
    )
    intent = {
        "source": "python_experiment",
        "mode": "python_experiment",
        "project_id": host.default_workspace.project_id,
        "workspace_id": host.default_workspace.workspace_id,
        "execution_binding": {
            "kind": "python_experiment",
            "purpose": "retrieval_benchmark",
            "operation_id": "lost",
            "workspace_id": host.default_workspace.workspace_id,
        },
    }
    host.store.admit_run(
        "lost",
        owner,
        [
            ("run.admitted", intent, "system", "runtime"),
            ("run.started", intent, "system", "runtime"),
        ],
    )
    await host.close()
    restored = RuntimeHost(original_settings, ScriptedProvider([]), enable_mcp=False)
    try:
        assert restored._is_python_experiment_run("lost")
        assert restored.store.get_run("lost")["status"] == "interrupted"
        assert not any(
            e["type"] == "workspace.checkpoint" for e in restored.store.run_events("lost")
        )
        assert restored.provider.requests == []
        with pytest.raises(ValueError, match="Control|control|ordinary"):
            await restored.continue_session(owner)
    finally:
        await restored.close()


async def test_control_purpose_run_identity_and_preexisting_session_cannot_be_repurposed(tmp_path):
    host = host_for(tmp_path)

    async def execute(run):
        pass

    try:
        for kind, run in (("engineering", "valid"), ("retrieval_benchmark", "../bad")):
            with pytest.raises(ValueError):
                await host.run_control_operation(
                    kind=kind,
                    run_id=run,
                    project_id=host.default_workspace.project_id,
                    accepted={},
                    execute=execute,
                )
        owner = "python_retrieval_benchmark_" + host.default_workspace.project_id
        host.create_session(owner)
        with pytest.raises(ValueError, match="identity conflict"):
            await host.run_control_operation(
                kind="retrieval_benchmark",
                run_id="valid",
                project_id=host.default_workspace.project_id,
                accepted={},
                execute=execute,
            )
        assert not host.is_control_session(owner)
        assert host.store.get_run_identity("valid") is None
    finally:
        await host.close()


async def test_experiments_and_ordinary_runs_share_two_global_model_slots(tmp_path):
    host = host_for(tmp_path)
    entered, release = asyncio.Queue(), asyncio.Event()

    class Provider:
        async def complete(self, request):
            await entered.put(request)
            await release.wait()
            return ModelResponse("rewritten query", usage={"input_tokens": 2, "output_tokens": 1})

    host.provider = Provider()
    tasks = []
    try:
        for number in range(2):
            repo = make_repo(tmp_path / f"project-{number}")
            workspace = host.resolve_or_register_workspace(repo)

            async def execute(run):
                from eventide.models import ModelRequest

                await host._complete_with_retry(
                    ModelRequest("rewrite", [], [], "scripted", 32),
                    run,
                    None,
                    kind="query_rewrite",
                    max_attempts=1,
                )

            tasks.append(
                asyncio.create_task(
                    host.run_control_operation(
                        kind="retrieval_benchmark",
                        project_id=workspace.project_id,
                        run_id=f"experiment-{number}",
                        accepted={},
                        execute=execute,
                    )
                )
            )
        await entered.get()
        await entered.get()
        tasks.append(asyncio.create_task(host.run(RunRequest("ordinary agent", mode="plan"))))
        await spin_until(lambda: host.dispatch.scheduler.counts()["waiting_models"] == 1)
        assert host.dispatch.scheduler.counts()["active_models"] == 2
        assert len(host.store.facts_by_type(("model.attempt_started",))) == 2
        waiting = next(run for run in host._run_tasks if run.startswith("run_"))
        await host.cancel_run(waiting)
        assert not host.store.usage_events(run_id=waiting)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(result, asyncio.CancelledError) for result in results) == 1
        assert all(
            call["kind"] == "query_rewrite"
            for run in ("experiment-0", "experiment-1")
            for call in build_usage_ledger(host.store.usage_events(run_id=run))["calls"]
        )
    finally:
        release.set()
        await host.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"confirmed": False},
        {"split": "bad"},
        {"repeats": True},
        {"profile": replace(_profile(16), provider="openai_compatible")},
    ],
)
async def test_experiment_requires_confirmation_and_local_frozen_inputs(tmp_path, arguments):
    host = host_for(tmp_path)
    params = {
        "project_id": host.default_workspace.project_id,
        "embedder": HashEmbedder(16),
        "profile": _profile(16),
        "confirmed": True,
        "repeats": 1,
        **arguments,
    }
    try:
        with pytest.raises(ValueError):
            await run_query_rewrite_benchmark(host, **params)
        assert host.provider.requests == []
        assert host.store.facts_by_type(("run.admitted",)) == []
    finally:
        await host.close()


async def test_dispatch_failure_releases_control_ownership(
    tmp_path,
    monkeypatch,
):
    host = host_for(tmp_path)

    async def execute(run):
        raise AssertionError("Failed dispatch cannot execute")

    def failed(*args):
        raise RuntimeError("cannot schedule")

    monkeypatch.setattr(host.dispatch, "start", failed)
    try:
        with pytest.raises(RuntimeError, match="cannot schedule"):
            await host.run_control_operation(
                kind="retrieval_benchmark",
                project_id=host.default_workspace.project_id,
                run_id="failed_start",
                accepted={"case": {"query": "frozen"}},
                execute=execute,
            )
        assert host.store.get_run("failed_start")["status"] == "failed"
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
        assert host._admission_done == {} and host._admissions == {}
        assert host.provider.requests == []
    finally:
        await host.close()


async def test_host_shutdown_preserves_cancelled_query_evidence_before_store_close(tmp_path):
    host = host_for(tmp_path)
    entered = asyncio.Event()

    class Provider:
        async def complete(self, request):
            entered.set()
            await asyncio.Event().wait()

    host.provider = Provider()
    task = asyncio.create_task(benchmark(host, output_path=tmp_path / "shutdown.json"))
    await entered.wait()
    await host.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((tmp_path / "shutdown.json").read_text())
    assert report["status"] == "aborted"
    assert len(report["remaining"]) == 19
    assert report["evidence"]["usage"]["unknown_attempts"] == 1
    assert len(report["evidence"]["run_ids"]) == 1


async def test_original_retrieval_failure_is_attempted_failed_query_not_unattempted(
    tmp_path,
    monkeypatch,
):
    host = host_for(tmp_path)

    def failed(*args, **kwargs):
        raise RuntimeError("private local retrieval failure")

    monkeypatch.setattr("eventide.rag.benchmark.retrieve", failed)
    try:
        report = await benchmark(host)
        assert report["status"] == "aborted"
        for comparison in report["comparisons"].values():
            assert comparison["failed"] == comparison["total"] == 1
            assert comparison["unattempted"] == 19
            assert comparison["results"][0]["ranking"]["results"] == ()
        assert host.provider.requests == []
        assert "private local" not in json.dumps(report)
    finally:
        await host.close()


async def test_cancel_queued_benchmark_exports_all_queries_unattempted(tmp_path):
    host = host_for(tmp_path)
    try:
        for _ in range(4):
            await host.admit_run(
                RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
            )
        output = tmp_path / "queued.json"
        task = asyncio.create_task(benchmark(host, output_path=output))
        await spin_until(lambda: host.dispatch.snapshot()["queued"])
        queued = host.dispatch.snapshot()["queued"][0]["run_id"]
        await host.cancel_run(queued)
        with pytest.raises(asyncio.CancelledError):
            await task
        report = json.loads(output.read_text())
        assert report["status"] == "aborted"
        assert len(report["remaining"]) == 20
        assert report["evidence"]["run_ids"] == [queued]
        assert report["evidence"]["usage"]["attempt_count"] == 0
        assert host.provider.requests == []
        for comparison in report["comparisons"].values():
            assert comparison["total"] == comparison["failed"] == 0
            assert comparison["recall_at_5"] is None
            assert comparison["unattempted"] == 20
        assert not report["promotion"]["promote"]
    finally:
        await host.close()
