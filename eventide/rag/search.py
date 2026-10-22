"""Sparse, dense, and reciprocal-rank-fusion retrieval."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider, embed_query
from eventide.rag.index import DenseCandidate, IndexedChunk, IndexStore, SparseCandidate
from eventide.rag.local_models import MAX_RERANK_CANDIDATES, LocalModelError

RRF_K = 60
MAX_RESULT_PASSAGES = 5
MAX_SNIPPET_CHARACTERS = 800
MAX_SOURCE_CHARACTERS = 4_000


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk: IndexedChunk
    score: float
    sources: tuple[str, ...]
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None
    freshness: str = "unverified"


class Reranker(Protocol):
    def score(self, query: str, passages: list[str]) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    results: tuple[SearchResult, ...]
    mode: str
    diagnostics: tuple[str, ...] = ()
    index_generation: int | None = None
    embedding_fingerprint: str | None = None


def verify_results(store: IndexStore, results: list[SearchResult]) -> list[SearchResult]:
    """Only final hits are read; stale bodies cannot leak into prompts or metadata."""
    checked: dict[tuple[str, str], str] = {}
    verified: list[SearchResult] = []
    for item in results[:MAX_RESULT_PASSAGES]:
        key = (item.chunk.file, item.chunk.content_hash)
        if key not in checked:
            checked[key] = store.file_freshness(*key)
        freshness = checked[key]
        chunk = item.chunk if freshness == "fresh" else replace(item.chunk, text="", heading="")
        verified.append(replace(item, chunk=chunk, freshness=freshness))
    return verified


def validate_k(k: int) -> int:
    if type(k) is not int or not 1 <= k <= 10:
        raise ValueError("k must be between 1 and 10")
    return k


def sparse_search(
    workspace: Path, query: str, *, k: int = 5, index_dir: Path | None = None,
) -> list[SearchResult]:
    """Run the P1 keyword-only retrieval path."""
    count = validate_k(k)
    store = IndexStore(workspace, index_dir=index_dir)
    candidates = store.sparse_search(query, limit=count)
    return verify_results(store, [
        SearchResult(
            chunk=candidate.chunk,
            score=-candidate.bm25,
            sources=("sparse",),
            sparse_rank=candidate.rank,
        )
        for candidate in candidates
    ])


def dense_search(
    workspace: Path,
    query: str,
    *,
    embedder: EmbeddingProvider,
    profile: EmbeddingProfile,
    k: int = 5,
    index_dir: Path | None = None,
) -> list[SearchResult]:
    """Embed a query and run the P2 exhaustive cosine path."""
    count = validate_k(k)
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Search query must not be empty")
    store = IndexStore(workspace, index_dir=index_dir)
    store.validate_profile(profile)
    vector = embed_query(embedder, cleaned)
    with store.snapshot() as reader:
        stored_profile = reader.validate_profile(profile)
        if stored_profile.dimension != len(vector):
            raise ValueError("Knowledge index vector dimension is inconsistent; rebuild --force")
        candidates = reader.dense_search(vector, limit=count)
    return verify_results(store, [
        SearchResult(
            chunk=candidate.chunk,
            score=candidate.similarity,
            sources=("dense",),
            dense_rank=candidate.rank,
        )
        for candidate in candidates
    ])


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
    index_dir: Path | None = None,
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
    with IndexStore(workspace, index_dir=index_dir).snapshot() as reader:
        return _hybrid_candidates(reader, cleaned, vector, profile, candidate_limit)


def _hybrid_candidates(
    reader: IndexStore, query: str, vector: list[float], profile: EmbeddingProfile, limit: int,
) -> list[SearchResult]:
    stored_profile = reader.validate_profile(profile)
    if stored_profile.dimension != len(vector):
        raise ValueError("Knowledge index vector dimension is inconsistent; rebuild --force")
    return _rrf_candidates(
        reader.dense_search(vector, limit=limit), reader.sparse_search(query, limit=limit),
        limit=limit,
    )


def hybrid_search(
    workspace: Path,
    query: str,
    *,
    embedder: EmbeddingProvider,
    profile: EmbeddingProfile,
    k: int = 5,
    index_dir: Path | None = None,
) -> list[SearchResult]:
    """Run dense and trigram/BM25 retrieval, then combine them with RRF."""
    count = validate_k(k)
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Search query must not be empty")
    store = IndexStore(workspace, index_dir=index_dir)
    # Fail before a potentially remote embedding request, then revalidate the
    # pinned snapshot in case a rebuild commits while the request is in flight.
    store.validate_profile(profile)
    vector = embed_query(embedder, cleaned)
    candidates = hybrid_candidates_from_vector(
        workspace, cleaned, vector, profile=profile, candidate_limit=count * 2,
        index_dir=index_dir,
    )[:count]
    return verify_results(store, candidates)


def retrieval_from_vector(
    workspace: Path, query: str, *, vector: list[float] | None,
    profile: EmbeddingProfile | None, reranker: Reranker | None = None,
    k: int = 5, diagnostics: tuple[str, ...] = (), index_dir: Path | None = None,
) -> RetrievalOutcome:
    """Pin evidence identity before ranking, then check only the final source files."""
    count = min(validate_k(k), MAX_RESULT_PASSAGES)
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Search query must not be empty")
    store = IndexStore(workspace, index_dir=index_dir)
    with store.snapshot() as reader:
        generation, fingerprint = reader.retrieval_identity()
        if vector is None:
            candidates = [SearchResult(
                chunk=item.chunk, score=-item.bm25, sources=("sparse",), sparse_rank=item.rank,
            ) for item in reader.sparse_search(cleaned, limit=count)]
            mode = "sparse"
        else:
            if profile is None:
                raise ValueError("An embedding profile is required with a query vector")
            if not vector or any(not math.isfinite(value) for value in vector):
                raise ValueError("Search needs a nonempty query and a finite embedding vector")
            candidates = _hybrid_candidates(
                reader, cleaned, vector, profile,
                MAX_RERANK_CANDIDATES if reranker is not None else count * 2,
            )
            mode = "dense+sparse"
    # No inference or filesystem read holds the SQLite transaction open.
    if vector is not None and reranker is not None and candidates:
        try:
            scores = reranker.score(query, [item.chunk.text for item in candidates])
            if len(scores) != len(candidates) or any(not math.isfinite(value) for value in scores):
                raise LocalModelError("local_reranker_invalid_response")
            ranked = sorted(enumerate(candidates), key=lambda item: (-scores[item[0]], item[0]))
            candidates = [
                replace(item, rerank_score=float(scores[index]), rerank_rank=rank)
                for rank, (index, item) in enumerate(ranked[:count], 1)
            ]
            mode = "dense+sparse+rerank"
        except Exception as exc:
            reason = exc.code if isinstance(exc, LocalModelError) else "local_reranker_failed"
            diagnostics = (*diagnostics, reason)
    results = verify_results(store, candidates[:count])
    return RetrievalOutcome(tuple(results), mode, diagnostics, generation, fingerprint)


def retrieve(
    workspace: Path, query: str, *, embedder: EmbeddingProvider,
    profile: EmbeddingProfile, reranker: Reranker | None = None, k: int = 5,
    index_dir: Path | None = None,
) -> RetrievalOutcome:
    """Synchronous local/development adapter; production can use retrieval_from_vector."""
    validate_k(k)
    if not query.strip():
        raise ValueError("Search query must not be empty")
    IndexStore(workspace, index_dir=index_dir).validate_profile(profile)
    try:
        vector = embed_query(embedder, query.strip())
    except LocalModelError as exc:
        return retrieval_from_vector(
            workspace, query, vector=None, profile=None, k=k, diagnostics=(exc.code,),
            index_dir=index_dir,
        )
    return retrieval_from_vector(
        workspace, query, vector=vector, profile=profile, reranker=reranker, k=k,
        index_dir=index_dir,
    )


def format_results(
    results: list[SearchResult], *, mode: str, diagnostics: tuple[str, ...] = (),
    source_budget: int = MAX_SOURCE_CHARACTERS,
) -> str:
    """Bound source text separately from citations; truncation never means a full file."""
    if type(source_budget) is not int or not 0 <= source_budget <= MAX_SOURCE_CHARACTERS:
        raise ValueError("source_budget must be between 0 and 4000 characters")
    selected = results[:MAX_RESULT_PASSAGES]
    remaining = source_budget
    lines = [f"Found {len(selected)} passages (mode: {mode}):"]
    if diagnostics:
        lines.append("Retrieval degraded: " + ", ".join(diagnostics))
    for index, result in enumerate(selected, 1):
        chunk = result.chunk
        source_text = "+".join(result.sources)
        lines.append(
            f"[{index}] {chunk.file}:{chunk.start_line}-{chunk.end_line} "
            f"(score {result.score:.4f}, {source_text})"
        )
        if result.freshness not in {"fresh", "unverified"}:
            lines.append(
                f"    [Evidence unavailable: {result.freshness}; indexed body withheld. "
                "Use read_file or search_text on the current source; "
                "indexed line range may differ.]"
            )
            continue
        if result.freshness == "fresh":
            lines.append("    [Content hash matched current source at check time.]")
        if result.rerank_score is not None:
            lines.append(f"    CrossEncoder score {result.rerank_score:.4f}; RRF score unchanged")
        snippet = chunk.text[:min(MAX_SNIPPET_CHARACTERS, remaining)]
        remaining -= len(snippet)
        lines.extend(f"    {line}" for line in snippet.splitlines())
        if len(snippet) < len(chunk.text):
            lines.append(
                f"    [Snippet truncated: {len(snippet)}/{len(chunk.text)} source characters; "
                "use read_file with the cited path and line range.]"
            )
    if len(selected) < len(results):
        lines.append(f"[Omitted {len(results) - len(selected)} additional passages; maximum 5.]")
    return "\n".join(lines)
