"""Frozen code retrieval ablations over externally built, local-only indexes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventide.eval_evidence import atomic_json, runtime_revision
from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider, embed_query
from eventide.rag.index import IndexStore
from eventide.rag.search import RetrievalOutcome, retrieval_from_vector
from eventide.rag.upgrade_evaluation import percentile, write_corpus

CODE_DATASET_SHA256 = "88853dbb8b8f3fd403e93556a2469afc3ba5c3b6791bf8c3c9653066ec1c7699"
HISTORICAL_REVISION = "97681036a8eabaedc0c25ad31db8466f6cdad35d"


class CodeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file: str
    contains: str = Field(min_length=1)


class CodeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    split: Literal["development", "holdout"]
    kind: Literal["identifier", "concept", "cross_file"]
    query: str = Field(min_length=1)
    evidence: list[CodeEvidence] = Field(min_length=1)


class CodeSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    complete_file: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CodeDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    version: Literal["retrieval-code-v2.0"]
    source_revision: Literal["97681036a8eabaedc0c25ad31db8466f6cdad35d"]
    documents: dict[str, str]
    provenance: dict[str, CodeSource]
    cases: list[CodeQuery]

    @model_validator(mode="after")
    def validate_manifest(self) -> CodeDataset:
        if self.documents.keys() != self.provenance.keys():
            raise ValueError("Every source needs provenance")
        for name, body in self.documents.items():
            path = PurePosixPath(name)
            if (not body or path.is_absolute() or ".." in path.parts or "\\" in name
                    or path.as_posix() != name or any(p.startswith(".") for p in path.parts)):
                raise ValueError("Invalid corpus path or content")
            origin = self.provenance[name]
            if (origin.source_path != name
                    or origin.end_line - origin.start_line + 1 != len(body.splitlines())
                    or (origin.complete_file and origin.start_line != 1)
                    or hashlib.sha256(body.encode()).hexdigest() != origin.sha256):
                raise ValueError("Source content/provenance mismatch")
        if len(self.cases) != 24 or len({c.id for c in self.cases}) != 24:
            raise ValueError("Code benchmark needs 24 unique query identities")
        if len({c.query.strip().casefold() for c in self.cases}) != 24:
            raise ValueError("Duplicate query text")
        families: dict[str, str] = {}
        labelled_files: dict[str, str] = {}
        for case in self.cases:
            if not case.query.strip():
                raise ValueError("Empty query")
            if families.setdefault(case.family, case.split) != case.split:
                raise ValueError("Task family crosses splits")
            expected = {e.file for e in case.evidence}
            if len(expected) != len(case.evidence) or len(expected) != (
                2 if case.kind == "cross_file" else 1
            ):
                raise ValueError("Invalid relevance file count")
            for evidence in case.evidence:
                if (evidence.file not in self.documents
                        or evidence.contains not in self.documents[evidence.file]):
                    raise ValueError("Relevance evidence missing from source")
                if labelled_files.setdefault(evidence.file, case.split) != case.split:
                    raise ValueError("Relevance target crosses splits")
        for split in ("development", "holdout"):
            counts = Counter(c.kind for c in self.cases if c.split == split)
            if counts != {"identifier": 4, "concept": 4, "cross_file": 4}:
                raise ValueError("Each split needs four queries of each kind")
        return self


def load_code_dataset(path: Path | None = None) -> CodeDataset:
    source = path or Path(__file__).parents[1] / "bundled_evals/retrieval-code-v2/dataset.json"
    body = source.read_bytes()
    if path is None and hashlib.sha256(body).hexdigest() != CODE_DATASET_SHA256:
        raise ValueError("Frozen code retrieval dataset changed")
    return CodeDataset.model_validate_json(body)


def prepare_code_corpus(root: Path, dataset: CodeDataset | None = None) -> Path:
    """Materialize only frozen source text in a new Git workspace; labels stay outside."""
    write_corpus(root, (dataset or load_code_dataset()).documents)
    return root.resolve()


def grade_code_hits(case: CodeQuery, outcome: RetrievalOutcome) -> dict[str, Any]:
    """Grade visible, fresh source evidence, not a matching path with unrelated text."""
    expected = {e.file: e.contains for e in case.evidence}
    relevant: set[str] = set()
    first_rank = 0
    hits = []
    seen_lines: set[tuple[str, int]] = set()
    total_lines = repeated_lines = overlapping_hits = 0
    remaining = 4000
    for rank, item in enumerate(outcome.results[:5], 1):
        chunk = item.chunk
        visible = chunk.text[:min(800, remaining)] if item.freshness == "fresh" else ""
        remaining -= len(visible)
        matched = chunk.file in expected and expected[chunk.file] in visible
        if matched:
            relevant.add(chunk.file)
            first_rank = first_rank or rank
        lines = {(chunk.file, line) for line in range(chunk.start_line, chunk.end_line + 1)}
        overlap = lines & seen_lines
        overlapping_hits += bool(overlap)
        repeated_lines += len(overlap)
        total_lines += len(lines)
        seen_lines.update(lines)
        hits.append({"file": chunk.file, "start_line": chunk.start_line,
                     "end_line": chunk.end_line, "text": visible, "heading": chunk.heading,
                     "freshness": item.freshness, "relevant": matched,
                     "sources": list(item.sources), "content_hash": chunk.content_hash})
    return {"passed": len(relevant) == len(expected),
            "recall_at_5": len(relevant) / len(expected),
            "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
            "hits": hits, "no_results": not hits, "mode": outcome.mode,
            "diagnostics": list(outcome.diagnostics),
            "unavailable_hits": sum(hit["freshness"] != "fresh" for hit in hits),
            "overlapping_hits": overlapping_hits,
            "repeated_indexed_lines": repeated_lines, "returned_indexed_lines": total_lines,
            "redundant_indexed_line_ratio": repeated_lines / total_lines if total_lines else 0.0,
            "index_generation": outcome.index_generation,
            "embedding_fingerprint": outcome.embedding_fingerprint}


def _summary(rows: list[dict[str, Any]], arm: str, planned: int) -> dict[str, Any]:
    measured = [row["arms"][arm] for row in rows if arm in row["arms"]]
    times = [t for result in measured for t in result["local_duration_ms"]]
    complete = [result["complete_query_ms"] for result in measured]
    total = len(rows)
    line_count = sum(r["returned_indexed_lines"] for r in measured)
    failed = sum(row["status"] == "failed" and arm not in row["arms"] for row in rows)
    interrupted = sum(row["status"] == "interrupted" and arm not in row["arms"] for row in rows)
    return {"attempted": total, "unattempted": planned - total, "measured": len(measured),
            "passed": sum(r["passed"] for r in measured), "failed": failed,
            "interrupted": interrupted, "unmeasured": total - len(measured),
            "recall_at_5": sum(r["recall_at_5"] for r in measured) / total if total else None,
            "mrr": sum(r["reciprocal_rank"] for r in measured) / total if total else None,
            "no_results": sum(r["no_results"] for r in measured),
            "no_result_ratio": sum(r["no_results"] for r in measured) / len(measured)
            if measured else None,
            "failure_ratio": failed / total if total else None,
            "local_timing_samples": len(times), "local_p50_ms": percentile(times, .5),
            "local_p95_ms": percentile(times, .95),
            "complete_query_p50_ms": percentile(complete, .5),
            "complete_query_p95_ms": percentile(complete, .95),
            "unavailable_hits": sum(r["unavailable_hits"] for r in measured),
            "overlapping_hits": sum(r["overlapping_hits"] for r in measured),
            "redundant_indexed_line_ratio":
            sum(r["repeated_indexed_lines"] for r in measured) / line_count if line_count else None}


def grade_code_indexes(
    workspace: Path, *, embedder: EmbeddingProvider, profile: EmbeddingProfile,
    baseline_index_dir: Path, candidate_index_dir: Path, baseline_runtime: dict[str, Any],
    current_window_index_dir: Path | None = None, filtered_index_dir: Path | None = None,
    build_records: dict[str, Any] | None = None,
    split: Literal["development", "holdout"] = "development", local_repeats: int = 3,
    output_path: Path | None = None, stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Compare prebuilt indexes without retaining a legacy chunker in production.

    The caller builds the historical index with HISTORICAL_REVISION and records
    its actual runtime fingerprint. This local-only experiment never calls an API.
    """
    if profile.provider not in {"hash", "local_semantic"}:
        raise ValueError("Code comparison supports local embeddings only")
    if split not in {"development", "holdout"} or type(local_repeats) is not int or not (
        1 <= local_repeats <= 3
    ):
        raise ValueError("Invalid split or local repeat count")
    if (baseline_runtime.get("commit") != HISTORICAL_REVISION
            or baseline_runtime.get("dirty") is not False
            or not baseline_runtime.get("code_sha256")):
        raise ValueError("Historical index requires the clean fixed runtime fingerprint")
    if split == "development" and (
        current_window_index_dir is None or filtered_index_dir is None
    ):
        raise ValueError("Development requires both window and filtering intermediate indexes")
    if split == "holdout" and (
        current_window_index_dir is not None or filtered_index_dir is not None
    ):
        raise ValueError("Holdout is frozen to the three primary arms")
    dataset = load_code_dataset()
    for path, origin in dataset.provenance.items():
        source = workspace / path
        if source.resolve() != source or hashlib.sha256(source.read_bytes()).hexdigest() != (
            origin.sha256
        ):
            raise ValueError("Experiment source differs from the frozen corpus")
    arms = {"fts": baseline_index_dir, "historical_window_all": baseline_index_dir}
    if current_window_index_dir is not None and filtered_index_dir is not None:
        arms.update(current_window_all=current_window_index_dir, filtered_window=filtered_index_dir)
    arms["structured_code"] = candidate_index_dir
    indexes = {}
    for arm, directory in arms.items():
        with IndexStore(workspace, index_dir=directory).snapshot() as reader:
            resolved = reader.validate_profile(profile)
            generation, fingerprint = reader.retrieval_identity()
            with reader._reader() as connection:
                meta = reader._meta(connection)
                chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                files = dict(connection.execute("SELECT path, content_hash FROM files"))
            if any(path not in dataset.provenance or dataset.provenance[path].sha256 != digest
                   for path, digest in files.items()):
                raise ValueError("Prebuilt index does not belong to the frozen corpus")
            selection = meta.get("source_selection_profile")
            for key in ("chunking", "source_selection"):
                encoded = meta.get(f"{key}_profile")
                if encoded is not None and hashlib.sha256(encoded.encode()).hexdigest() != (
                    meta[f"{key}_fingerprint"]
                ):
                    raise ValueError("Prebuilt index policy fingerprint mismatch")
            indexes[arm] = {"generation": generation, "embedding_fingerprint": fingerprint,
                            "dimension": resolved.dimension, "files": len(files), "chunks": chunks,
                            "chunking_profile": json.loads(meta["chunking_profile"]),
                            "chunking_fingerprint": meta["chunking_fingerprint"],
                            "source_selection_profile": json.loads(selection)
                            if selection is not None else None,
                            "source_selection_fingerprint":
                            meta.get("source_selection_fingerprint"),
                            "source_scope": json.loads(selection)["source_scope"]
                            if selection is not None else "not_recorded"}
    if len({v["embedding_fingerprint"] for v in indexes.values()}) != 1:
        raise ValueError("All arms must use the same embedding profile")
    historical = indexes["historical_window_all"]["chunking_profile"]
    if historical["version"] != "windows-v1" or historical["code_structure"]:
        raise ValueError("Historical baseline must be the original windows-v1 index")
    for arm in ("current_window_all", "filtered_window", "structured_code"):
        if arm not in indexes:
            continue
        expected_scope = "all" if arm == "current_window_all" else "code"
        chunking = indexes[arm]["chunking_profile"]
        if (indexes[arm]["source_scope"] != expected_scope
                or chunking["version"] != "windows-v2"
                or chunking["code_structure"] != (arm == "structured_code")):
            raise ValueError(f"Prebuilt index policy does not match arm: {arm}")
    cases = [c for c in dataset.cases if c.split == split]
    report: dict[str, Any] = {
        "schema_version": 1, "experiment": "retrieval-code-v2", "status": "running",
        "dataset_version": dataset.version, "dataset_sha256": CODE_DATASET_SHA256,
        "source_revision": dataset.source_revision,
        "corpus_sha256": hashlib.sha256(json.dumps(
            dataset.documents, sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest(), "split": split, "runtime": runtime_revision(),
        "historical_runtime": baseline_runtime, "indexes": indexes,
        "build_records": build_records, "cases": [c.model_dump() for c in cases],
        "local_repeats": local_repeats, "arms": list(arms), "results": [],
        "limitations": ["Query vectors are reused; local timing repeats are not new questions",
                        "Local timing includes current-source freshness verification",
                        "Complete query timing is one vector plus the first local search",
                        "Build stage measurements come from the caller and may be absent",
                        "Overlapping indexed lines describe repeated source ranges, not Token use",
                        "Unmeasured attempts score zero; failures and interruptions stay separate",
                        "Queries never dispatched are excluded from quality denominators",
                        "Retrieval scores do not establish downstream Agent or repair quality"],
        "promotion": {"promote": False, "reason": "downstream_evidence_required"},
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            raise ValueError("Experiment report already exists; preserve prior attempts")
        atomic_json(output_path, report)
    try:
        for case in cases:
            if stop_requested is not None and stop_requested():
                report["status"] = "aborted"
                break
            row: dict[str, Any] = {"id": case.id, "kind": case.kind, "family": case.family,
                                   "status": "running", "phase": "embedding", "arms": {}}
            report["results"].append(row)
            start = time.perf_counter()
            try:
                vector = embed_query(embedder, case.query)
                row["embedding_duration_ms"] = (time.perf_counter() - start) * 1000
                for arm, directory in arms.items():
                    row["phase"] = arm
                    times = []
                    for repeat in range(local_repeats):
                        start = time.perf_counter()
                        outcome = retrieval_from_vector(
                            workspace, case.query, vector=None if arm == "fts" else vector,
                            profile=None if arm == "fts" else profile, index_dir=directory,
                        )
                        times.append((time.perf_counter() - start) * 1000)
                        if (outcome.index_generation != indexes[arm]["generation"]
                                or outcome.embedding_fingerprint !=
                                indexes[arm]["embedding_fingerprint"]):
                            raise RuntimeError("Index changed during the experiment")
                        if repeat == 0:
                            result = grade_code_hits(case, outcome)
                    row["arms"][arm] = {
                        **result, "local_duration_ms": times,
                        "complete_query_ms": times[0] + (
                            row["embedding_duration_ms"] if arm != "fts" else 0
                        ),
                    }
                row["status"] = "completed"
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                row.update(status="failed", error_type=type(exc).__name__)
                report["status"] = "aborted"
                break
            finally:
                if output_path is not None:
                    atomic_json(output_path, report)
        else:
            report["status"] = "completed"
    finally:
        rows = report["results"]
        if report["status"] == "running":
            report["status"] = "aborted"
            for row in rows:
                if row["status"] == "running":
                    row["status"] = "interrupted"
        report["comparisons"] = {arm: _summary(rows, arm, len(cases)) for arm in arms}
        report["by_kind"] = {
            kind: {arm: _summary([r for r in rows if r["kind"] == kind], arm, 4)
                   for arm in arms} for kind in ("identifier", "concept", "cross_file")
        }
        report["attempted"] = len(rows)
        report["failed"] = sum(row["status"] == "failed" for row in rows)
        report["interrupted"] = sum(row["status"] == "interrupted" for row in rows)
        report["remaining"] = [case.id for case in cases[len(rows):]]
        embeddings = [row["embedding_duration_ms"] for row in rows
                      if "embedding_duration_ms" in row]
        report["embedding_p50_ms"] = percentile(embeddings, .5)
        report["embedding_p95_ms"] = percentile(embeddings, .95)
        if report["status"] != "completed":
            report["promotion"]["reason"] = "incomplete_comparison"
        if output_path is not None:
            atomic_json(output_path, report)
    return report
