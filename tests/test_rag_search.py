"""Dense retrieval and embedding identity checks."""

import subprocess
from pathlib import Path

import pytest

from eventide.rag.embedding import HASH_ALGORITHM_VERSION, EmbeddingProfile, HashEmbedder
from eventide.rag.index import IndexStore
from eventide.rag.search import dense_search


def _workspace(root: Path) -> Path:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".git" / "info" / "exclude").write_text(".eventide/\n", encoding="utf-8")
    return root


def _profile(dimension: int) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="hash",
        model="hash-v1",
        base_url="local",
        algorithm_version=HASH_ALGORITHM_VERSION,
        dimension=dimension,
    )


def test_dense_search_prefers_matching_hash_tokens(isolated_workspace: Path) -> None:
    root = _workspace(isolated_workspace)
    (root / "alpha.txt").write_text("alpha alpha authorization", encoding="utf-8")
    (root / "beta.txt").write_text("beta deployment", encoding="utf-8")
    embedder = HashEmbedder(64)
    profile = _profile(64)
    IndexStore(root).build(embedder=embedder, profile=profile)

    results = dense_search(root, "alpha authorization", embedder=embedder, profile=profile)

    assert results[0].chunk.file == "alpha.txt"
    assert results[0].sources == ("dense",)
    assert results[0].dense_rank == 1


def test_embedding_fingerprint_change_requires_force(isolated_workspace: Path) -> None:
    root = _workspace(isolated_workspace)
    (root / "doc.txt").write_text("stable content", encoding="utf-8")
    store = IndexStore(root)
    store.build(embedder=HashEmbedder(8), profile=_profile(8))

    with pytest.raises(ValueError, match="configuration changed"):
        store.build(embedder=HashEmbedder(16), profile=_profile(16))

    stats = store.build(force=True, embedder=HashEmbedder(16), profile=_profile(16))
    assert stats.rebuilt_files == 1
    assert store.embedding_profile().dimension == 16


def test_zero_query_vector_has_no_dense_candidates(isolated_workspace: Path) -> None:
    root = _workspace(isolated_workspace)
    (root / "doc.txt").write_text("content", encoding="utf-8")
    store = IndexStore(root)
    store.build(embedder=HashEmbedder(8), profile=_profile(8))
    assert store.dense_search([0.0] * 8, limit=5) == []

