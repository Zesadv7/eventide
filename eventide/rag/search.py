"""Sparse, dense, and reciprocal-rank-fusion retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import IndexedChunk, IndexStore


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk: IndexedChunk
    score: float
    sources: tuple[str, ...]
    dense_rank: int | None = None
    sparse_rank: int | None = None


def validate_k(k: int) -> int:
    if not 1 <= k <= 10:
        raise ValueError("k must be between 1 and 10")
    return k


def sparse_search(workspace: Path, query: str, *, k: int = 5) -> list[SearchResult]:
    """Run the P1 keyword-only retrieval path."""
    count = validate_k(k)
    candidates = IndexStore(workspace).sparse_search(query, limit=count)
    return [
        SearchResult(
            chunk=candidate.chunk,
            score=-candidate.bm25,
            sources=("sparse",),
            sparse_rank=candidate.rank,
        )
        for candidate in candidates
    ]


def dense_search(
    workspace: Path,
    query: str,
    *,
    embedder: EmbeddingProvider,
    profile: EmbeddingProfile,
    k: int = 5,
) -> list[SearchResult]:
    """Embed a query and run the P2 exhaustive cosine path."""
    count = validate_k(k)
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Search query must not be empty")
    store = IndexStore(workspace)
    stored_profile = store.validate_profile(profile)
    vectors = embedder.embed([cleaned])
    if len(vectors) != 1:
        raise RuntimeError("Embedding response count does not match the request")
    vector = vectors[0]
    if stored_profile.dimension != len(vector):
        raise ValueError("Knowledge index vector dimension is inconsistent; rebuild --force")
    candidates = store.dense_search(vector, limit=count)
    return [
        SearchResult(
            chunk=candidate.chunk,
            score=candidate.similarity,
            sources=("dense",),
            dense_rank=candidate.rank,
        )
        for candidate in candidates
    ]


def format_results(results: list[SearchResult], *, mode: str) -> str:
    lines = [f"Found {len(results)} passages (mode: {mode}):"]
    for index, result in enumerate(results, 1):
        chunk = result.chunk
        source_text = "+".join(result.sources)
        lines.append(
            f"[{index}] {chunk.file}:{chunk.start_line}-{chunk.end_line} "
            f"(score {result.score:.4f}, {source_text})"
        )
        lines.extend(f"    {line}" for line in chunk.text[:800].splitlines())
    return "\n".join(lines)
