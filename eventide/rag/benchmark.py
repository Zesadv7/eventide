"""Frozen retrieval ablations; local models are explicit and failures remain in reports."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

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
