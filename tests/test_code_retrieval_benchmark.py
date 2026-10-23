"""Code corpus contracts and comparison bookkeeping, never semantic-model scores."""

import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from eventide.rag import code_benchmark as benchmark
from eventide.rag.benchmark import DATASET_SHA256
from eventide.rag.embedding import HashEmbedder
from eventide.rag.evaluation import _profile
from eventide.rag.index import IndexedChunk, IndexStore
from eventide.rag.search import RetrievalOutcome, SearchResult


def test_frozen_real_code_corpus_is_distinct_and_annotations_stay_outside_source(tmp_path):
    dataset = benchmark.load_code_dataset()
    assert len(dataset.cases) == 24 and len(dataset.documents) == 19
    assert sum(len(t.encode()) for t in dataset.documents.values()) == 142971
    assert sum(p.complete_file for p in dataset.provenance.values()) == 16
    assert len({c.family for c in dataset.cases}) == 8
    for kind in ("identifier", "concept", "cross_file"):
        assert sum(c.kind == kind for c in dataset.cases) == 8
    workspace = benchmark.prepare_code_corpus(tmp_path / "source")
    assert (workspace / ".git").is_dir()
    assert not (workspace / "dataset.json").exists()
    assert not (workspace / ".eventide").exists()
    for path, source in dataset.provenance.items():
        assert hashlib.sha256((workspace / path).read_bytes()).hexdigest() == source.sha256
    old = Path(benchmark.__file__).parents[1] / "bundled_evals/retrieval-v1/dataset.json"
    assert hashlib.sha256(old.read_bytes()).hexdigest() == DATASET_SHA256
    with pytest.raises(FileExistsError):
        benchmark.prepare_code_corpus(workspace)


@pytest.mark.parametrize("change", [
    "path", "body", "provenance", "range", "duplicate", "family", "split", "missing",
    "needle", "cross_file", "label_overlap", "empty",
])
def test_manifest_rejects_data_and_label_drift(change):
    raw = benchmark.load_code_dataset().model_dump()
    if change == "path":
        raw["documents"]["../escape.py"] = "bad"
    elif change == "body":
        raw["documents"]["eventide/skills.py"] += "# drift\n"
    elif change == "provenance":
        del raw["provenance"]["eventide/skills.py"]
    elif change == "range":
        raw["provenance"]["eventide/skills.py"]["start_line"] = 2
    elif change == "duplicate":
        raw["cases"][1]["query"] = raw["cases"][0]["query"]
    elif change == "family":
        raw["cases"][12]["family"] = raw["cases"][0]["family"]
    elif change == "split":
        raw["cases"][0]["split"] = "holdout"
    elif change == "missing":
        raw["cases"][0]["evidence"][0]["file"] = "missing.py"
    elif change == "needle":
        raw["cases"][0]["evidence"][0]["contains"] = "not in any source"
    elif change == "cross_file":
        raw["cases"][2]["evidence"].pop()
    elif change == "label_overlap":
        raw["cases"][12]["evidence"] = raw["cases"][0]["evidence"]
    else:
        raw["cases"][0]["query"] = " "
    with pytest.raises(ValidationError):
        benchmark.CodeDataset.model_validate(raw)


def test_manifest_independent_review_has_narrow_questions_and_implementation_evidence():
    cases = {c.id: c for c in benchmark.load_code_dataset().cases}
    assert "不存在" not in cases["dev-attachments-concept"].query
    assert "恢复默认" not in cases["dev-generation-concept"].query
    assert "check_cancelled=check_cancelled" in (
        cases["hol-preparation-cross_file"].evidence[1].contains
    )


def test_evidence_grade_requires_both_files_fresh_visible_and_reports_overlap():
    case = benchmark.load_code_dataset().cases[2]
    first, second = case.evidence
    hits = [
        SearchResult(IndexedChunk(1, first.file, 1, 10, "", first.contains), 1, ("dense",),
                     freshness="fresh"),
        SearchResult(IndexedChunk(2, first.file, 8, 12, "", first.contains), .9, ("dense",),
                     freshness="fresh"),
        SearchResult(IndexedChunk(3, second.file, 1, 10, "", second.contains), .8, ("sparse",),
                     freshness="changed"),
    ]
    result = benchmark.grade_code_hits(case, RetrievalOutcome(tuple(hits), "dense+sparse"))
    assert result["recall_at_5"] == .5 and result["reciprocal_rank"] == 1
    assert not result["passed"] and result["hits"][2]["text"] == ""
    assert result["overlapping_hits"] == 1 and result["repeated_indexed_lines"] == 3
    hits[2] = replace(hits[2], freshness="fresh")
    assert benchmark.grade_code_hits(case, RetrievalOutcome(tuple(hits), "dense+sparse"))["passed"]
    hits[2] = replace(hits[2], chunk=replace(hits[2].chunk, text="x" * 800 + second.contains))
    assert not benchmark.grade_code_hits(case, RetrievalOutcome(tuple(hits), "dense+sparse"))[
        "passed"
    ]


@pytest.fixture(scope="module")
def comparison_indexes(tmp_path_factory):
    root = tmp_path_factory.mktemp("code-benchmark")
    workspace = benchmark.prepare_code_corpus(root / "source")
    historical = root / "historical"
    IndexStore(workspace, index_dir=historical).build(
        embedder=HashEmbedder(16), profile=_profile(16),
    )
    # These Hash databases exercise reading/identity/bookkeeping, not old/new chunk quality.
    # A synthetic windows-v1 identity keeps this reader test valid after production retires v1.
    with sqlite3.connect(historical / "index.sqlite") as connection:
        chunking = json.loads(connection.execute(
            "SELECT value FROM meta WHERE key='chunking_profile'",
        ).fetchone()[0])
        chunking["version"] = "windows-v1"
        body = json.dumps(chunking, sort_keys=True, separators=(",", ":"))
        connection.execute("UPDATE meta SET value=? WHERE key='chunking_profile'", (body,))
        connection.execute("UPDATE meta SET value=? WHERE key='chunking_fingerprint'",
                           (hashlib.sha256(body.encode()).hexdigest(),))
    directories = {"baseline_index_dir": historical}
    for argument in ("current_window_index_dir", "filtered_index_dir", "candidate_index_dir"):
        directories[argument] = root / argument
        shutil.copytree(historical, directories[argument])
        # Explicit synthetic policy fixtures, not a historical model-quality experiment.
        with sqlite3.connect(directories[argument] / "index.sqlite") as connection:
            chunking["version"] = "windows-v2"
            chunking["code_structure"] = argument == "candidate_index_dir"
            body = json.dumps(chunking, sort_keys=True, separators=(",", ":"))
            selection = json.dumps({"source_scope": "all" if argument ==
                                    "current_window_index_dir" else "code"})
            for key, value in {"chunking_profile": body,
                               "chunking_fingerprint": hashlib.sha256(body.encode()).hexdigest(),
                               "source_selection_profile": selection,
                               "source_selection_fingerprint": hashlib.sha256(
                                   selection.encode()).hexdigest()}.items():
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value),
                )
    return workspace, directories


def arguments(comparison_indexes):
    workspace, directories = comparison_indexes
    return {"workspace": workspace, **directories, "embedder": HashEmbedder(16),
            "profile": _profile(16), "local_repeats": 1,
            "baseline_runtime": {"commit": benchmark.HISTORICAL_REVISION,
                                 "dirty": False, "code_sha256": "0" * 64}}


def test_each_query_embeds_once_and_local_repeats_do_not_inflate_quality(
    comparison_indexes, monkeypatch, tmp_path,
):
    params = arguments(comparison_indexes)
    calls = []
    def vector(embedder, query):
        calls.append(query)
        return embedder.embed([query])[0]
    monkeypatch.setattr(benchmark, "embed_query", vector)
    output = tmp_path / "report.json"
    report = benchmark.grade_code_indexes(**{**params, "local_repeats": 3}, output_path=output,
                                          build_records={"historical_window_all": {"ms": 12}})
    assert report["status"] == "completed" and report["attempted"] == len(calls) == 12
    assert len(report["arms"]) == 5 and report["failed"] == 0 and report["remaining"] == []
    assert report["build_records"]["historical_window_all"]["ms"] == 12
    for arm, summary in report["comparisons"].items():
        assert summary["attempted"] == summary["measured"] == 12
        assert summary["local_timing_samples"] == 36
        assert summary["no_result_ratio"] is not None
        assert summary["complete_query_p95_ms"] is not None
        assert report["by_kind"]["cross_file"][arm]["attempted"] == 4
    for row in report["results"]:
        assert row["arms"]["fts"]["complete_query_ms"] == row["arms"]["fts"]["local_duration_ms"][0]
        result = row["arms"]["structured_code"]
        assert result["complete_query_ms"] == (
            row["embedding_duration_ms"] + result["local_duration_ms"][0]
        )
    assert not report["promotion"]["promote"]
    assert json.loads(output.read_text())["comparisons"] == report["comparisons"]
    with pytest.raises(ValueError, match="already exists"):
        benchmark.grade_code_indexes(**params, output_path=output)
    assert len(calls) == 12


@pytest.mark.parametrize("change", ["remote", "dirty", "revision", "missing_middle", "repeats"])
def test_invalid_comparison_rejected_before_embedding(comparison_indexes, change):
    params = arguments(comparison_indexes)
    if change == "remote":
        params["profile"] = replace(_profile(16), provider="openai_compatible")
    elif change == "dirty":
        params["baseline_runtime"]["dirty"] = True
    elif change == "revision":
        params["baseline_runtime"]["commit"] = "not-historical"
    elif change == "missing_middle":
        params.pop("filtered_index_dir")
    else:
        params["local_repeats"] = True
    with pytest.raises(ValueError):
        benchmark.grade_code_indexes(**params)


def test_holdout_has_only_predeclared_primary_arms(comparison_indexes):
    params = arguments(comparison_indexes)
    with pytest.raises(ValueError, match="three primary"):
        benchmark.grade_code_indexes(**params, split="holdout")
    params.pop("current_window_index_dir")
    params.pop("filtered_index_dir")
    report = benchmark.grade_code_indexes(**params, split="holdout")
    assert report["arms"] == ["fts", "historical_window_all", "structured_code"]
    assert report["attempted"] == 12
    assert all(c["id"].startswith("hol-") for c in report["results"])


def test_failure_retains_attempt_and_stops_remaining_batch(
    comparison_indexes, monkeypatch, tmp_path,
):
    def fail(embedder, query):
        raise RuntimeError("private provider detail")
    monkeypatch.setattr(benchmark, "embed_query", fail)
    output = tmp_path / "failed.json"
    report = benchmark.grade_code_indexes(**arguments(comparison_indexes), output_path=output)
    assert report["status"] == "aborted" and report["failed"] == report["attempted"] == 1
    assert len(report["remaining"]) == 11
    assert report["results"][0]["phase"] == "embedding"
    assert report["results"][0]["error_type"] == "RuntimeError"
    assert "private provider detail" not in output.read_text()
    assert report["comparisons"]["structured_code"]["recall_at_5"] == 0
    assert report["comparisons"]["structured_code"]["unattempted"] == 11


def test_stop_before_dispatch_is_not_failure(comparison_indexes):
    report = benchmark.grade_code_indexes(
        **arguments(comparison_indexes), stop_requested=lambda: True,
    )
    assert report["status"] == "aborted" and report["attempted"] == report["failed"] == 0
    assert len(report["remaining"]) == 12
    assert report["comparisons"]["structured_code"]["recall_at_5"] is None


def test_rebuilt_index_aborts_after_preserving_previous_arm(comparison_indexes, monkeypatch):
    retrieve = benchmark.retrieval_from_vector
    calls = []
    def drift(*args, **kwargs):
        outcome = retrieve(*args, **kwargs)
        calls.append(1)
        return replace(outcome, index_generation=900) if len(calls) == 2 else outcome
    monkeypatch.setattr(benchmark, "retrieval_from_vector", drift)
    report = benchmark.grade_code_indexes(**arguments(comparison_indexes))
    assert report["failed"] == 1 and len(report["remaining"]) == 11
    assert list(report["results"][0]["arms"]) == ["fts"]
    assert report["results"][0]["phase"] == "historical_window_all"


@pytest.mark.parametrize("drift", ["policy", "fingerprint", "embedding", "corpus"])
def test_prebuilt_index_drift_rejected_before_query(comparison_indexes, tmp_path, drift):
    params = arguments(comparison_indexes)
    target = tmp_path / "candidate"
    shutil.copytree(params["candidate_index_dir"], target)
    params["candidate_index_dir"] = target
    with sqlite3.connect(target / "index.sqlite") as connection:
        if drift == "policy":
            body = json.dumps({"source_scope": "all"})
            connection.execute(
                "UPDATE meta SET value=? WHERE key='source_selection_profile'", (body,),
            )
            connection.execute(
                "UPDATE meta SET value=? WHERE key='source_selection_fingerprint'",
                (hashlib.sha256(body.encode()).hexdigest(),),
            )
        elif drift == "fingerprint":
            connection.execute("UPDATE meta SET value='bad' WHERE key='chunking_fingerprint'")
        elif drift == "embedding":
            connection.execute("UPDATE meta SET value='other' WHERE key='embedding_model'")
        else:
            connection.execute("UPDATE files SET content_hash='changed'")
    with pytest.raises(ValueError):
        benchmark.grade_code_indexes(**params)


def test_unexpected_exception_propagates_and_preserves_interrupted_report(
    comparison_indexes, monkeypatch, tmp_path,
):
    def broken(*args):
        raise TypeError("programming defect")
    monkeypatch.setattr(benchmark, "embed_query", broken)
    path = tmp_path / "interrupted.json"
    with pytest.raises(TypeError, match="programming defect"):
        benchmark.grade_code_indexes(**arguments(comparison_indexes), output_path=path)
    report = json.loads(path.read_text())
    assert report["status"] == "aborted" and report["interrupted"] == 1
    assert report["failed"] == 0 and len(report["remaining"]) == 11
    result = report["comparisons"]["structured_code"]
    assert result["interrupted"] == 1 and result["failed"] == 0


def test_source_drift_prevents_another_attempt(comparison_indexes, tmp_path):
    params = arguments(comparison_indexes)
    params["workspace"] = benchmark.prepare_code_corpus(tmp_path / "changed-source")
    (params["workspace"] / "eventide/skills.py").write_text("modified source")
    with pytest.raises(ValueError, match="source differs"):
        benchmark.grade_code_indexes(**params)
