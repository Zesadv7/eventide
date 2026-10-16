"""Sparse, dense, and reciprocal-rank-fusion retrieval."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import DenseCandidate, IndexedChunk, IndexStore, SparseCandidate
from eventide.rag.local_models import MAX_RERANK_CANDIDATES, LocalModelError

RRF_K = 60


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk: IndexedChunk
    score: float
    sources: tuple[str, ...]
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None


class Reranker(Protocol):
    def score(self, query: str, passages: list[str]) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    results: tuple[SearchResult, ...]
    mode: str
    diagnostics: tuple[str, ...] = ()


def validate_k(k: int) -> int:
    if type(k) is not int or not 1 <= k <= 10:
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
    store.validate_profile(profile)
    vectors = embedder.embed([cleaned])
    if len(vectors) != 1:
        raise RuntimeError("Embedding response count does not match the request")
    vector = vectors[0]
    with store.snapshot() as reader:
        stored_profile = reader.validate_profile(profile)
        if stored_profile.dimension != len(vector):
            raise ValueError("Knowledge index vector dimension is inconsistent; rebuild --force")
        candidates = reader.dense_search(vector, limit=count)
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
    return _rrf_candidates(dense, sparse, limit=count)


def _rrf_candidates(
    dense: list[DenseCandidate], sparse: list[SparseCandidate], *, limit: int,
) -> list[SearchResult]:
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

    return sorted(by_id.values(), key=sort_key)[:limit]


def hybrid_candidates_from_vector(
    workspace: Path, query: str, vector: list[float], *, profile: EmbeddingProfile,
    candidate_limit: int = MAX_RERANK_CANDIDATES,
) -> list[SearchResult]:
    """Fuse one already-observed query vector in a pinned read snapshot.

    Async/remote callers can obtain the vector through their own governed request
    chain, then invoke this local function without a second embedding request.
    """
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= MAX_RERANK_CANDIDATES:
        raise ValueError("candidate_limit must be between 1 and 20")
    cleaned = query.strip()
    if not cleaned or not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("Search needs a nonempty query and a finite embedding vector")
    with IndexStore(workspace).snapshot() as reader:
        stored_profile = reader.validate_profile(profile)
        if stored_profile.dimension != len(vector):
            raise ValueError("Knowledge index vector dimension is inconsistent; rebuild --force")
        dense = reader.dense_search(vector, limit=candidate_limit)
        sparse = reader.sparse_search(cleaned, limit=candidate_limit)
    return _rrf_candidates(dense, sparse, limit=candidate_limit)


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
    # Fail before a potentially remote embedding request, then revalidate the
    # pinned snapshot in case a rebuild commits while the request is in flight.
    store.validate_profile(profile)
    vectors = embedder.embed([cleaned])
    if len(vectors) != 1:
        raise RuntimeError("Embedding response count does not match the request")
    return hybrid_candidates_from_vector(
        workspace, cleaned, vectors[0], profile=profile, candidate_limit=count * 2,
    )[:count]


def retrieval_from_vector(
    workspace: Path, query: str, *, vector: list[float] | None,
    profile: EmbeddingProfile | None, reranker: Reranker | None = None,
    k: int = 5, diagnostics: tuple[str, ...] = (),
) -> RetrievalOutcome:
    """Return explicit retrieval mode, independent rerank scores and fallback reasons."""
    count = min(validate_k(k), 5) if reranker is not None else validate_k(k)
    if vector is None:
        candidates = sparse_search(workspace, query, k=count)
        return RetrievalOutcome(tuple(candidates), "sparse", diagnostics)
    if profile is None:
        raise ValueError("An embedding profile is required with a query vector")
    candidates = hybrid_candidates_from_vector(
        workspace, query, vector, profile=profile,
        candidate_limit=MAX_RERANK_CANDIDATES if reranker is not None else count * 2,
    )
    if reranker is None or not candidates:
        return RetrievalOutcome(tuple(candidates[:count]), "dense+sparse", diagnostics)
    try:
        scores = reranker.score(query, [item.chunk.text for item in candidates])
        if len(scores) != len(candidates) or any(not math.isfinite(value) for value in scores):
            raise LocalModelError("local_reranker_invalid_response")
        ranked = sorted(enumerate(candidates), key=lambda item: (-scores[item[0]], item[0]))
        results = tuple(
            replace(item, rerank_score=float(scores[index]), rerank_rank=rank)
            for rank, (index, item) in enumerate(ranked[:count], 1)
        )
        return RetrievalOutcome(results, "dense+sparse+rerank", diagnostics)
    except Exception as exc:
        reason = exc.code if isinstance(exc, LocalModelError) else "local_reranker_failed"
        return RetrievalOutcome(tuple(candidates[:count]), "dense+sparse", (*diagnostics, reason))


def retrieve(
    workspace: Path, query: str, *, embedder: EmbeddingProvider,
    profile: EmbeddingProfile, reranker: Reranker | None = None, k: int = 5,
) -> RetrievalOutcome:
    """Synchronous local/development adapter; production can use retrieval_from_vector."""
    validate_k(k)
    if not query.strip():
        raise ValueError("Search query must not be empty")
    IndexStore(workspace).validate_profile(profile)
    try:
        vectors = embedder.embed([query.strip()])
    except LocalModelError as exc:
        return retrieval_from_vector(
            workspace, query, vector=None, profile=None, k=k, diagnostics=(exc.code,),
        )
    if len(vectors) != 1:
        raise RuntimeError("Embedding response count does not match the request")
    return retrieval_from_vector(
        workspace, query, vector=vectors[0], profile=profile, reranker=reranker, k=k,
    )


def format_results(
    results: list[SearchResult], *, mode: str, diagnostics: tuple[str, ...] = (),
) -> str:
    lines = [f"Found {len(results)} passages (mode: {mode}):"]
    if diagnostics:
        lines.append("Retrieval degraded: " + ", ".join(diagnostics))
    for index, result in enumerate(results, 1):
        chunk = result.chunk
        source_text = "+".join(result.sources)
        lines.append(
            f"[{index}] {chunk.file}:{chunk.start_line}-{chunk.end_line} "
            f"(score {result.score:.4f}, {source_text})"
        )
        if result.rerank_score is not None:
            lines.append(f"    CrossEncoder score {result.rerank_score:.4f}; RRF score unchanged")
        lines.extend(f"    {line}" for line in chunk.text[:800].splitlines())
    return "\n".join(lines)
