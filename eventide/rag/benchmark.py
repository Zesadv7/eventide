"""Frozen retrieval ablations; local models are explicit and failures remain in reports."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventide.eval_evidence import runtime_revision
from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.evaluation import _grade_hits, _init_git_workspace, _summarize
from eventide.rag.index import IndexStore
from eventide.rag.search import (
    Reranker,
    RetrievalOutcome,
    dense_search,
    hybrid_search,
    retrieve,
    sparse_search,
)

if TYPE_CHECKING:
    from eventide.host import RuntimeHost

DATASET_SHA256 = "b484aa273ab80de18d945d75237a473a6a833e6cddfd9558306a8b6ad24136b3"


class QueryCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    split: Literal["development", "holdout"]
    expect_files: list[str] = Field(min_length=1)
    expect_contains: str | None = None


class RetrievalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    version: str = Field(min_length=1)
    documents: dict[str, str]
    cases: list[QueryCase]

    @model_validator(mode="after")
    def validate_dataset(self) -> RetrievalDataset:
        for name, text in self.documents.items():
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                    or path.as_posix() != name or any(p.startswith(".") for p in path.parts)
                    or not text):
                raise ValueError("Corpus document path/content invalid")
        if len(self.documents) < 10 or len(self.cases) < 40:
            raise ValueError("Frozen benchmark requires at least 10 documents and 40 queries")
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate query identity")
        if len({c.query.strip().casefold() for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate query text")
        for split in ("development", "holdout"):
            if sum(c.split == split for c in self.cases) < 20:
                raise ValueError("Each split requires at least 20 queries")
        for case in self.cases:
            if not case.query.strip() or len(set(case.expect_files)) != len(case.expect_files):
                raise ValueError("Empty query or duplicate relevance label")
            for name in case.expect_files:
                if name not in self.documents:
                    raise ValueError("Relevance label missing from corpus")
                if case.expect_contains and case.expect_contains not in self.documents[name]:
                    raise ValueError("Relevance passage missing from corpus")
        return self


def load_dataset(path: Path | None = None) -> RetrievalDataset:
    source = path or Path(__file__).parents[1] / "bundled_evals/retrieval-v1/dataset.json"
    body = source.read_bytes()
    if path is None and hashlib.sha256(body).hexdigest() != DATASET_SHA256:
        raise ValueError("Frozen retrieval dataset changed")
    return RetrievalDataset.model_validate_json(body)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def promotion_decision(
    baseline: dict[str, Any], candidate: dict[str, Any], *,
    downstream_baseline: int | None = None, downstream_candidate: int | None = None,
) -> dict[str, Any]:
    """Missing downstream evidence never promotes a retrieval-only result."""
    quality = (
        candidate["recall_at_5"] >= baseline["recall_at_5"]
        and candidate["mrr"] >= baseline["mrr"]
        and (candidate["recall_at_5"] > baseline["recall_at_5"]
             or candidate["mrr"] > baseline["mrr"])
    )
    latency = candidate["latency_p95_ms"] <= 2 * baseline["latency_p95_ms"]
    downstream = (downstream_baseline is not None and downstream_candidate is not None
                  and downstream_candidate >= downstream_baseline)
    return {"quality_improved": quality, "latency_within_limit": latency,
            "downstream_verified": downstream, "promote": quality and latency and downstream}


def run_benchmark(
    *, embedder: EmbeddingProvider, profile: EmbeddingProfile,
    reranker: Reranker | None = None, dataset: RetrievalDataset | None = None,
    split: Literal["development", "holdout", "all"] = "development",
    repeats: int = 3, reranker_identity: str | None = None,
    extra_methods: dict[str, Callable[[Path, str], RetrievalOutcome]] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """CPU/local ablations. Extra methods must explicitly own any external usage themselves."""
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats must be between 1 and 10")
    if split not in {"development", "holdout", "all"}:
        raise ValueError("Invalid split")
    if profile.provider == "openai_compatible":
        raise ValueError("Remote benchmark requires governed Run-owned index admission")
    frozen = dataset or load_dataset()
    cases = [c for c in frozen.cases if split == "all" or c.split == split]
    comparisons: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="eventide-retrieval-benchmark-") as directory:
        workspace = Path(directory)
        for name, body in frozen.documents.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        _init_git_workspace(workspace)
        build_started = time.perf_counter()
        stats = IndexStore(workspace).build(embedder=embedder, profile=profile)
        build_ms = (time.perf_counter() - build_started) * 1000
        resolved = IndexStore(workspace).embedding_profile()
        methods: dict[str, Callable[[Path, str], RetrievalOutcome]] = {
            "sparse": lambda w, q: RetrievalOutcome(tuple(sparse_search(w, q)), "sparse"),
            "dense": lambda w, q: RetrievalOutcome(tuple(dense_search(
                w, q, embedder=embedder, profile=profile)), "dense"),
            "hybrid": lambda w, q: RetrievalOutcome(tuple(hybrid_search(
                w, q, embedder=embedder, profile=profile)), "hybrid"),
        }
        if reranker is not None:
            if not reranker_identity:
                raise ValueError("Reranker identity required for reproducible comparison")
            methods["rerank"] = lambda w, q: retrieve(
                w, q, embedder=embedder, profile=profile, reranker=reranker,
            )
        for name, method in (extra_methods or {}).items():
            if name in methods:
                raise ValueError("Cannot override frozen comparison method")
            methods[name] = method
        for name, method in methods.items():
            results = []
            for case in cases:
                for repetition in range(repeats):
                    if stop_requested is not None and stop_requested():
                        break
                    started = time.perf_counter()
                    try:
                        outcome = method(workspace, case.query)
                        result = _grade_hits(case.model_dump(), list(outcome.results))
                        result.update(diagnostics=list(outcome.diagnostics),
                                      returned_characters=sum(len(h.chunk.text)
                                                              for h in outcome.results),
                                      status="completed", mode=outcome.mode)
                    except Exception as exc:
                        result = _grade_hits(case.model_dump(), [])
                        result.update(status="failed", error_type=type(exc).__name__,
                                      diagnostics=[], returned_characters=0)
                    result.update(duration_ms=(time.perf_counter() - started) * 1000,
                                  split=case.split, repetition=repetition)
                    results.append(result)
            summary = _summarize(results) if results else {
                "passed": 0, "total": 0, "recall_at_5": None, "mrr": None,
                "latency_p50_ms": None, "latency_p95_ms": None, "results": [],
            }
            comparisons[name] = {**summary,
                                 "expected": len(cases) * repeats,
                                 "unattempted": len(cases) * repeats - len(results),
                                 "failed": sum(r["status"] == "failed" for r in results),
                                 "degraded": sum(bool(r["diagnostics"]) for r in results)}
        return {
            "schema_version": 1, "dataset_version": frozen.version, "split": split,
            "status": "aborted" if any(c["unattempted"] for c in comparisons.values())
            else "completed",
            "runtime": runtime_revision(),
            "dataset_sha256": _digest(frozen.model_dump()),
            "corpus_sha256": _digest(frozen.documents), "repeats": repeats,
            "embedding_fingerprint": resolved.fingerprint(),
            "reranker_identity": reranker_identity, "build_duration_ms": build_ms,
            "chunking_fingerprint": stats.chunking_fingerprint,
            "comparisons": comparisons,
            "promotion": {name: promotion_decision(comparisons["sparse"], value)
                          if not value["unattempted"] and not comparisons["sparse"]["unattempted"]
                          else {"quality_improved": False, "latency_within_limit": False,
                                "downstream_verified": False, "promote": False,
                                "reason": "incomplete_comparison"}
                          for name, value in comparisons.items() if name != "sparse"},
            "limitations": ["local CPU only; no automatic downstream promotion",
                            "timings include inference and index/model integrity checks",
                            "returned characters are not provider input Tokens"],
        }


async def run_query_rewrite_benchmark(
    host: RuntimeHost, *, project_id: str, embedder: EmbeddingProvider,
    profile: EmbeddingProfile, confirmed: bool = False,
    split: Literal["development", "holdout", "all"] = "development", repeats: int = 3,
    reranker: Reranker | None = None, reranker_identity: str | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Controlled Python experiment, using one Host-owned root and claim per query.

    Confirmation permits real rewrite requests through the supplied Host. Local
    indexing/ranking never fabricates provider usage. JSON is a discardable export
    of that Host's accepted inputs, typed results and exclusive attempt evidence.
    """
    import asyncio
    import uuid
    from dataclasses import asdict

    from eventide.budget_prices import summarize_cost_events
    from eventide.budgets import BudgetExceeded
    from eventide.eval_evidence import atomic_json
    from eventide.retrieval_rewrite import query_rewrite_outcome
    from eventide.usage import build_usage_ledger
    from eventide.utils import owned_local_work

    if confirmed is not True:
        raise ValueError("Query rewrite benchmark requires explicit confirmation")
    if (type(repeats) is not int or not 1 <= repeats <= 10
            or split not in {"development", "holdout", "all"}):
        raise ValueError("Invalid retrieval experiment split or repeats")
    if profile.provider == "openai_compatible":
        raise ValueError("Retrieval experiments require a fixed local index model")
    if reranker is not None and not reranker_identity:
        raise ValueError("Reranker identity required for reproducible comparison")
    await host.initialize()
    host.store.get_project(project_id)
    frozen = load_dataset()
    selected = [(case, repetition) for case in frozen.cases
                if split == "all" or case.split == split for repetition in range(repeats)]
    configuration = host._run_intent(None)
    identity = {
        "experiment_id": f"retrieval_{uuid.uuid4().hex}", "split": split, "repeats": repeats,
        "dataset_version": frozen.version, "dataset_sha256": _digest(frozen.model_dump()),
        "corpus_sha256": _digest(frozen.documents), "runtime": runtime_revision(),
        "embedding_fingerprint": profile.fingerprint(), "reranker_identity": reranker_identity,
        "model_configuration": configuration,
    }
    originals: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    events: dict[str, list[dict[str, Any]]] = {}
    controls: list[str] = []
    report: dict[str, Any] = {
        "schema_version": 1, **identity, "status": "running",
        "limitations": [
            "ranking quality includes fallback candidates; execution failures are separate",
            "local retrieval time is separate from total rewrite and provider request time",
            "returned characters are not provider input Tokens",
            "downstream task quality and complete cost evidence are required before promotion",
        ],
    }

    def save(status: str) -> None:
        observed = [event for snapshot in events.values() for event in snapshot]
        comparisons = {}
        for name, rows in (("original", originals), ("query_rewrite", candidates)):
            summary = _summarize(rows) if rows else {
                "passed": 0, "total": 0, "recall_at_5": None, "mrr": None,
                "latency_p50_ms": None, "latency_p95_ms": None, "results": [],
            }
            comparisons[name] = {**summary, "expected": len(selected),
                                 "failed": sum(r["status"] == "failed" for r in rows),
                                 "interrupted": sum(r["status"] == "interrupted" for r in rows),
                                 "degraded": sum(r["status"] == "degraded" for r in rows),
                                 "invalid_outputs": sum(
                                     "rewrite_invalid_or_unchanged" in r.get("diagnostics", [])
                                     for r in rows),
                                 "successful_rewrites": sum(
                                     r.get("rewrite_succeeded") is True for r in rows
                                 ) if name == "query_rewrite" else None,
                                 "unattempted": len(selected) - len(rows)}
        attempted = {(row["id"], row["repetition"]) for row in candidates}
        report.update(
            status=status, comparisons=comparisons,
            remaining=[{"id": c.id, "split": c.split, "repetition": r}
                       for c, r in selected if (c.id, r) not in attempted],
            evidence={"source": "eventide.event_log", "database": str(host.store.path),
                      "run_ids": list(controls), "usage": build_usage_ledger(observed),
                      "costs": summarize_cost_events([e for e in observed if e["type"] in {
                          "budget.reserved", "cost.estimated",
                      }])},
            # Downstream task evidence and all-attempt cost comparisons are separate.
            promotion={"promote": False, "reason": "downstream_evidence_missing"
                       if status == "completed" else "incomplete_comparison"},
        )
        if output_path is not None:
            atomic_json(output_path, report)

    save("running")
    try:
        with tempfile.TemporaryDirectory(prefix="eventide-retrieval-rewrite-") as directory:
            workspace = Path(directory)
            for name, body in frozen.documents.items():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
            await owned_local_work(_init_git_workspace, workspace)
            build_started = time.perf_counter()
            stats = await owned_local_work(IndexStore(workspace).build,
                                           embedder=embedder, profile=profile)
            report.update(indexed_files=stats.files, indexed_chunks=stats.chunks,
                          chunking_fingerprint=stats.chunking_fingerprint,
                          build_duration_ms=(time.perf_counter() - build_started) * 1000)
            for case, repetition in selected:
                if host._run_intent(None) != configuration:
                    raise ValueError("Model configuration changed during the experiment")
                run_id = f"run_{uuid.uuid4().hex}"
                controls.append(run_id)

                async def execute(
                    owned: str, case: QueryCase = case, repetition: int = repetition,
                ) -> None:
                    original: RetrievalOutcome | None = None
                    outcome: RetrievalOutcome | None = None
                    retrieval_ms = original_ms = 0.0
                    started = time.perf_counter()
                    status = "completed"

                    async def search(query: str, k: int) -> RetrievalOutcome:
                        nonlocal original, retrieval_ms, original_ms
                        search_started = time.perf_counter()
                        try:
                            found = await owned_local_work(
                                retrieve, workspace, query, embedder=embedder, profile=profile,
                                reranker=reranker, k=k,
                            )
                        finally:
                            elapsed = (time.perf_counter() - search_started) * 1000
                            retrieval_ms += elapsed
                        if original is None:
                            original, original_ms = found, elapsed
                        return found

                    try:
                        outcome = await query_rewrite_outcome(
                            host, owned, workspace, search, case.query,
                        )
                        if any(d in {"rewrite_failed", "rewrite_index_changed"}
                               for d in outcome.diagnostics):
                            raise RuntimeError("Query rewrite did not complete; batch stopped")
                        if any(d.startswith("rewrite_") for d in outcome.diagnostics):
                            status = "degraded"
                    except BaseException as exc:
                        status = "interrupted" if isinstance(
                            exc, (asyncio.CancelledError, BudgetExceeded),
                        ) else "failed"
                        raise
                    finally:
                        available = original is not None
                        original = original or RetrievalOutcome((), "unavailable")
                        elapsed = (time.perf_counter() - started) * 1000
                        result = outcome or original
                        base = {**_grade_hits(case.model_dump(), list(original.results)),
                                "status": "completed" if available else status,
                                "duration_ms": original_ms if available else retrieval_ms,
                                "retrieval_duration_ms": original_ms if available else retrieval_ms,
                                "model_duration_ms": 0.0,
                                "returned_characters": sum(len(h.chunk.text)
                                                           for h in original.results),
                                "diagnostics": list(original.diagnostics),
                                "ranking": asdict(original), "repetition": repetition,
                                "split": case.split, "run_id": owned}
                        calls = build_usage_ledger(host.model_usage_events(owned))["calls"]
                        model_ms = sum(c["duration_ms"] for c in calls
                                       if c["duration_ms"] is not None)
                        candidate = {
                            **_grade_hits(case.model_dump(), list(result.results)),
                            "status": status, "duration_ms": elapsed,
                            "retrieval_duration_ms": retrieval_ms,
                            "model_duration_ms": model_ms,
                            "rewrite_succeeded": status == "completed"
                            and result.mode == "query-rrf",
                            "returned_characters": sum(len(h.chunk.text) for h in result.results),
                            "diagnostics": list(result.diagnostics),
                            "ranking": asdict(result), "repetition": repetition,
                            "split": case.split, "run_id": owned,
                        }
                        host.store.append_event(owned, "retrieval.benchmark_result", {
                            "original": base, "query_rewrite": candidate,
                        })
                        originals.append(base)
                        candidates.append(candidate)
                        events[owned] = host.store.run_events(owned)
                        save("running" if status in {"completed", "degraded"} else "aborted")

                try:
                    await host.run_control_operation(
                        kind="retrieval_benchmark", project_id=project_id, run_id=run_id,
                        accepted={**identity, "case": case.model_dump(), "repetition": repetition},
                        execute=execute,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if host.store.get_run_identity(run_id) is None:
                        controls.remove(run_id)
                        raise
                    report["stop_reason"] = type(exc).__name__
                    save("aborted")
                    return report
        save("completed")
        return report
    except BaseException as exc:
        report["stop_reason"] = type(exc).__name__
        save("aborted")
        raise
