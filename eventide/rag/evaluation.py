"""Deterministic retrieval evaluation helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from eventide.rag.embedding import HASH_ALGORITHM_VERSION, EmbeddingProfile, HashEmbedder
from eventide.rag.index import IndexStore
from eventide.rag.search import hybrid_search

_SUITE_FIELDS = {"suite", "index", "cases"}
_CASE_FIELDS = {"id", "query", "expect_files", "expect_contains"}


def _load_suite(path: Path) -> tuple[Path, list[dict[str, Any]], str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Retrieval suite must be a mapping")
    unknown = set(raw) - _SUITE_FIELDS
    if unknown:
        raise ValueError(f"Unknown retrieval suite fields: {sorted(unknown)}")
    if not isinstance(raw.get("index"), str) or not raw["index"].strip():
        raise ValueError("Retrieval suite requires a corpus path in 'index'")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Retrieval suite requires a non-empty cases list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise ValueError(f"Retrieval case {position} must be a mapping")
        extra = set(case) - _CASE_FIELDS
        if extra:
            raise ValueError(f"Unknown fields in retrieval case {position}: {sorted(extra)}")
        case_id = case.get("id")
        query = case.get("query")
        expected = case.get("expect_files")
        contains = case.get("expect_contains")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"Retrieval case {position} has an invalid or duplicate id")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"Retrieval case {case_id} requires a query")
        if not isinstance(expected, list) or not expected or not all(
            isinstance(item, str) and item for item in expected
        ):
            raise ValueError(f"Retrieval case {case_id} requires expect_files")
        if contains is not None and not isinstance(contains, str):
            raise ValueError(f"Retrieval case {case_id} has invalid expect_contains")
        seen.add(case_id)
        normalized.append(
            {
                "id": case_id,
                "query": query,
                "expect_files": [PurePosixPath(item).as_posix() for item in expected],
                "expect_contains": contains,
            }
        )
    corpus = (path.parent / raw["index"]).resolve()
    if not corpus.is_dir():
        raise ValueError(f"Retrieval corpus does not exist: {corpus}")
    return corpus, normalized, str(raw.get("suite") or "retrieval")


def _init_git_workspace(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    with (path / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(".eventide/\n")


def _profile(dimension: int) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="hash",
        model="hash-v1",
        base_url="local",
        algorithm_version=HASH_ALGORITHM_VERSION,
        dimension=dimension,
    )


def run_retrieval_evaluation(path: Path) -> dict[str, Any]:
    """Build a temporary offline index and report macro recall@5 and MRR."""
    suite_path = path.expanduser().resolve()
    corpus, cases, suite_name = _load_suite(suite_path)
    started = time.perf_counter()
    dimension = 256
    embedder = HashEmbedder(dimension)
    profile = _profile(dimension)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="eventide-rag-eval-") as directory:
        workspace = Path(directory)
        shutil.copytree(corpus, workspace, dirs_exist_ok=True)
        _init_git_workspace(workspace)
        IndexStore(workspace).build(embedder=embedder, profile=profile)
        for case in cases:
            hits = hybrid_search(
                workspace,
                str(case["query"]),
                embedder=embedder,
                profile=profile,
                k=5,
            )
            expected = set(case["expect_files"])
            contains = case["expect_contains"]
            relevant_ranks: list[int] = []
            recalled: set[str] = set()
            rendered_hits: list[dict[str, Any]] = []
            for rank, hit in enumerate(hits, 1):
                is_relevant = hit.chunk.file in expected and (
                    contains is None or contains in hit.chunk.text
                )
                if is_relevant:
                    relevant_ranks.append(rank)
                    recalled.add(hit.chunk.file)
                rendered_hits.append(
                    {
                        "rank": rank,
                        "file": hit.chunk.file,
                        "start_line": hit.chunk.start_line,
                        "end_line": hit.chunk.end_line,
                        "score": round(hit.score, 8),
                        "sources": list(hit.sources),
                        "relevant": is_relevant,
                    }
                )
            recall = len(recalled) / len(expected)
            reciprocal_rank = 1.0 / min(relevant_ranks) if relevant_ranks else 0.0
            results.append(
                {
                    "id": case["id"],
                    "passed": recall == 1.0,
                    "recall_at_5": round(recall, 4),
                    "reciprocal_rank": round(reciprocal_rank, 4),
                    "hits": rendered_hits,
                }
            )
    passed = sum(bool(result["passed"]) for result in results)
    return {
        "mode": "offline-hash",
        "suite": suite_name,
        "suite_path": str(suite_path),
        "index": str(corpus),
        "passed": passed,
        "total": len(results),
        "recall_at_5": round(
            sum(float(result["recall_at_5"]) for result in results) / len(results), 4
        ),
        "mrr": round(
            sum(float(result["reciprocal_rank"]) for result in results) / len(results), 4
        ),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "results": results,
    }


def format_retrieval_report(report: dict[str, Any]) -> str:
    lines = [
        f"检索评测：{report['suite']}（offline HashEmbedder）",
        f"通过：{report['passed']}/{report['total']}",
        f"recall@5：{float(report['recall_at_5']):.4f}",
        f"MRR：{float(report['mrr']):.4f}",
    ]
    for result in report["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        lines.append(
            f"  - {status} {result['id']}: recall@5={result['recall_at_5']:.4f}, "
            f"RR={result['reciprocal_rank']:.4f}"
        )
    return "\n".join(lines)


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
