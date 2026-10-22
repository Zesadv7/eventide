"""Real Host control and Agent loops with deterministic external API substitutes."""

import asyncio
import json
from dataclasses import replace
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
async def test_actual_agent_pairs_use_current_files_after_semantic_search(
    host, embeddings, tmp_path, split, count,
):
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


async def test_agent_missing_usage_aborts_without_replacement_attempt(host, embeddings, tmp_path):
    case = load_agent_dataset().cases[0]
    host.provider = ScriptedProvider([{"text": json.dumps(exact_answer(case))}])
    report = await run_agent_comparison(host, output_directory=tmp_path / "unknown", confirmed=True)
    assert report["status"] == "aborted" and len(report["remaining"]) == 7
    assert report["arms"]["baseline"]["results"][0]["status"] == "usage_unknown"
    assert report["evidence"]["usage"]["unknown_attempts"] == 1
    assert not embeddings


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
