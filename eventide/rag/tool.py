"""Runtime schema and handler for knowledge search."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from eventide.models import ToolOutput
from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import IndexStore
from eventide.rag.search import (
    MAX_RESULT_PASSAGES,
    Reranker,
    RetrievalOutcome,
    format_results,
    retrieval_from_vector,
    retrieve,
)


def retrieval_output(
    outcome: RetrievalOutcome, *, workspace_id: str | None = None,
) -> ToolOutput:
    """Preserve provenance alongside the bounded model preview in the existing tool event."""
    rendered = format_results(
        list(outcome.results), mode=outcome.mode, diagnostics=outcome.diagnostics,
    )
    identity = (
        f"Workspace: {workspace_id or 'unrecorded'}; "
        f"index generation: {outcome.index_generation}; "
        f"embedding fingerprint: {outcome.embedding_fingerprint}."
    )
    return ToolOutput(identity + "\n" + rendered, metadata={"retrieval": {
        "workspace_id": workspace_id,
        "index_generation": outcome.index_generation,
        "embedding_fingerprint": outcome.embedding_fingerprint,
        "mode": outcome.mode,
        "diagnostics": list(outcome.diagnostics),
        "hits": [{
            "file": item.chunk.file,
            "start_line": item.chunk.start_line,
            "end_line": item.chunk.end_line,
            "indexed_content_hash": item.chunk.content_hash,
            "freshness": item.freshness,
            "sources": list(item.sources),
        } for item in outcome.results[:MAX_RESULT_PASSAGES]],
    }})

SEARCH_KNOWLEDGE_TOOL = {
    "name": "search_knowledge",
    "description": (
        "Search the current Workspace knowledge index and return citation-ready passages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
    },
}


def index_available(
    workspace: Path, profile: EmbeddingProfile, *, index_dir: Path | None = None,
) -> bool:
    """Check local schema/fingerprint validity without embedding or network access."""
    if not IndexStore(workspace, index_dir=index_dir).path.is_file():
        return False
    try:
        IndexStore(workspace, index_dir=index_dir).validate_profile(profile)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return False
    return True


def handler(
    workspace: Path,
    embedder: EmbeddingProvider,
    profile: EmbeddingProfile,
    *, reranker: Reranker | None = None, index_dir: Path | None = None,
) -> Callable[..., str]:
    """Bind search to the current Run's Workspace and embedding identity."""

    def search_knowledge(query: str, k: int = 5) -> str:
        if not IndexStore(workspace, index_dir=index_dir).path.is_file():
            return "Error: no knowledge index; build it in the Web knowledge index page"
        outcome = retrieve(
            workspace,
            query,
            k=k,
            embedder=embedder,
            profile=profile,
            reranker=reranker, index_dir=index_dir,
        )
        return format_results(
            list(outcome.results), mode=outcome.mode, diagnostics=outcome.diagnostics,
        )

    return search_knowledge


def sparse_handler(
    workspace: Path, reason: str, *, index_dir: Path | None = None,
) -> Callable[..., str]:
    """Expose a diagnosed lexical fallback when a prepared model is unavailable."""
    # Verify the index and its filesystem boundary before advertising the tool.
    IndexStore(workspace, index_dir=index_dir).sparse_search("eventide", limit=1)

    def search_knowledge(query: str, k: int = 5) -> str:
        outcome = retrieval_from_vector(
            workspace, query, vector=None, profile=None, k=k, diagnostics=(reason,),
            index_dir=index_dir,
        )
        return format_results(list(outcome.results), mode=outcome.mode,
                              diagnostics=outcome.diagnostics)

    return search_knowledge
