"""Offline retrieval metrics use labeled files rather than model self-report."""

import json
from pathlib import Path

import pytest

from eventide.cli import main
from eventide.rag.evaluation import run_retrieval_evaluation


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


def test_retrieval_eval_cli_json(isolated_workspace: Path, capsys) -> None:
    suite = _suite(isolated_workspace)

    assert main(["rag", "eval", str(suite), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["suite"] == "tiny-retrieval"
    assert report["mode"] == "offline-hash"


def test_retrieval_suite_rejects_unknown_fields(isolated_workspace: Path) -> None:
    suite = _suite(isolated_workspace)
    suite.write_text(
        "suite: bad\nindex: corpus\nunknown: true\ncases: []\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Unknown retrieval suite fields"):
        run_retrieval_evaluation(suite)
