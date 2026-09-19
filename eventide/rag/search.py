"""Sparse, dense, and reciprocal-rank-fusion retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import DenseCandidate, IndexedChunk, IndexStore, SparseCandidate

RRF_K = 60


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


def rrf_fuse(
    dense: list[DenseCandidate], sparse: list[SparseCandidate], *, k: int
) -> list[SearchResult]:
    """Fuse incomparable dense and BM25 scales using one-based ranks only."""
    count = validate_k(k)
    by_id: dict[int, SearchResult] = {}
    for source, candidates in (("dense", dense), ("sparse", sparse)):
        for candidate in candidates:
            existing = by_id.get(candidate.chunk.id)
            score = 1.0 / (RRF_K + candidate.rank)
            if existing is None:
                by_id[candidate.chunk.id] = SearchResult(
                    chunk=candidate.chunk,
                    score=score,
                    sources=(source,),
                    dense_rank=candidate.rank if source == "dense" else None,
                    sparse_rank=candidate.rank if source == "sparse" else None,
                )
            else:
                present = {*existing.sources, source}
                sources = tuple(item for item in ("dense", "sparse") if item in present)
                by_id[candidate.chunk.id] = SearchResult(
                    chunk=existing.chunk,
                    score=existing.score + score,
                    sources=sources,
                    dense_rank=(
                        candidate.rank if source == "dense" else existing.dense_rank
                    ),
                    sparse_rank=(
                        candidate.rank if source == "sparse" else existing.sparse_rank
                    ),
                )

    def sort_key(result: SearchResult) -> tuple[float, int, str, int, int]:
        ranks = [rank for rank in (result.dense_rank, result.sparse_rank) if rank is not None]
        return (
            -result.score,
            min(ranks),
            result.chunk.file,
            result.chunk.start_line,
            result.chunk.id,
        )

    return sorted(by_id.values(), key=sort_key)[:count]


def hybrid_search(
    workspace: Path,
    query: str,
    *,
    embedder: EmbeddingProvider,
    profile: EmbeddingProfile,
    k: int = 5,
) -> list[SearchResult]:
    """Run dense and trigram/BM25 retrieval, then combine them with RRF."""
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
    dense = store.dense_search(vector, limit=count * 2)
    sparse = store.sparse_search(cleaned, limit=count * 2)
    return rrf_fuse(dense, sparse, k=count)


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
