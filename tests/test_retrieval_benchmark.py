"""Frozen labels and failed attempts remain visible in retrieval comparisons."""

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from eventide.rag.benchmark import (
    RetrievalDataset,
    load_dataset,
    promotion_decision,
    run_benchmark,
)
from eventide.rag.embedding import HashEmbedder
from eventide.rag.evaluation import _profile


def test_frozen_dataset_has_two_disjoint_twenty_query_splits():
    dataset = load_dataset()
    development = {c.id for c in dataset.cases if c.split == "development"}
    holdout = {c.id for c in dataset.cases if c.split == "holdout"}
    assert len(development) == len(holdout) == 20
    assert not development & holdout
    assert len(dataset.documents) == 16


@pytest.mark.parametrize("change", ["path", "duplicate", "label", "passage", "split", "empty"])
def test_manifest_rejects_untrustworthy_labels_or_paths(change):
    raw = load_dataset().model_dump()
    if change == "path":
        raw["documents"]["../escape.py"] = "bad"
    elif change == "duplicate":
        raw["cases"][1]["query"] = raw["cases"][0]["query"]
    elif change == "label":
        raw["cases"][0]["expect_files"] = ["missing.py"]
    elif change == "passage":
        raw["cases"][0]["expect_contains"] = "no such evidence"
    elif change == "split":
        raw["cases"][0]["split"] = "holdout"
    else:
        raw["cases"][0]["query"] = " "
    with pytest.raises(ValidationError):
        RetrievalDataset.model_validate(raw)


def test_hash_mechanism_reports_failures_and_does_not_claim_semantic_quality(tmp_path):
    def failed(workspace, query):
        raise RuntimeError("private error text")
    report = run_benchmark(embedder=HashEmbedder(16), profile=_profile(16), repeats=1,
                           extra_methods={"failure": failed})
    assert report["comparisons"]["sparse"]["total"] == 20
    failure = report["comparisons"]["failure"]
    assert failure["failed"] == 20 and failure["passed"] == 0
    assert "private error text" not in json.dumps(report)
    assert all(not decision["promote"] for decision in report["promotion"].values())
    assert len(report["dataset_sha256"]) == 64
    assert all(r["split"] == "development" for r in failure["results"])
    assert all(r["returned_characters"] >= 0 for r in report["comparisons"]["hybrid"]["results"])


@pytest.mark.parametrize("arguments", [
    {"repeats": 0}, {"repeats": True}, {"split": "wrong"},
    {"profile": replace(_profile(16), provider="openai_compatible")},
    {"extra_methods": {"sparse": lambda w, q: None}},
    {"reranker": object()},
])
def test_invalid_experiments_are_rejected(arguments):
    params = {"embedder": HashEmbedder(16), "profile": _profile(16), **arguments}
    with pytest.raises(ValueError):
        run_benchmark(**params)


def test_promotion_requires_all_three_independent_evidence_conditions():
    baseline = {"recall_at_5": 0.5, "mrr": 0.5, "latency_p95_ms": 10}
    candidate = {"recall_at_5": 0.6, "mrr": 0.5, "latency_p95_ms": 20}
    assert not promotion_decision(baseline, candidate)["promote"]
    assert promotion_decision(baseline, candidate, downstream_baseline=10,
                              downstream_candidate=10)["promote"]
    assert not promotion_decision(baseline, {**candidate, "latency_p95_ms": 21},
                                  downstream_baseline=10, downstream_candidate=10)["promote"]
    assert not promotion_decision(baseline, {**candidate, "mrr": 0.4},
                                  downstream_baseline=10, downstream_candidate=10)["promote"]
    assert not promotion_decision(baseline, candidate, downstream_baseline=10,
                                  downstream_candidate=9)["promote"]
