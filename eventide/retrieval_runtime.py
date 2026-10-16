"""Run-owned remote retrieval; policy is enforced by the existing ToolExecutor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from eventide.config import Settings
from eventide.embedding_runtime import embed_remote
from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import IndexStore
from eventide.rag.search import (
    Reranker,
    RetrievalOutcome,
    format_results,
    retrieval_from_vector,
    retrieve,
    validate_k,
)
from eventide.utils import owned_local_work

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


def runtime_search_handler(
    host: RuntimeHost, run_id: str, workspace: Path, profile: EmbeddingProfile,
    *, settings: Settings, reranker: Reranker | None = None,
    embedder: EmbeddingProvider | None = None,
) -> Callable[..., Awaitable[str]]:
    """Freeze endpoint settings, validate index before spending, revalidate after response."""
    async def search(query: str, k: int = 5) -> RetrievalOutcome:
        validate_k(k)
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("Search query must not be empty")
        if profile.provider != "openai_compatible":
            if embedder is None:
                raise ValueError("A local embedder is required")
            return await owned_local_work(retrieve, workspace, cleaned, embedder=embedder,
                                          profile=profile, reranker=reranker, k=k)
        await owned_local_work(IndexStore(workspace).validate_profile, profile)
        vectors = await embed_remote(host, run_id, [cleaned], settings=settings)
        return await owned_local_work(
            retrieval_from_vector, workspace, cleaned, vector=vectors[0], profile=profile,
            reranker=reranker, k=k,
        )

    if settings.retrieval_query_rewrite:
        from eventide.retrieval_rewrite import with_query_rewrite

        return with_query_rewrite(host, run_id, workspace, search)

    async def search_knowledge(query: str, k: int = 5) -> str:
        outcome = await search(query, k)
        return format_results(list(outcome.results), mode=outcome.mode,
                              diagnostics=outcome.diagnostics)

    return search_knowledge
