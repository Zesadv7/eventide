"""Runtime schema and handler for knowledge search."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import INDEX_RELATIVE_PATH, IndexStore
from eventide.rag.search import format_results, hybrid_search

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


def index_available(workspace: Path, profile: EmbeddingProfile) -> bool:
    """Check local schema/fingerprint validity without embedding or network access."""
    if not (workspace / INDEX_RELATIVE_PATH).is_file():
        return False
    try:
        IndexStore(workspace).validate_profile(profile)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return False
    return True


def handler(
    workspace: Path,
    embedder: EmbeddingProvider,
    profile: EmbeddingProfile,
) -> Callable[..., str]:
    """Bind search to the current Run's Workspace and embedding identity."""

    def search_knowledge(query: str, k: int = 5) -> str:
        if not (workspace / INDEX_RELATIVE_PATH).is_file():
            return "Error: no knowledge index; run 'eventide rag build'"
        results = hybrid_search(
            workspace,
            query,
            k=k,
            embedder=embedder,
            profile=profile,
        )
        return format_results(results, mode="dense+sparse")

    return search_knowledge
