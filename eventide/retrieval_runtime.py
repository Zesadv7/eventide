"""Run-owned remote retrieval; policy is enforced by the existing ToolExecutor."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.config import Settings
from eventide.embedding_runtime import embed_remote
from eventide.models import ToolOutput, WorkspaceRecord
from eventide.rag.chunker import chunking_identity
from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider, provider_from_settings
from eventide.rag.index import IndexStore
from eventide.rag.search import (
    Reranker,
    RetrievalOutcome,
    retrieval_from_vector,
    retrieve,
    validate_k,
)
from eventide.rag.source_selection import source_selection_identity
from eventide.utils import owned_local_work
from eventide.workspace import digest

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


def configuration(settings: Settings) -> dict[str, Any]:
    """Public identity; credentials never enter an admission or status event."""
    _, profile = provider_from_settings(settings)
    public = {"provider": profile.provider, "model": profile.model,
              "algorithm_version": profile.algorithm_version, "dimension": profile.dimension}
    return {**public, "config_hash": digest({**public, "endpoint": profile.base_url})}


def freeze_configuration(
    settings: Settings, option: str, *, code_structure: bool = False, source_scope: str = "code",
) -> dict[str, Any]:
    if option not in {"existing", "prepare"}:
        raise ValueError("knowledge_index must be existing or prepare")
    if type(code_structure) is not bool:
        raise ValueError("code_structure must be a boolean")
    if source_scope not in {"code", "all"}:
        raise ValueError("source_scope must be code or all")
    preparation = {
        "code_structure": code_structure, "source_scope": source_scope,
        "index_configuration": {
            "chunking_profile": chunking_identity(code_structure=code_structure),
            "source_selection_profile": source_selection_identity(source_scope),
        },
    }
    try:
        config = configuration(settings)
    except (OSError, ValueError, RuntimeError) as exc:
        if option == "prepare":
            raise
        # An unavailable optional local model must remain visible without disabling file tools.
        return {"knowledge_index": option, "retrieval_configuration": {"error": str(exc)},
                **preparation}
    return {"knowledge_index": option, "retrieval_configuration": config, **preparation}


def validate_frozen_configuration(settings: Settings, intent: dict[str, Any]) -> dict[str, Any]:
    """Revalidate recorded preparation rules without inventing historical identities."""
    option = intent["knowledge_index"]
    current = freeze_configuration(
        settings, option, code_structure=intent.get("code_structure", False),
        source_scope=intent.get("source_scope", "code"),
    )
    if current["retrieval_configuration"] != intent["retrieval_configuration"]:
        raise ValueError("Embedding configuration changed since admission")
    if "index_configuration" in intent:
        if current["index_configuration"] != intent["index_configuration"]:
            raise ValueError("Index preparation rules changed; start a new Run")
        return current
    if option == "prepare":
        raise ValueError("Historical index preparation rules were not recorded; start a new Run")
    return {"knowledge_index": option, "retrieval_configuration": intent["retrieval_configuration"]}


def managed_index_dir(host: RuntimeHost, workspace: WorkspaceRecord) -> Path:
    return host.state_dir / "indexes" / workspace.workspace_id


def validate_index_source(root: Path) -> None:
    if not root.is_dir() or root.resolve() != root:
        raise ValueError("Index Workspace missing or path identity changed")


def validate_preparation(host: RuntimeHost, workspace: WorkspaceRecord) -> None:
    validate_index_source(Path(workspace.path))
    if host.state_dir.resolve().is_relative_to(Path(workspace.path).resolve()):
        raise ValueError("Index preparation requires the service state directory outside source")


def index_location(host: RuntimeHost, workspace: WorkspaceRecord) -> tuple[Path, str]:
    managed = managed_index_dir(host, workspace)
    # lexists semantics: a broken managed link must not trigger legacy fallback.
    path = managed / "index.sqlite"
    if path.exists() or path.is_symlink() or managed.is_symlink():
        return managed, "managed"
    return Path(workspace.path) / ".eventide", "legacy"


def index_status(host: RuntimeHost, workspace: WorkspaceRecord) -> dict[str, Any]:
    directory, location = index_location(host, workspace)
    result: dict[str, Any] = {"status": "unavailable", "workspace_id": workspace.workspace_id,
                              "location": location}
    try:
        validate_index_source(Path(workspace.path))
        result.update(configuration(host.settings))
        _, profile = provider_from_settings(host.settings)
        index = IndexStore(Path(workspace.path), index_dir=directory)
        with index.snapshot() as snapshot:
            stored = snapshot.validate_profile(profile)
            result.update(status="available", generation=snapshot.generation(),
                          embedding_fingerprint=stored.fingerprint())
    except FileNotFoundError:
        result["reason"] = "index_missing"
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        result.update(reason="index_unavailable", detail=host._clean(str(exc)))
    return result


async def prepare_index(
    host: RuntimeHost, run_id: str, workspace: WorkspaceRecord, *,
    settings: Settings, force: bool = False, code_structure: bool = False,
    source_scope: str = "code", frozen_configuration: dict[str, Any] | None = None,
) -> Any:
    """Manual and in-task builds share publication ownership, outside model slots."""
    from eventide.index_runtime import build_index

    frozen = frozen_configuration if frozen_configuration is not None else freeze_configuration(
        settings, "prepare", code_structure=code_structure, source_scope=source_scope,
    )
    validate_preparation(host, workspace)
    async with host._index_build_locks[workspace.workspace_id]:
        validate_preparation(host, workspace)
        validate_frozen_configuration(settings, frozen)
        embedder, profile = provider_from_settings(settings)
        previous, _ = index_location(host, workspace)
        if not force and (previous / "index.sqlite").exists():
            await owned_local_work(
                IndexStore(Path(workspace.path), index_dir=previous).validate_build_configuration,
                profile, code_structure=code_structure, source_scope=source_scope,
            )
        return await build_index(host, run_id, Path(workspace.path), settings=settings,
                                 profile=profile, embedder=embedder, force=force,
                                 code_structure=code_structure, source_scope=source_scope,
                                 index_dir=managed_index_dir(host, workspace))


async def configure_run_retrieval(
    host: RuntimeHost, run_id: str, workspace: WorkspaceRecord,
    intent: dict[str, Any], *, supported: bool, sink: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from eventide.rag.local_models import LocalModelError, reranker_from_settings
    from eventide.rag.tool import SEARCH_KNOWLEDGE_TOOL

    state: dict[str, Any] = {"status": "unavailable", "workspace_id": workspace.workspace_id}
    async def publish() -> None:
        await host._emit(run_id, "retrieval.status", host._clean(state), sink)

    if not supported:
        state["reason"] = "unsupported_execution"
        await publish()
        return [], {}
    option = intent.get("knowledge_index", "existing")
    frozen = intent.get("retrieval_configuration")
    try:
        validate_index_source(Path(workspace.path))
        current = configuration(host.settings)
        if frozen is not None:
            validate_frozen_configuration(host.settings, intent)
        if option == "prepare":
            state.update(status="preparing", location="managed", model=current["model"])
            await publish()
            stats = await prepare_index(
                host, run_id, workspace, settings=host.settings,
                code_structure=intent["code_structure"], source_scope=intent["source_scope"],
                frozen_configuration=intent,
            )
            state["build"] = asdict(stats)
        status = await owned_local_work(index_status, host, workspace)
        state.update(status)
        if status["status"] != "available":
            if option == "prepare":
                raise ValueError(status.get("detail", status["reason"]))
            await publish()
            return [], {}
        embedder, profile = provider_from_settings(host.settings)
        reranker = reranker_from_settings(host.settings)
        directory, _ = index_location(host, workspace)
        handler = runtime_search_handler(host, run_id, Path(workspace.path), profile,
                                         settings=host.settings, reranker=reranker,
                                         embedder=embedder, index_dir=directory,
                                         workspace_id=workspace.workspace_id)
        await publish()
        return [SEARCH_KNOWLEDGE_TOOL], {"search_knowledge": handler}
    except asyncio.CancelledError:
        state.update(status="failed", reason="cancelled")
        await publish()
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        from eventide.budgets import BudgetExceeded
        if isinstance(exc, BudgetExceeded):
            state.update(status="failed", reason="token_budget")
            await publish()
            raise
        if isinstance(exc, LocalModelError) and option == "existing":
            directory, location = index_location(host, workspace)
            try:
                index = IndexStore(Path(workspace.path), index_dir=directory)
                with index.snapshot() as snapshot:
                    profile = snapshot.embedding_profile()
                    generation = snapshot.generation()
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as unavailable:
                state["fallback_error"] = host._clean(str(unavailable))
            else:
                reason = exc.code

                async def sparse(query: str, k: int = 5) -> ToolOutput:
                    from eventide.rag.tool import retrieval_output

                    validate_index_source(Path(workspace.path))
                    outcome = await owned_local_work(
                        retrieval_from_vector, Path(workspace.path), query, vector=None,
                        profile=None, k=k, diagnostics=(reason,), index_dir=directory,
                    )
                    return retrieval_output(outcome, workspace_id=workspace.workspace_id)

                state.update(status="available", reason=reason, mode="sparse", location=location,
                             generation=generation, embedding_fingerprint=profile.fingerprint())
                await publish()
                await host._emit(run_id, "retrieval.degraded", {
                    "reason": reason, "fallback": "sparse",
                }, sink)
                return [SEARCH_KNOWLEDGE_TOOL], {"search_knowledge": sparse}
        state.update(status="failed" if option == "prepare" else "unavailable",
                     reason=exc.code if isinstance(exc, LocalModelError) else "index_unavailable",
                     detail=host._clean(str(exc)))
        await publish()
        if option == "prepare":
            raise
        return [], {}


def runtime_search_handler(
    host: RuntimeHost, run_id: str, workspace: Path, profile: EmbeddingProfile,
    *, settings: Settings, reranker: Reranker | None = None,
    embedder: EmbeddingProvider | None = None,
    index_dir: Path | None = None, workspace_id: str,
) -> Callable[..., Awaitable[ToolOutput]]:
    """Freeze endpoint settings, validate index before spending, revalidate after response."""
    async def search(query: str, k: int = 5) -> RetrievalOutcome:
        validate_index_source(workspace)
        validate_k(k)
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("Search query must not be empty")
        if profile.provider != "openai_compatible":
            if embedder is None:
                raise ValueError("A local embedder is required")
            return await owned_local_work(retrieve, workspace, cleaned, embedder=embedder,
                                          profile=profile, reranker=reranker, k=k,
                                          index_dir=index_dir)
        await owned_local_work(IndexStore(workspace, index_dir=index_dir).validate_profile, profile)
        vectors = await embed_remote(host, run_id, [cleaned], settings=settings)
        return await owned_local_work(
            retrieval_from_vector, workspace, cleaned, vector=vectors[0], profile=profile,
            reranker=reranker, k=k, index_dir=index_dir,
        )

    if settings.retrieval_query_rewrite:
        from eventide.retrieval_rewrite import with_query_rewrite

        return with_query_rewrite(host, run_id, workspace, search, index_dir=index_dir,
                                  workspace_id=workspace_id)

    async def search_knowledge(query: str, k: int = 5) -> ToolOutput:
        outcome = await search(query, k)
        from eventide.rag.tool import retrieval_output
        return retrieval_output(outcome, workspace_id=workspace_id)

    return search_knowledge
