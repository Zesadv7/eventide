"""Real Host control and Agent loops with deterministic external API substitutes."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventide import embedding_runtime
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.rag.agent_evaluation import (
    agent_comparison_report,
    attempt_plan,
    load_agent_dataset,
    run_agent_comparison,
)
from eventide.rag.embedding import HashEmbedder
from eventide.rag.upgrade_evaluation import remote_comparison_report, run_remote_comparison
from tests.test_embedding_runtime import Client
from tests.test_host import make_repo
from tests.test_retrieval_agent_evaluation import exact_answer
from tests.test_retrieval_agent_v2 import reviewed_answer
from tests.test_runtime import settings_for


@pytest.fixture
async def host(tmp_path):
    source = make_repo(tmp_path / "source")
    settings = replace(settings_for(source), state_dir=tmp_path / "state",
                       embedding_provider="openai_compatible", embedding_model="fixture",
                       embedding_api_key="fixture-secret", embedding_base_url="https://invalid/v1",
                       max_steps=12, run_token_limit=20_000)
    runtime = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        yield runtime
    finally:
        await runtime.close()


@pytest.fixture
def embeddings(monkeypatch):
    clients = []

    class Embeddings(Client):
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            vectors = HashEmbedder(16).embed(kwargs["input"])
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=vector)
                                         for i, vector in enumerate(vectors)],
                                   usage=SimpleNamespace(prompt_tokens=len(vectors) * 3))

    def factory(settings):
        client = Embeddings()
        clients.append(client)
        return client

    monkeypatch.setattr(embedding_runtime, "_client", factory)
    return clients


async def test_remote_comparison_reuses_vectors_and_rebuilds_from_canonical_events(
    host, embeddings, tmp_path,
):
    report = await run_remote_comparison(
        host, project_id=host.default_workspace.project_id,
        output_directory=tmp_path / "comparison", confirmed=True,
    )
    assert report["status"] == "completed" and report["attempted"] == 20
    assert not report["remaining"] and report["failed"] == 0
    requests = [request for client in embeddings for request in client.requests]
    assert len(requests) == 21  # One corpus batch and precisely one vector per query.
    assert all(client.closed for client in embeddings)
    assert all(len(request["input"]) == 1 for request in requests[1:])
    for comparison in report["comparisons"].values():
        assert comparison["total"] == comparison["queries"] == 20
        assert comparison["local_timing_samples"] == 60
        assert comparison["local_timing_repeats"] == 3
    for row in report["results"]:
        samples = row["arms"]["remote_hybrid"]
        assert row["combined_query_duration_ms"] == (
            row["embedding_duration_ms"] + samples[0]["duration_ms"]
        )
        assert samples[0]["ranking"]["index_generation"]
        assert all(hit["freshness"] == "fresh" for hit in samples[0]["ranking"]["results"])
    assert report["evidence"]["usage"]["attempt_count"] == 21
    assert report["evidence"]["usage"]["complete"]
    run_id = report["evidence"]["run_ids"][0]
    assert remote_comparison_report(host, run_id) == report
    assert not (tmp_path / "comparison/remote/.eventide").exists()
    assert not (tmp_path / "comparison/hash/.eventide").exists()


async def test_remote_stop_preserves_query_and_never_calls_remaining_vectors(
    host, embeddings, tmp_path,
):
    def stop():
        return len(embeddings) == 2

    with pytest.raises(asyncio.CancelledError):
        await run_remote_comparison(host, project_id=host.default_workspace.project_id,
                                    output_directory=tmp_path / "cancelled", confirmed=True,
                                    stop_requested=stop)
    report = json.loads((tmp_path / "cancelled/report.json").read_text())
    assert report["status"] == "aborted" and report["attempted"] == 1
    assert len(report["remaining"]) == 19 and len(embeddings) == 2
    assert report["evidence"]["usage"]["attempt_count"] == 2


async def test_remote_api_failure_retains_unknown_and_stops_before_queries(
    host, monkeypatch, tmp_path,
):
    clients = []

    class Failed(Client):
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            raise ConnectionError("fixture-secret")

    def factory(settings):
        client = Failed()
        clients.append(client)
        return client

    monkeypatch.setattr(embedding_runtime, "_client", factory)
    with pytest.raises(embedding_runtime.EmbeddingRequestError):
        await run_remote_comparison(host, project_id=host.default_workspace.project_id,
                                    output_directory=tmp_path / "failed", confirmed=True)
    report = json.loads((tmp_path / "failed/report.json").read_text())
    assert report["status"] == "aborted" and len(report["remaining"]) == 20
    assert report["evidence"]["usage"]["unknown_attempts"] == 1
    assert len(clients) == 1 and "fixture-secret" not in json.dumps(report)


@pytest.mark.parametrize("split,count", [("development", 8), ("holdout", 16)])
@pytest.mark.parametrize("provider", ["openai_compatible", "local_semantic"])
async def test_actual_agent_pairs_use_current_files_after_semantic_search(
    host, embeddings, tmp_path, split, count, provider, monkeypatch,
):
    if provider == "local_semantic":
        from eventide.rag.embedding import provider_from_settings

        local = HashEmbedder(16)
        _, profile = provider_from_settings(host.settings)
        profile = replace(profile, provider="local_semantic", base_url="local", dimension=16)
        host.settings = replace(host.settings, embedding_provider="local_semantic")
        monkeypatch.setattr("eventide.retrieval_runtime.provider_from_settings",
                            lambda settings: (local, profile))
    dataset = load_agent_dataset()
    cases = {case.id: case for case in dataset.cases}
    script = []
    usage = {"input_tokens": 3, "output_tokens": 1}
    for slot in attempt_plan(dataset, split):
        case = cases[slot["case_id"]]
        if slot["arm"] == "candidate":
            script.append({"tool_calls": [{"id": "semantic", "name": "search_knowledge",
                                           "arguments": {"query": case.question}}], "usage": usage})
        script.append({"tool_calls": [{"id": f"read-{number}", "name": "read_file",
                                       "arguments": {"path": name}}
                                      for number, name in enumerate(sorted(
                                          {f.evidence.path for f in case.facts}))],
                       "usage": usage})
        script.append({"text": json.dumps(exact_answer(case)), "usage": usage})
    provider = ScriptedProvider(script)
    host.provider = provider
    report = await run_agent_comparison(host, output_directory=tmp_path / split,
                                        split=split, confirmed=True)
    assert report["status"] == "completed", report
    assert sum(arm["attempted"] for arm in report["arms"].values()) == count
    for arm in report["arms"].values():
        assert arm["passed"] == count // 2 and arm["valid_citation_ratio"] == 1
        assert all(row["grade"]["source_unchanged"] for row in arm["results"])
        assert arm["cross_workspace_results"] == arm["stale_hits_marked_fresh"] == 0
        assert arm["stale_or_foreign_quotes"] == 0
    assert not report["remaining"] and not report["missing_results"]
    assert report["evidence"]["usage"]["complete"]
    if provider == "local_semantic":
        assert not embeddings
        assert all(row["preparation_usage"]["attempt_count"] == 0
                   for row in report["arms"]["candidate"]["results"])
    for row in report["arms"]["baseline"]["results"]:
        assert not any(call["name"] == "search_knowledge" for call in row["tool_calls"])
        assert row["preparation_usage"]["attempt_count"] == 0
    candidates = report["arms"]["candidate"]["results"]
    if split == "development":
        stale = next(row for row in candidates if row["case_id"] == "fresh-retention")
        retrieval = next(call for call in stale["tool_calls"] if call["name"] == "search_knowledge")
        assert "changed" in json.dumps(retrieval["metadata"]) or "stale" in retrieval["content"]
        assert "retention_days = 3" not in retrieval["content"]
    else:
        copy = next(row for row in candidates if row["case_id"] == "copy-dispatch")
        assert "fifo" not in json.dumps(copy["tool_calls"])
    controls = [run for run in report["evidence"]["run_ids"] if run.startswith("investigation_")]
    assert agent_comparison_report(host, controls) == report


@pytest.mark.parametrize("dataset_version", ["v1", "v2"])
async def test_agent_missing_usage_aborts_without_replacement_attempt(
    host, embeddings, tmp_path, dataset_version,
):
    case = load_agent_dataset(dataset_version).cases[0]
    host.provider = ScriptedProvider([{"text": json.dumps(exact_answer(case))}])
    report = await run_agent_comparison(
        host, output_directory=tmp_path / "unknown", confirmed=True,
        dataset_version=dataset_version,
    )
    assert report["status"] == "aborted" and len(report["remaining"]) == 7
    assert report["arms"]["baseline"]["results"][0]["status"] == "usage_unknown"
    assert report["evidence"]["usage"]["unknown_attempts"] == 1
    assert not embeddings
    arm = report["arms"]["baseline"]
    assert arm["attempted"] == arm["failed"] == 1 and arm["passed"] == 0
    assert arm["results"][0]["grade"]["passed"]  # Preserve the valid but incomplete attempt.
    if dataset_version == "v2":
        quality = arm["quality"]
        assert quality["graded_attempts"] == 0 and quality["ungraded_attempts"] == 1
        assert quality["schema_valid_attempts"] == quality["authentic_citations"] == 0
        assert quality["fact_accuracy"] is quality["citation_authenticity_ratio"] is None
    else:
        assert "quality" not in arm


async def test_quality_failure_is_retained_without_retry_and_rag_is_optional(
    host, embeddings, tmp_path,
):
    dataset = load_agent_dataset()
    cases = {case.id: case for case in dataset.cases}
    script = []
    for number, slot in enumerate(attempt_plan(dataset, "development")):
        case = cases[slot["case_id"]]
        answer = exact_answer(case)
        if number == 0:
            answer["facts"][case.facts[0].key] = "wrong"
        script.append({"text": json.dumps(answer),
                       "usage": {"input_tokens": 3, "output_tokens": 1}})
    host.provider = ScriptedProvider(script)
    report = await run_agent_comparison(host, output_directory=tmp_path / "quality", confirmed=True)
    assert report["status"] == "completed"
    assert report["arms"]["baseline"]["passed"] == 3
    assert report["arms"]["baseline"]["failed"] == 1
    assert report["arms"]["candidate"]["passed"] == 4
    assert len(host.provider.requests) == 8
    assert all(row["tool_call_count"] == 0 for arm in report["arms"].values()
               for row in arm["results"])


@pytest.mark.parametrize("split", ["development", "holdout"])
async def test_v2_pairs_report_each_quality_layer_from_canonical_host_events(
    host, embeddings, tmp_path, split,
):
    dataset = load_agent_dataset("v2")
    cases = {case.id: case for case in dataset.cases}
    script = []
    usage = {"input_tokens": 3, "output_tokens": 1}
    for slot in attempt_plan(dataset, split):
        case = cases[slot["case_id"]]
        answer = reviewed_answer(case)
        if slot["arm"] == "candidate":
            script.append({"tool_calls": [{"id": "semantic", "name": "search_knowledge",
                                           "arguments": {"query": case.question}}], "usage": usage})
        script.append({"tool_calls": [
            {"id": f"read-{number}", "name": "read_file", "arguments": {"path": path}}
            for number, path in enumerate(sorted({c["path"] for c in answer["citations"]}))
        ], "usage": usage})
        script.append({"text": json.dumps(answer), "usage": usage})
    host.provider = ScriptedProvider(script)
    report = await run_agent_comparison(
        host, output_directory=tmp_path / f"v2-{split}", split=split,
        confirmed=True, dataset_version="v2",
    )
    assert report["schema_version"] == 2 and report["version"] == "retrieval-agent-v2"
    assert report["code_structure"] is False and report["status"] == "completed"
    assert not report["remaining"] and not report["missing_results"]
    assert report["evidence"]["usage"]["complete"]
    for name, arm in report["arms"].items():
        assert arm["expected"] == arm["attempted"] == arm["passed"] == 4
        assert arm["failed"] == arm["cross_workspace_results"] == 0
        assert arm["stale_hits_marked_fresh"] == arm["stale_or_foreign_quotes"] == 0
        assert arm["valid_citation_ratio"] == 1
        quality = arm["quality"]
        assert quality["graded_attempts"] == quality["schema_valid_attempts"] == 4
        assert quality["ungraded_attempts"] == 0
        assert quality["field_keys_correct_attempts"] == 4
        assert (quality["expected_facts"] == quality["present_facts"]
                == quality["correct_facts"] == 8)
        assert quality["field_coverage"] == quality["fact_accuracy"] == 1
        assert quality["citation_authenticity_ratio"] == 1
        assert quality["semantic_search_calls"] == (4 if name == "candidate" else 0)
        assert quality["semantic_search_runs"] == (4 if name == "candidate" else 0)
    controls = [run for run in report["evidence"]["run_ids"] if run.startswith("investigation_")]
    assert agent_comparison_report(host, controls) == report


async def test_v2_quality_failures_stay_distinct_and_structural_option_is_forwarded(
    host, embeddings, tmp_path, monkeypatch,
):
    from eventide import models, retrieval_runtime

    original_prepare = retrieval_runtime.prepare_index
    original_request = models.RunRequest
    prepared = []
    requested = []

    async def prepare(*args, code_structure=False, **kwargs):
        prepared.append(code_structure)
        return await original_prepare(*args, code_structure=code_structure, **kwargs)

    def request(*args, **kwargs):
        requested.append(kwargs.get("code_structure", False))
        return original_request(*args, **kwargs)

    # Exercise actual Host construction and index metadata as well as forwarding.
    monkeypatch.setattr(retrieval_runtime, "prepare_index", prepare)
    monkeypatch.setattr(models, "RunRequest", request)
    dataset = load_agent_dataset("v2")
    cases = {case.id: case for case in dataset.cases}
    plan = attempt_plan(dataset, "development")
    script = []
    for number, slot in enumerate(plan):
        answer = reviewed_answer(cases[slot["case_id"]])
        if number == 0:
            answer["facts"]["requested_key"] = answer["facts"].pop("storage_method")
        elif number == 1:
            answer["facts"]["storage_method"] = "append"
        script.append({"text": json.dumps(answer),
                       "usage": {"input_tokens": 3, "output_tokens": 1}})
    host.provider = ScriptedProvider(script)
    report = await run_agent_comparison(
        host, output_directory=tmp_path / "v2-forwarding", dataset_version="v2",
        code_structure=True, confirmed=True,
    )
    assert report["status"] == "completed" and report["code_structure"] is True
    assert prepared == [True] * 4
    assert requested == [slot["arm"] == "candidate" for slot in plan]
    assert len(host.provider.requests) == 8
    from eventide.rag.index import IndexStore

    for row in report["arms"]["candidate"]["results"]:
        workspace = host.store.get_workspace(row["workspace_id"])
        index = IndexStore(
            Path(workspace.path),
            index_dir=retrieval_runtime.managed_index_dir(host, workspace),
        )
        with index.snapshot() as snapshot, snapshot._reader() as connection:
            meta = snapshot._meta(connection)
        assert json.loads(meta["chunking_profile"])["code_structure"] is True
        assert json.loads(meta["source_selection_profile"])["source_scope"] == "code"
    baseline = report["arms"]["baseline"]
    candidate = report["arms"]["candidate"]
    assert baseline["passed"] == candidate["passed"] == 3
    assert baseline["quality"]["field_keys_correct_attempts"] == 3
    assert candidate["quality"]["field_keys_correct_attempts"] == 4
    for arm in (baseline, candidate):
        assert arm["quality"]["schema_valid_attempts"] == 4
        assert arm["quality"]["correct_facts"] == 7
        assert arm["quality"]["authentic_citations"] == 8
        assert arm["quality"]["semantic_search_calls"] == 0  # Never a passing prerequisite.
        assert arm["failed"] == 1
    assert "fact_keys_mismatch" in baseline["results"][0]["grade"]["failure_reasons"]
    assert candidate["results"][0]["grade"]["failure_reasons"] == ["fact_values_mismatch"]


async def test_cancelled_agent_keeps_unknown_attempt_and_unattempted_slots(host, tmp_path):
    started = asyncio.Event()

    class Waiting(ScriptedProvider):
        async def complete(self, request):
            self.requests.append(request)
            started.set()
            await asyncio.Event().wait()

    host.provider = Waiting([])
    task = asyncio.create_task(run_agent_comparison(
        host, output_directory=tmp_path / "cancel-agent", confirmed=True,
    ))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((tmp_path / "cancel-agent/report.json").read_text())
    assert report["status"] == "aborted" and len(report["remaining"]) == 7
    assert report["arms"]["baseline"]["results"][0]["status"] == "interrupted"
    assert report["evidence"]["usage"]["unknown_attempts"] == 1
    assert len(host.provider.requests) == 1


async def test_explicit_confirmation_prevents_any_request_or_artifact(host, embeddings, tmp_path):
    with pytest.raises(ValueError, match="confirmation"):
        await run_remote_comparison(host, project_id=host.default_workspace.project_id,
                                    output_directory=tmp_path / "remote")
    with pytest.raises(ValueError, match="confirmation"):
        await run_agent_comparison(host, output_directory=tmp_path / "agent")
    assert not embeddings and not (tmp_path / "remote").exists()
    assert not (tmp_path / "agent").exists()


async def test_v2_quality_excludes_unknown_attempt_but_retains_completed_schema_failure(
    host, embeddings, tmp_path,
):
    dataset = load_agent_dataset("v2")
    notice, catalog = dataset.cases[:2]
    usage = {"input_tokens": 10, "output_tokens": 5}
    host.provider = ScriptedProvider([
        {"text": "not a JSON answer", "usage": usage},
        {"text": json.dumps(reviewed_answer(notice)), "usage": usage},
        {"text": json.dumps(reviewed_answer(catalog))},
    ])
    report = await run_agent_comparison(
        host, output_directory=tmp_path / "mixed-quality",
        dataset_version="v2", confirmed=True,
    )
    assert report["status"] == "aborted" and len(report["remaining"]) == 5
    baseline = report["arms"]["baseline"]
    assert baseline["failed"] == baseline["quality"]["graded_attempts"] == 1
    assert baseline["quality"]["schema_valid_attempts"] == 0
    candidate = report["arms"]["candidate"]
    assert candidate["attempted"] == 2 and candidate["passed"] == candidate["failed"] == 1
    assert candidate["results"][1]["status"] == "usage_unknown"
    assert candidate["results"][1]["grade"]["passed"]
    quality = candidate["quality"]
    assert quality["graded_attempts"] == quality["ungraded_attempts"] == 1
    assert quality["schema_valid_attempts"] == 1
    assert quality["expected_facts"] == quality["correct_facts"] == 2
    assert quality["authentic_citations"] == 2 and quality["citation_authenticity_ratio"] == 1
    assert report["evidence"]["usage"]["unknown_attempts"] == 1
    controls = [run for run in report["evidence"]["run_ids"] if run.startswith("investigation_")]
    assert agent_comparison_report(host, controls) == report
