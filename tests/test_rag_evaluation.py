"""Offline retrieval metrics use labeled files rather than model self-report."""

import json
from pathlib import Path

import pytest

from eventide.cli import main
from eventide.rag.evaluation import (
    _grade_hits,
    format_retrieval_report,
    report_json,
    run_retrieval_evaluation,
)
from eventide.rag.index import IndexedChunk
from eventide.rag.search import SearchResult


def _suite(root: Path) -> Path:
    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "auth.py").write_text(
        "def verify_token(value):\n    return value == 'signed'\n", encoding="utf-8"
    )
    (corpus / "deploy.md").write_text(
        "# Release\nblue green deployment rollout\n", encoding="utf-8"
    )
    (corpus / "other.txt").write_text("unrelated cache entry", encoding="utf-8")
    suite = root / "retrieval.yaml"
    suite.write_text(
        """suite: tiny-retrieval
index: corpus
cases:
  - id: auth
    query: verify_token signed
    expect_files: [auth.py]
    expect_contains: verify_token
  - id: deploy
    query: blue green rollout
    expect_files: [deploy.md]
""",
        encoding="utf-8",
    )
    return suite


def test_retrieval_eval_reports_recall_and_mrr(isolated_workspace: Path) -> None:
    report = run_retrieval_evaluation(_suite(isolated_workspace))

    assert report["passed"] == report["total"] == 2
    assert report["recall_at_5"] == 1.0
    assert report["mrr"] == 1.0
    assert all(result["hits"][0]["relevant"] for result in report["results"])
    assert "recall@5：1.0000" in format_retrieval_report(report)
    assert json.loads(report_json(report))["mrr"] == 1.0
    assert set(report["comparisons"]) == {"sparse", "dense", "hybrid"}
    assert report["indexed_files"] == report["indexed_chunks"] == 3
    assert len(report["corpus_sha256"]) == 64


def test_grading_does_not_confuse_partial_recall_with_first_hit():
    case = {"id": "partial", "expect_files": ["a.txt", "b.txt"], "expect_contains": "answer"}
    hits = [
        SearchResult(IndexedChunk(1, "other.txt", 1, 1, "", "answer"), 1.0, ("sparse",)),
        SearchResult(IndexedChunk(2, "a.txt", 1, 1, "", "answer"), 0.5, ("sparse",)),
        SearchResult(IndexedChunk(3, "a.txt", 2, 2, "", "answer"), 0.4, ("sparse",)),
        SearchResult(IndexedChunk(4, "b.txt", 1, 1, "", "wrong passage"), 0.3, ("sparse",)),
    ]
    result = _grade_hits(case, hits)
    assert result["passed"] is False
    assert result["recall_at_5"] == 0.5
    assert result["reciprocal_rank"] == 0.5
    assert _grade_hits(case, [])["reciprocal_rank"] == 0.0


def test_corpus_hash_changes_and_missing_labels_fail(isolated_workspace):
    suite = _suite(isolated_workspace)
    before = run_retrieval_evaluation(suite)
    source = isolated_workspace / "corpus" / "other.txt"
    source.write_text("changed unrelated document", encoding="utf-8")
    after = run_retrieval_evaluation(suite)
    assert before["corpus_sha256"] != after["corpus_sha256"]
    assert before["suite_sha256"] == after["suite_sha256"]
    suite.write_text(
        suite.read_text(encoding="utf-8").replace("auth.py", "absent.py"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Expected file is absent"):
        run_retrieval_evaluation(suite)


def test_retrieval_eval_cli_json(isolated_workspace: Path, capsys) -> None:
    suite = _suite(isolated_workspace)

    assert main(["rag", "eval", str(suite), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["suite"] == "tiny-retrieval"
    assert report["mode"] == "offline-hash"


def test_retrieval_suite_rejects_unknown_fields(isolated_workspace: Path) -> None:
    suite = _suite(isolated_workspace)
    suite.write_text("suite: bad\nindex: corpus\nunknown: true\ncases: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown retrieval suite fields"):
        run_retrieval_evaluation(suite)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[]\n", "must be a mapping"),
        ("suite: bad\ncases: []\n", "corpus path"),
        ("suite: bad\nindex: corpus\ncases: []\n", "non-empty cases"),
        (
            "suite: bad\nindex: corpus\ncases:\n  - id: x\n    query: q\n    expect_files: []\n",
            "expect_files",
        ),
    ],
)
def test_retrieval_suite_validation(isolated_workspace: Path, body: str, message: str) -> None:
    (isolated_workspace / "corpus").mkdir()
    suite = isolated_workspace / "bad.yaml"
    suite.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        run_retrieval_evaluation(suite)
