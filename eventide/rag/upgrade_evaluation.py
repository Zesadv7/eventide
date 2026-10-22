"""Host-governed remote retrieval experiments, with reconstructable partial results."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from eventide.budget_prices import summarize_cost_events
from eventide.embedding_runtime import embed_remote
from eventide.eval_evidence import atomic_json, runtime_revision
from eventide.rag.benchmark import DATASET_SHA256, load_dataset
from eventide.rag.embedding import HashEmbedder, provider_from_settings
from eventide.rag.evaluation import _grade_hits, _init_git_workspace, _profile, _summarize
from eventide.rag.index import IndexStore
from eventide.rag.search import RetrievalOutcome, retrieval_from_vector
from eventide.usage import build_usage_ledger
from eventide.utils import owned_local_work

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


def write_corpus(root: Path, documents: dict[str, str]) -> None:
    """Only validated frozen manifests enter a new experiment-owned checkout."""
    root.mkdir(parents=True, exist_ok=False)
    for name, body in documents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _init_git_workspace(root)


def percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else None


def experiment_events(host: RuntimeHost, run_ids: list[str]) -> list[dict[str, Any]]:
    events = [event for run_id in run_ids for event in host.store.run_events(run_id)]
    attempts = {event["payload"]["attempt_id"] for event in events
                if event["type"] in {"model.attempt_started", "model.attempt_finished"}}
    return events + [event for event in host.store.facts_by_type(("cost.estimated",))
                     if event["payload"].get("attempt_id") in attempts]


def experiment_evidence(host: RuntimeHost, run_ids: list[str]) -> dict[str, Any]:
    events = experiment_events(host, run_ids)
    return {"source": "eventide.event_log", "database": str(host.store.path),
            "run_ids": run_ids, "usage": build_usage_ledger(events),
            "costs": summarize_cost_events([e for e in events if e["type"] in {
                "budget.reserved", "cost.estimated",
            }])}


def remote_comparison_report(host: RuntimeHost, run_id: str) -> dict[str, Any]:
    """JSON exports can be recreated from the accepted plan and canonical query facts."""
    events = host.store.run_events(run_id)
    accepted = next(e["payload"]["configuration"] for e in events
                    if e["type"] == "control.operation_accepted")
    rows = [e["payload"] for e in events if e["type"] == "retrieval.upgrade_query"]
    terminal = next((e["type"] for e in reversed(events)
                     if e["type"] in {"run.completed", "run.failed", "run.interrupted"}), None)
    comparisons = {}
    for arm in ("sparse", "hash_hybrid", "remote_hybrid"):
        samples = [sample for row in rows for sample in row.get("arms", {}).get(arm, [])]
        queries = [row["arms"][arm][0] for row in rows if arm in row.get("arms", {})]
        complete_times = [row["complete_query_ms"][arm] for row in rows
                          if row["status"] == "completed"]
        summary = _summarize(queries) if queries else {
            "passed": 0, "total": 0, "recall_at_5": None, "mrr": None,
            "latency_p50_ms": None, "latency_p95_ms": None, "results": [],
        }
        comparisons[arm] = {**summary, "local_timing_repeats": accepted["local_repeats"],
                            "local_timing_samples": len(samples), "queries": len(queries),
                            "latency_p50_ms": percentile(
                                [s["duration_ms"] for s in samples], .5),
                            "latency_p95_ms": percentile(
                                [s["duration_ms"] for s in samples], .95),
                            "complete_query_p50_ms": percentile(complete_times, .5),
                            "complete_query_p95_ms": percentile(complete_times, .95),
                            "no_results": sum(not r["arms"][arm][0]["hits"] for r in rows
                                              if arm in r.get("arms", {})),
                            "no_result_ratio": sum(not q["hits"] for q in queries) / len(queries)
                            if queries else None}
    attempted = {row["id"] for row in rows}
    embedding_times = [row["embedding_duration_ms"] for row in rows
                       if row.get("embedding_duration_ms") is not None]
    complete_times = [row["combined_query_duration_ms"] for row in rows
                      if row["status"] == "completed"]
    return {
        "schema_version": 1, **accepted,
        "status": "completed" if terminal == "run.completed" else
                  "aborted" if terminal is not None else "running",
        "build_status": "completed" if any(e["type"] == "retrieval.upgrade_index" for e in events)
        else "failed_or_interrupted" if terminal is not None else "running",
        "comparisons": comparisons, "results": rows,
        "attempted": len(rows), "failed": sum(r["status"] == "failed" for r in rows),
        "query_failure_ratio": sum(r["status"] == "failed" for r in rows) / len(rows)
        if rows else None,
        "interrupted": sum(r["status"] == "interrupted" for r in rows),
        "remaining": [case for case in accepted["cases"] if case["id"] not in attempted],
        "embedding_latency_p50_ms": percentile(embedding_times, .5),
        "embedding_latency_p95_ms": percentile(embedding_times, .95),
        "complete_query_latency_p50_ms": percentile(complete_times, .5),
        "complete_query_latency_p95_ms": percentile(complete_times, .95),
        "index": next((e["payload"] for e in events
                       if e["type"] == "retrieval.upgrade_index"), None),
        "evidence": experiment_evidence(host, [run_id]),
        "promotion": {"promote": False, "reason": "downstream_agent_evidence_required"},
        "limitations": ["Local timing includes current-file freshness checks",
                        "Local repeats do not request additional remote vectors",
                        "Build usage is included; pricing may be unknown",
                        "Retrieval quality is not engineering repair success"],
    }


async def run_remote_comparison(
    host: RuntimeHost, *, project_id: str, output_directory: Path,
    split: Literal["development", "holdout"] = "development", local_repeats: int = 3,
    confirmed: bool = False, stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """One paid vector per frozen query; all requests keep the supplied Host's ownership."""
    from eventide.retrieval_runtime import managed_index_dir, prepare_index

    if confirmed is not True:
        raise ValueError("Remote retrieval experiment requires explicit confirmation")
    if split not in {"development", "holdout"} or type(local_repeats) is not int or not (
        1 <= local_repeats <= 3
    ):
        raise ValueError("Invalid retrieval experiment split or local repeat count")
    if host.settings.embedding_provider != "openai_compatible":
        raise ValueError("Remote comparison requires a configured remote embedding model")
    if host.settings.retrieval_query_rewrite or host.settings.reranker_model_path is not None:
        raise ValueError("This comparison excludes query rewriting and reranking")
    await host.initialize()
    host.store.get_project(project_id)
    dataset = load_dataset()
    cases = [case for case in dataset.cases if case.split == split]
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=False)
    remote_root, hash_root = output_directory / "remote", output_directory / "hash"
    await owned_local_work(write_corpus, remote_root, dataset.documents)
    await owned_local_work(write_corpus, hash_root, dataset.documents)
    remote_workspace = host.resolve_or_register_workspace(remote_root)
    hash_workspace = host.resolve_or_register_workspace(hash_root)
    remote_dir = managed_index_dir(host, remote_workspace)
    hash_dir = managed_index_dir(host, hash_workspace)
    _, remote_profile = provider_from_settings(host.settings)
    hash_embedder, hash_profile = HashEmbedder(256), _profile(256)
    run_id = f"retrieval_{uuid.uuid4().hex}"
    accepted = {
        "experiment": "retrieval-upgrade-v1", "split": split,
        "dataset_sha256": DATASET_SHA256, "dataset_version": dataset.version,
        "corpus_sha256": hashlib.sha256(json.dumps(
            dataset.documents, sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest(), "cases": [c.model_dump() for c in cases],
        "runtime": runtime_revision(), "local_repeats": local_repeats,
        "model_configuration": host._run_intent(None),
        "remote_workspace_id": remote_workspace.workspace_id,
        "hash_workspace_id": hash_workspace.workspace_id,
    }

    async def execute(owned: str) -> None:
        build_start = time.perf_counter()
        await prepare_index(host, owned, remote_workspace, settings=host.settings)
        if not build_usage_ledger(host.model_usage_events(owned))["complete"]:
            raise RuntimeError("Index preparation usage is unknown; batch stopped")
        await owned_local_work(IndexStore(hash_root, index_dir=hash_dir).build,
                               embedder=hash_embedder, profile=hash_profile)
        resolved = IndexStore(remote_root, index_dir=remote_dir).embedding_profile()
        host.store.append_event(owned, "retrieval.upgrade_index", {
            "embedding_fingerprint": resolved.fingerprint(),
            "hash_fingerprint": hash_profile.fingerprint(),
            "build_duration_ms": (time.perf_counter() - build_start) * 1000,
        })
        for case in cases:
            if stop_requested is not None and stop_requested():
                raise asyncio.CancelledError
            row: dict[str, Any] = {"id": case.id, "split": case.split, "arms": {},
                                   "status": "running", "embedding_duration_ms": None}
            started = time.perf_counter()
            try:
                vector_started = time.perf_counter()
                vector = (await embed_remote(host, owned, [case.query],
                                             settings=host.settings))[0]
                row["embedding_duration_ms"] = (time.perf_counter() - vector_started) * 1000
                if not build_usage_ledger(host.model_usage_events(owned))["complete"]:
                    raise RuntimeError("Remote embedding usage is unknown; batch stopped")
                hash_started = time.perf_counter()
                local_vector = hash_embedder.embed([case.query])[0]
                row["hash_embedding_duration_ms"] = (time.perf_counter() - hash_started) * 1000
                for arm in ("sparse", "hash_hybrid", "remote_hybrid"):
                    samples = []
                    for repetition in range(local_repeats):
                        local_started = time.perf_counter()
                        outcome: RetrievalOutcome = await owned_local_work(
                            retrieval_from_vector,
                            remote_root if arm == "remote_hybrid" else hash_root, case.query,
                            vector=vector if arm == "remote_hybrid" else
                            local_vector if arm == "hash_hybrid" else None,
                            profile=resolved if arm == "remote_hybrid" else hash_profile,
                            index_dir=remote_dir if arm == "remote_hybrid" else hash_dir,
                        )
                        elapsed = (time.perf_counter() - local_started) * 1000
                        samples.append({**_grade_hits(case.model_dump(), [
                            hit for hit in outcome.results if hit.freshness == "fresh"
                        ]),
                                        "duration_ms": elapsed, "repetition": repetition,
                                        "ranking": asdict(outcome)})
                    row["arms"][arm] = samples
                row["complete_query_ms"] = {
                    arm: row["arms"][arm][0]["duration_ms"] + (
                        row["embedding_duration_ms"] if arm == "remote_hybrid" else
                        row["hash_embedding_duration_ms"] if arm == "hash_hybrid" else 0.0
                    ) for arm in row["arms"]
                }
                row["status"] = "completed"
            except BaseException as exc:
                row.update(status="interrupted" if isinstance(exc, asyncio.CancelledError)
                           else "failed", error_type=type(exc).__name__)
                raise
            finally:
                row["benchmark_duration_ms"] = (time.perf_counter() - started) * 1000
                row["combined_query_duration_ms"] = (
                    row["embedding_duration_ms"] + row["arms"]["remote_hybrid"][0]["duration_ms"]
                    if row["status"] == "completed" else None
                )
                host.store.append_event(owned, "retrieval.upgrade_query", row)
                atomic_json(output_directory / "report.json", remote_comparison_report(host, owned))

    try:
        await host.run_control_operation(kind="retrieval_benchmark", project_id=project_id,
                                         run_id=run_id, accepted=accepted, execute=execute)
    finally:
        # Failure and cancellation still export their canonical terminal and unknown attempts.
        if host.store.get_run_identity(run_id) is not None:
            atomic_json(output_directory / "report.json", remote_comparison_report(host, run_id))
    return remote_comparison_report(host, run_id)
