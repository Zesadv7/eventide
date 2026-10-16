"""Explicit Python index/search checks remain after terminal UI retirement."""

import subprocess
from pathlib import Path

import pytest

from eventide.rag.embedding import HASH_ALGORITHM_VERSION, EmbeddingProfile, HashEmbedder
from eventide.rag.index import IndexStore
from eventide.rag.search import format_results, hybrid_search

PROFILE = EmbeddingProfile("hash", "hash-v1", "local", HASH_ALGORITHM_VERSION, 256)


def workspace(root: Path) -> Path:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".git/info/exclude").write_text(".eventide/\n", encoding="utf-8")
    (root / "knowledge.txt").write_text("deployment uses blue-green rollout", encoding="utf-8")
    return root


def test_index_and_search_python_interfaces(isolated_workspace):
    root = workspace(isolated_workspace)
    stats = IndexStore(root).build(embedder=HashEmbedder(), profile=PROFILE)
    assert stats.files == stats.chunks == 1
    hits = hybrid_search(root, "blue-green", embedder=HashEmbedder(), profile=PROFILE)
    output = format_results(hits, mode="dense+sparse")
    assert "mode: dense+sparse" in output
    assert "knowledge.txt:1-1" in output


def test_python_search_validates_result_limit(isolated_workspace):
    root = workspace(isolated_workspace)
    IndexStore(root).build(embedder=HashEmbedder(), profile=PROFILE)
    with pytest.raises(ValueError, match="k must be between 1 and 10"):
        hybrid_search(root, "rollout", k=11, embedder=HashEmbedder(), profile=PROFILE)
