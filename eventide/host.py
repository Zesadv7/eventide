"""Single execution owner for Workspace-bound sessions and canonical facts."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from eventide import attachments, model_catalog, run_policy
from eventide.budget_prices import PriceVersion
from eventide.budgets import BudgetExceeded, BudgetScope, is_service_probe_run
from eventide.config import SUPPORTED_PROVIDERS, Settings, normalize_provider
from eventide.context_builder import ContextBuilder, request_size, tool_catalog_size
from eventide.execution_snapshots import ExecutionSnapshots
from eventide.execution_worktrees import ExecutionWorktrees
from eventide.executor import (
    ApprovalHandler,
    CommandExecutor,
    CoordinatedCommandExecutor,
    LocalCommandExecutor,
    ToolContext,
    ToolExecutor,
)
from eventide.generation import (
    GenerationCapabilities,
    GenerationPatch,
    capabilities,
    resolve_generation,
)
from eventide.mcp.client import MCPManager
from eventide.model_budget import ModelBudgetController
from eventide.models import (
    ContinuationRequest,
    ModelRequest,
    ModelResponse,
    RunRequest,
    RunResult,
    ToolCall,
    WorkspaceRecord,
    WorkspaceTarget,
)
from eventide.normalization import normalize, redact
from eventide.policy import PolicyEngine
from eventide.previews import PreviewStore, SafeTextBuffer
from eventide.projections import TaskPlanProjection
from eventide.provider_config import apply_provider_configuration
from eventide.provider_probe import run_probe
from eventide.providers import Provider, ProviderError, build_provider
from eventide.rag.embedding import provider_from_settings
from eventide.rag.index import INDEX_RELATIVE_PATH
from eventide.rag.local_models import LocalModelError, reranker_from_settings
from eventide.rag.tool import SEARCH_KNOWLEDGE_TOOL
from eventide.rag.tool import handler as knowledge_search_handler
from eventide.rag.tool import index_available as knowledge_index_available
from eventide.rag.tool import sparse_handler as knowledge_sparse_handler
from eventide.retrieval_runtime import remote_search_handler
from eventide.run_dispatch import RunDispatch
from eventide.secrets import SecretBox, SecretKeyError
from eventide.skill_packages import SkillPackageError
from eventide.skill_runtime import SkillRuntime, execute_skill_script, script_tool
from eventide.skills import SkillCatalog
from eventide.store import RuntimeStore
from eventide.task_plan import TOOL as TODO_WRITE_TOOL
from eventide.task_plan import active_id as task_plan_active
from eventide.task_plan import plan_update
from eventide.task_plan import prompt as task_plan_prompt
from eventide.task_plan import summary as plan_summary
from eventide.tool_contracts import production_tool_catalog
from eventide.tool_results import TOOL as READ_TOOL_RESULT_TOOL
from eventide.tool_results import reader as tool_result_reader
from eventide.tools.runtime_catalog import HANDLERS, TOOLS
from eventide.usage import build_usage_ledger
from eventide.workers import (
    READONLY_WORKER_TOOL,
    ReadonlyProfile,
    SourceSnapshot,
    validate_report,
    validate_tasks,
)
from eventide.workspace import (
    HostLease,
    canonical_workspace,
    changed_since,
    digest,
    git_metadata,
    runtime_checkpoint_directories,
    runtime_checkpoint_files,
    session_working_directory,
    workspace_checkpoint,
)
from eventide.worktrees import WorktreeService

# stop_reason values that mean the provider cut the answer off at max_tokens.
TRUNCATION_REASONS = {"max_tokens", "length", "max_output_tokens", "incomplete"}
READONLY_WORKER_TIMEOUT = 60.0
BASE_RUNTIME_READONLY_TOOLS = frozenset(
    {"read_file", "glob", "compact", "read_tool_result", "todo_write", "search_knowledge"}
)


@dataclass(slots=True)
class _Admission:
    session_id: str
    done: asyncio.Event


class _RunTokenBudgetExceeded(RuntimeError):
    pass


class _UnknownToolOutcome(RuntimeError):
    pass


class EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class SessionManager:
    def __init__(self, store: RuntimeStore):
        self.store = store

    def create(
        self,
        workspace_id: str,
        session_id: str | None = None,
        working_directory: str | Path | None = None,
    ) -> str:
        identity = session_id or f"session_{uuid.uuid4().hex[:16]}"
        if self.store.get_session(identity) and working_directory is None:
            self.store.create_session(identity, workspace_id)
            return identity
        workspace = self.store.get_workspace(workspace_id)
        relative, _ = session_working_directory(Path(workspace.path), working_directory)
        self.store.create_session(
            identity,
            workspace_id,
            working_directory=relative,
        )
        return identity

    def workspace(self, session_id: str) -> WorkspaceRecord:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        return self.store.get_workspace(session["workspace_id"])

    def working_directory(self, session_id: str) -> Path:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        workspace = self.store.get_workspace(session["workspace_id"])
        _, path = session_working_directory(Path(workspace.path), session["working_directory"])
        return path


class ProviderCleanupError(RuntimeError):
    """Old transport cleanup failed before a configuration write was attempted."""


class RuntimeHost:
    def __init__(
        self,
        settings: Settings | None = None,
        provider: Provider | None = None,
        store: RuntimeStore | None = None,
        tools: list[dict[str, Any]] | None = None,
        handlers: dict[str, Any] | None = None,
        *,
        enable_mcp: bool = True,
    ):
        self.settings = settings or Settings.from_env()
        self._startup_settings = self.settings
        self._owns_store = store is None
        self._worktree_removals: set[Path] = set()
        self._lease = HostLease(store.path.parent if store else self.settings.state_dir)
        try:
            self.store = store or RuntimeStore(self.settings.state_dir / "runtime.sqlite")
            self._checkpoint_files = runtime_checkpoint_files(self.store.path, self.state_dir)
            self._checkpoint_directories = runtime_checkpoint_directories(self.state_dir)
            self.execution_snapshots = ExecutionSnapshots(
                self.state_dir, checkpoint_files=self._checkpoint_files,
                checkpoint_directories=self._checkpoint_directories,
            )
            self.execution_worktrees = ExecutionWorktrees(self.state_dir)
            self._recover_interrupted_runs()
            self._secret_box = SecretBox(self.settings.state_dir)
            self._provider_secret_error: str | None = None
            self._load_persisted_provider()
            self.sessions = SessionManager(self.store)
            self.context_builder = ContextBuilder(self.store)
            self.default_workspace = self.resolve_or_register_workspace(self.settings.workdir)
        except BaseException:
            if self._owns_store and hasattr(self, "store"):
                self.store.close()
            self._lease.close()
            raise
        self.provider = provider
        self.tools = production_tool_catalog(list(TOOLS if tools is None else tools))
        self.handlers = dict(HANDLERS if handlers is None else handlers)
        self.policy = PolicyEngine()
        self.executor = ToolExecutor(
            self.handlers,
            command_executor=LocalCommandExecutor(self.settings.command_timeout),
            tools=self.tools,
        )
        self._workspace_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._snapshot_preparing: set[str] = set()
        self._project_git_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._admissions: dict[str, str] = {}
        self._admission_done: dict[str, _Admission] = {}
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._closed = False
        self._configuring = False
        self._provider_cleanup_required = False
        self._configuration_uncertain = False
        self._config_path: Path | None = None
        self._enable_mcp = enable_mcp
        self.previews = PreviewStore()
        self.skills = SkillRuntime(self.store, self.state_dir)
        self.model_budget = ModelBudgetController(self.store)
        self.dispatch = RunDispatch(self)

    def _budget_control_session(self) -> str:
        session_id = self.create_session(f"budgets_{self.default_workspace.workspace_id}")
        if not self.is_control_session(session_id):
            self.store.append_fact(session_id, "control.session", {"kind": "budgets"})
            self.store.update_session(session_id, title="Budget 管理", archived=True)
        return session_id

    def configure_budget(
        self, scope: BudgetScope, *, token_limit: int | None, request_id: str,
    ) -> dict[str, Any]:
        """Explicit Python control interface; zero blocks, None disables a limit."""
        if scope.kind == "run" and self.store.get_run_identity(scope.identity) is None:
            raise ValueError("Run not found")
        if scope.kind == "project":
            self.store.get_project(scope.identity)
        if scope.kind == "delivery":
            from eventide.delivery_runtime import DeliveryController

            DeliveryController(self).load(scope.identity)
        return self.model_budget.ledger.configure(
            self._budget_control_session(), scope=scope, token_limit=token_limit,
            request_id=request_id,
        )

    def budget_status(self, *, scopes: tuple[BudgetScope, ...] = ()) -> dict[str, Any]:
        return self.model_budget.ledger.snapshot(scopes=scopes)

    def register_price(self, price: PriceVersion) -> dict[str, Any]:
        return self.model_budget.costs.register_price(self._budget_control_session(), price)

    def cost_status(self) -> dict[str, Any]:
        return self.model_budget.costs.snapshot()

    @property
    def state_dir(self) -> Path:
        """State root for runtime-owned session files such as attachments."""
        return self.settings.state_dir

    def _workspace_checkpoint(self, root: Path) -> dict[str, Any] | None:
        return workspace_checkpoint(
            root, exclude_untracked=self._checkpoint_files,
            exclude_untracked_directories=self._checkpoint_directories,
        )

    def _provider(self) -> Provider:
        if self._provider_cleanup_required or self._configuration_uncertain:
            raise ValueError("Provider configuration requires recovery")
        if self.provider is None:
            self.provider = build_provider(self.settings)
        return self.provider

    def resolve_or_register_workspace(
        self, target: WorkspaceTarget | Path | str
    ) -> WorkspaceRecord:
        if isinstance(target, WorkspaceTarget):
            if bool(target.workspace_id) == bool(target.path):
                raise ValueError("Choose exactly one workspace ID or path")
            if target.workspace_id:
                record = self.store.get_workspace(target.workspace_id)
                self._check_worktree_admission(Path(record.path))
                return record
            target = str(target.path)
        if isinstance(target, str) and target.startswith("ws_"):
            record = self.store.get_workspace(target)
            self._check_worktree_admission(Path(record.path))
            return record
        root, git_root = canonical_workspace(Path(target))
        self._check_worktree_admission(root)
        return self.store.register_workspace(root, git_root)

    def _check_worktree_admission(self, root: Path) -> None:
        if any(root.is_relative_to(path) for path in self._worktree_removals):
            raise ValueError("Worktree is being removed; Workspace/Session admission is closed")

    def create_session(
        self,
        session_id: str | None = None,
        *,
        workspace_id: str | None = None,
        working_directory: str | Path | None = None,
    ) -> str:
        existing = self.store.get_session(session_id) if session_id else None
        target_workspace = (
            workspace_id
            or (existing["workspace_id"] if existing else None)
            or self.default_workspace.workspace_id
        )
        self._check_worktree_admission(Path(self.store.get_workspace(target_workspace).path))
        return self.sessions.create(
            target_workspace,
            session_id,
            working_directory if working_directory is not None else None,
        )

    async def manage_worktree(
        self,
        target: Path | str,
        action: str,
        *,
        name: str | None = None,
        ref: str = "HEAD",
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """User control-plane operations; never advertised as model tools."""
        if self._closed:
            raise ValueError("RuntimeHost is closed")
        if action not in {"list", "create", "keep", "remove"}:
            raise ValueError(f"Unknown worktree action: {action}")
        if action != "list" and name is None:
            raise ValueError("Worktree name is required")
        workspace = self.resolve_or_register_workspace(target)
        service = WorktreeService(Path(workspace.path), self.state_dir, workspace.workspace_id)
        task = asyncio.current_task()
        assert task is not None
        self._active_tasks.add(task)
        removal_path: Path | None = None
        try:
            async with (
                self._workspace_execution(workspace.workspace_id),
                self._project_git(workspace.project_id),
            ):
                if action != "list" and self.store.workspace_has_unknown_outcome(
                    workspace.workspace_id
                ):
                    raise ValueError("Workspace has an unresolved tool outcome")
                records = await asyncio.to_thread(service.list)
                if action == "list":
                    return [record.to_dict() for record in records]
                if action == "remove":
                    record = next((item for item in records if item.name == name), None)
                    if record is None:
                        raise ValueError(f"Managed worktree not found: {name}")
                    for checkout in self.store.list_workspaces():
                        if checkout.path == record.path and (
                            self.store.list_sessions(
                                workspace_id=checkout.workspace_id, include_archived=True,
                            ) or self.store.workspace_has_execution_history(checkout.workspace_id)
                            or self.workspace_busy(checkout.workspace_id)
                        ):
                            raise ValueError(
                                "Worktree has Session history; keep it for review and recovery"
                            )
                    # Admission is synchronous, so reserve before the next await.
                    removal_path = Path(record.path)
                    self._worktree_removals.add(removal_path)
                session_id = self.create_session(
                    f"worktrees_{workspace.workspace_id}", workspace_id=workspace.workspace_id
                )
                self.store.update_session(session_id, title="Worktree 管理")
                run_id = f"run_{uuid.uuid4().hex[:16]}"
                self.store.create_run(run_id, session_id)
                intent = {"action": action, "name": name, "ref": ref}
                await self._emit(
                    run_id,
                    "run.started",
                    {
                        "workspace_id": workspace.workspace_id,
                        "project_id": workspace.project_id,
                        "source_workspace_id": workspace.source_workspace_id,
                        "source": "worktree",
                    },
                    None,
                    author="user",
                )
                await self._emit(run_id, "worktree.requested", intent, None, author="user")
                cancelled = False
                try:
                    assert name is not None
                    operation = asyncio.create_task(
                        asyncio.to_thread(service.create, name, ref)
                        if action == "create"
                        else asyncio.to_thread(
                            service.keep if action == "keep" else service.remove, name
                        )
                    )
                    # Retain ownership of Git writes until their outcome is known, even on Stop.
                    while True:
                        try:
                            result = (await asyncio.shield(operation)).to_dict()
                            break
                        except asyncio.CancelledError:
                            cancelled = True
                    if action == "remove":
                        for checkout in self.store.list_workspaces():
                            if checkout.path == result["path"]:
                                self.store.remove_workspace(checkout.workspace_id)
                    elif action == "create":
                        checkout = self.resolve_or_register_workspace(result["path"])
                        if checkout.project_id != workspace.project_id:
                            raise ValueError("Managed worktree has a different project identity")
                    await self._emit(run_id, "worktree.completed", result, None, author="user")
                    await self._emit(
                        run_id,
                        "run.completed",
                        {
                            "output": f"Worktree {action}: {result['name']} ({result['path']})",
                            "steps": 0,
                            "tool_calls": 0,
                            "usage": {},
                        },
                        None,
                    )
                except Exception as exc:
                    await self._emit(
                        run_id,
                        "run.failed",
                        {"error": self._clean(str(exc))},
                        None,
                    )
                    raise
                if cancelled:
                    raise asyncio.CancelledError
                return result
        finally:
            if removal_path is not None:
                self._worktree_removals.discard(removal_path)
            self._active_tasks.discard(task)

    def session_status(self, session_id: str) -> dict[str, Any]:
        workspace = self.sessions.workspace(session_id)
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        run = self.store.latest_run_summary(session_id)
        activity = self.store.session_activity(session_id)
        first_intent = activity["first_intent"]
        task_state = TaskPlanProjection.project(self.store.task_events(session_id))
        title = (
            str(session.get("title") or first_intent or "New session").replace("\n", " ").strip()
        )
        return {
            "session_id": session_id,
            "workspace_id": workspace.workspace_id,
            "project_id": workspace.project_id,
            "source_workspace_id": workspace.source_workspace_id,
            "working_directory": str(Path(workspace.path) / session["working_directory"]),
            "created_at": session["created_at"],
            "updated_at": activity["updated_at"],
            "title": title[:96],
            "archived": bool(session.get("archived", False)),
            "status": "parked"
            if run and run["status"] == "interrupted"
            else run["status"]
            if run
            else "idle",
            "latest_run": run,
            "execution": self.run_execution(run["id"]) if run else {"kind": "source"},
            "task_plan": self.store.task_plan(session_id) or [],
            "active_task_id": task_state["active_task_id"],
            "task_state": task_state,
        }

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.store.get_workspace(workspace_id)
        metadata = git_metadata(Path(workspace.path))
        return {**asdict(workspace), **metadata}

    def project_status(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        source = self.store.get_workspace(project.source_workspace_id)
        return {
            **asdict(project),
            "source_path": source.path,
            "workspace_count": len(self.store.list_project_workspaces(project_id)),
            "session_count": len(self.store.list_project_sessions(project_id)),
        }

    def workspace_capabilities(self, workspace_id: str) -> dict[str, Any]:
        """Read-only capability introspection built from pure file parsing.

        MCP configurations are parsed, never connected. Only names and
        transports are reported: commands, environment variables, headers, and
        credentials stay out of the response because they are secret-bearing.
        """
        workspace = self.store.get_workspace(workspace_id)
        workspace_root = Path(workspace.path)
        catalog = SkillCatalog.discover(workspace_root)
        # Same resolution rule as _execute: an explicit config path belongs to
        # the default workspace only; every other workspace owns its mcp.json.
        mcp_config = (
            self._config_path
            if self._config_path and workspace == self.default_workspace
            else workspace_root / "mcp.json"
        )
        notes: list[str] = []
        servers: list[dict[str, str]] = []
        if not mcp_config.exists():
            notes.append("未配置 mcp.json")
        else:
            try:
                configs = MCPManager.load_configs(mcp_config)
            except (OSError, ValueError) as exc:
                notes.append(f"mcp.json 解析失败：{type(exc).__name__}: {exc}")
            else:
                servers = [
                    {"name": config.name, "transport": config.transport} for config in configs
                ]
        return {
            "workspace_id": workspace.workspace_id,
            "readonly_workers": {
                "enabled": bool(self.settings.workers_enabled and workspace.git_root),
                "roles": ["researcher", "reviewer"],
                "max_concurrency": 2,
                "max_depth": 1,
                "max_tasks_per_run": 4,
            },
            "skills": [
                {"name": skill.name, "description": skill.description} for skill in catalog.skills
            ],
            "mcp": servers,
            "paths": {
                "mcp_config": str(mcp_config),
                "skills_dir": str(workspace_root / "skills"),
            },
            "notes": notes,
        }

    async def available_models(self) -> dict[str, Any]:
        """Best-effort model ids for the composer picker; provider failures
        degrade to an empty list with a note instead of raising."""
        ids, error = await model_catalog.list_models(
            self.settings.provider, self.settings.api_key, self.settings.base_url
        )
        return {"models": ids, "error": error}

    def session_runs(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        self.sessions.workspace(session_id)
        return self.store.list_runs(session_id, limit=limit, before=before)

    async def initialize(self, config_path: Path | None = None) -> None:
        if self._closed:
            raise RuntimeError("Host is closed")
        if config_path:
            self._config_path = config_path
        # MCP transports are opened/closed in the owning run task (SDK task-affinity).
        # Each workspace reads its own catalog, never another project's MCP configuration.

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = [t for t in self._active_tasks if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for run_id in list(self._admission_done):
            self._finish_admitted(run_id, asyncio.CancelledError())
            self._release_admission(run_id)
            self.dispatch.finished(run_id)
        try:
            await self._close_provider()
        finally:
            if self._owns_store:
                self.store.close()
            self._lease.close()

    async def _close_provider(self) -> None:
        if self.provider is None:
            return
        closer = getattr(self.provider, "close", None)
        if closer is None:
            closer = getattr(self.provider, "aclose", None)
        self._provider_cleanup_required = True
        if closer is not None:
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                raise ProviderCleanupError("Provider client cleanup failed") from None
        self.provider = None
        self._provider_cleanup_required = False

    def _load_persisted_provider(self) -> None:
        resolved = apply_provider_configuration(
            self._startup_settings,
            self.store.get_provider_config(),
            self._secret_box,
        )
        self.settings = resolved.settings
        self._provider_secret_error = resolved.secret_error

    def configuration_owner(self) -> str:
        owner = "configuration_control"
        if self.store.get_session(owner) is None:
            self.create_session(owner)
            self.store.append_fact(owner, "control.session", {"kind": "configuration"})
            self.store.update_session(owner, title="Configuration", archived=True)
        elif not any(e["payload"].get("kind") == "configuration"
                     for e in self.store.facts_by_type(("control.session",))
                     if e["session_id"] == owner):
            raise ValueError("Configuration control identity conflict")
        return owner

    def provider_config_status(self) -> dict[str, Any]:
        return self._provider_status(self.settings, self._provider_secret_error)

    def _provider_status(self, settings: Settings, secret_error: str | None) -> dict[str, Any]:
        record = self.store.get_provider_config()
        encrypted = bool(record and record.get("api_key_ciphertext"))
        if secret_error:
            key_status = "decrypt_error"
            key_source = "decrypt_error"
        elif settings.api_key:
            key_status = "configured"
            key_source = "sqlite_encrypted" if encrypted else "environment"
        else:
            key_status = "missing"
            key_source = "none"
        state = {
            "provider": settings.provider,
            "base_url": settings.base_url,
            "model": settings.model,
            "api_key_configured": key_status == "configured",
            "api_key_status": key_status,
            "api_key_source": key_source,
            "persistence": "sqlite_encrypted" if record else "environment",
            "updated_at": record.get("updated_at") if record else None,
            "configuration_error": secret_error,
            "client_status": ("restart_required" if self._configuration_uncertain else
                              "cleanup_required" if self._provider_cleanup_required else "ready"),
        }
        state["config_hash"] = digest([state, record, digest(settings.api_key or "")])
        for field in ("model", "base_url", "configuration_error"):
            state[field] = normalize(state[field], (settings.api_key or "",))
        return state

    @contextmanager
    def _configuration_change(self) -> Iterator[None]:
        if self._closed or self._configuration_uncertain:
            raise ValueError("Provider configuration requires restart; Host closed or uncertain")
        if self._configuring or self.runtime_busy():
            raise ValueError("Wait for active runs or configuration changes")
        self._configuring = True
        try:
            yield
        finally:
            self._configuring = False

    async def configure_provider(
        self, *, provider: str, api_key: str | None, base_url: str | None, model: str,
        persist: bool = False, clear_api_key: bool = False,
        expected_config_hash: str | None = None, mutation_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate first, close the idle client, then commit settings and receipt together."""
        with self._configuration_change():
            if expected_config_hash is not None and (
                self.provider_config_status()["config_hash"] != expected_config_hash
            ):
                raise ValueError("Configuration changed; refresh before saving")
            normalized = normalize_provider(provider)
            if normalized not in SUPPORTED_PROVIDERS:
                raise ValueError("Unsupported provider")
            if not model.strip():
                raise ValueError("Model cannot be empty")
            normalized_url = base_url.strip() if base_url else None
            selected_key = api_key.strip() if api_key and api_key.strip() else self.settings.api_key
            current = self.store.get_provider_config() or {}
            encrypted = current.get("api_key_ciphertext")
            if persist:
                if self._provider_secret_error and encrypted and not api_key and not clear_api_key:
                    raise SecretKeyError(
                        "已保存的 API Key 无法解密；请输入新密钥覆盖，或先清除旧密钥"
                    )
                if clear_api_key:
                    encrypted = None
                    selected_key = self._startup_settings.api_key
                elif api_key and api_key.strip():
                    encrypted = self._secret_box.encrypt(api_key.strip())
            candidate = replace(self.settings, provider=normalized, api_key=selected_key,
                                base_url=normalized_url, model=model.strip())
            await self._close_provider()
            # Once closed, never reuse the old transport even if a disk commit fails.
            self.provider = None
            if persist:
                result = self._commit_provider_configuration(candidate, {
                    "provider": normalized, "base_url": normalized_url,
                    "model": model.strip(), "api_key_ciphertext": encrypted,
                }, mutation_id, "save")
            else:
                result = {}
            self.settings = candidate
            self._provider_secret_error = None
            return result or self.provider_config_status()

    async def reset_provider_configuration(
        self, *, expected_config_hash: str | None = None, mutation_id: str | None = None,
    ) -> dict[str, Any]:
        with self._configuration_change():
            if expected_config_hash is not None and (
                self.provider_config_status()["config_hash"] != expected_config_hash
            ):
                raise ValueError("Configuration changed; refresh before resetting")
            await self._close_provider()
            self.provider = None
            result = self._commit_provider_configuration(
                self._startup_settings, None, mutation_id, "reset",
            )
            self.settings = self._startup_settings
            self._provider_secret_error = None
            return result

    def _commit_provider_configuration(
        self, candidate: Settings, record: dict[str, Any] | None,
        mutation_id: str | None, action: str,
    ) -> dict[str, Any]:
        owner = self.configuration_owner()
        before = self.store.get_provider_config()
        try:
            return self.store.commit_configuration(
                owner, [("provider.configured", {"action": action})],
                lambda: self._provider_status(candidate, None),
                mutation_id=mutation_id, provider_update=(record,),
            )
        except BaseException:
            # The commit itself can succeed even when its caller loses the result.
            # Reconcile from durable evidence; never leave an old in-memory key active.
            try:
                completed = mutation_id is not None and any(
                    e["type"] == "http.mutation_completed"
                    and e["payload"].get("identity") == mutation_id
                    for e in self.store.session_events(owner)
                )
                if completed or self.store.get_provider_config() != before:
                    self._load_persisted_provider()
            except Exception:
                self._configuration_uncertain = True
            raise

    async def probe_provider(
        self,
        *,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        model: str,
        run_id: str | None = None,
    ) -> ModelResponse:
        await self.initialize()
        if (self._configuring or self._provider_cleanup_required
                or self._configuration_uncertain):
            raise ValueError("Provider configuration is changing or requires recovery")
        normalized = normalize_provider(provider)
        if normalized not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider '{provider}'")
        selected_key = api_key.strip() if api_key and api_key.strip() else self.settings.api_key
        if not selected_key and self._provider_secret_error:
            raise SecretKeyError(self._provider_secret_error)
        probe_settings = replace(
            self.settings,
            provider=normalized,
            api_key=selected_key,
            base_url=base_url.strip() if base_url else None,
            model=model.strip(),
        )
        return await run_probe(self, probe_settings, build_provider, run_id=run_id)

    def _clean(self, value: Any) -> Any:
        return normalize(redact(value), (self.settings.api_key or "",))

    def _semantic(self, value: Any) -> Any:
        """Preserve model-visible data while removing values unsafe to persist.

        Execution uses the provider's raw tool arguments. Semantic facts keep the
        complete text so the event log and the next model request do not silently
        change prompts, tool calls, results, or final answers.
        """
        return normalize(
            redact(value, max_text=None),
            (self.settings.api_key or "",),
        )

    async def _emit(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        sink: EventSink | None,
        *,
        role: str = "system",
        author: str = "runtime",
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise ValueError("Run not found")
        event = self.store.append_fact(
            run["session_id"],
            event_type,
            payload,
            run_id=run_id,
            role=role,
            author=author,
        )
        if sink:
            aliases = {"tool.prepared": "tool.request", "tool.completed": "tool.result"}
            with suppress(Exception):
                await sink.emit(
                    {
                        **event,
                        "seq": event["session_seq"],
                        "type": aliases.get(event["type"], event["type"]),
                    }
                )
        return event

    async def _complete_with_retry(
        self,
        request: ModelRequest,
        run_id: str,
        sink: EventSink | None,
        *,
        kind: str = "agent",
    ) -> ModelResponse:
        request_id = f"request_{uuid.uuid4().hex}"
        admitted = self._admitted_intent(run_id) or {}
        call_kind = "worker" if kind == "agent" and admitted.get("parent_run_id") else kind
        for attempt in range(1, 4):
            async with self.dispatch.scheduler.model_slot():
                attempt_id = f"attempt_{uuid.uuid4().hex}"
                started = time.perf_counter()
                identity = {
                    "request_id": request_id, "attempt_id": attempt_id, "attempt": attempt,
                    "kind": call_kind, "model": request.model, "generation": request.generation,
                    "max_output_tokens": request.max_tokens,
                    "provider": self.settings.provider,
                }
                try:
                    receipt = self.model_budget.begin(run_id, request, identity)
                except BudgetExceeded as exc:
                    await self._emit(run_id, "budget.blocked", {
                        "request_id": request_id, "violations": exc.violations,
                    }, sink)
                    raise
                try:
                    await self._notify_model_attempt(run_id, sink)
                    if receipt.warnings:
                        await self._emit(run_id, "budget.warning", {
                            "attempt_id": attempt_id, "budgets": receipt.warnings,
                        }, sink)
                    response = await asyncio.wait_for(
                        self._complete_request(request, run_id, request_id, kind),
                        timeout=self.settings.model_timeout,
                    )
                except BaseException as exc:
                    self.model_budget.finish(run_id, {
                        **identity, "status": "cancelled" if isinstance(exc, asyncio.CancelledError)
                        else "failed", "usage_source": "unknown",
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    })
                    if not isinstance(exc, (asyncio.TimeoutError, ProviderError)):
                        raise
                    streaming = kind == "agent" and callable(
                        getattr(self._provider(), "stream", None)
                    )
                    retryable = (
                        isinstance(exc, asyncio.TimeoutError) or exc.retryable
                    ) and not streaming
                    message = (
                        f"Model request timed out ({self.settings.model_timeout:g}s)"
                        if isinstance(exc, asyncio.TimeoutError) else self._clean(str(exc))
                    )
                    await self._emit(run_id, "model.retry", {
                        **identity, "retryable": retryable and attempt < 3,
                        "error": message,
                    }, sink)
                    if not retryable or attempt == 3:
                        if isinstance(exc, asyncio.TimeoutError):
                            raise RuntimeError(message) from exc
                        raise
                else:
                    settled = self.model_budget.finish(run_id, {
                        **identity, "status": "completed", "usage": response.usage,
                        "usage_source": "reported" if response.usage else "unknown",
                        "stop_reason": response.stop_reason,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    })
                    await self._notify_model_attempt(run_id, sink)
                    if settled.warnings:
                        await self._emit(run_id, "budget.warning", {
                            "attempt_id": attempt_id, "budgets": settled.warnings,
                        }, sink)
                    return response
            await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
        raise RuntimeError("Model attempts exhausted")

    async def _notify_model_attempt(self, run_id: str, sink: EventSink | None) -> None:
        """Publish a committed attempt; accounting never depends on delivery to a sink."""
        if sink:
            event = self.store.run_events(run_id)[-1]
            with suppress(Exception):
                await sink.emit({**event, "seq": event["session_seq"]})

    async def _complete_request(
        self, request: ModelRequest, run_id: str, request_id: str, kind: str,
    ) -> ModelResponse:
        stream = getattr(self._provider(), "stream", None)
        if not callable(stream) or kind != "agent":
            return await self._provider().complete(request)
        buffer = SafeTextBuffer((self.settings.api_key or "",), self._clean)
        completed: ModelResponse | None = None
        observed = False
        try:
            async with aclosing(stream(request)) as iterator:
                async for event in iterator:
                    observed = True
                    if completed is not None:
                        raise ProviderError("Model stream emitted data after its terminal response")
                    if event.kind == "text_delta":
                        self.previews.append(run_id, request_id, buffer.feed(event.text))
                    elif event.kind == "response_completed":
                        if completed is not None or event.response is None:
                            raise ProviderError("Invalid duplicate or missing stream terminal")
                        completed = event.response
                    else:
                        raise ProviderError("Unknown model stream event")
            if completed is None:
                raise ProviderError("Model stream ended without a complete response")
            return completed
        except ProviderError as exc:
            if observed:
                raise ProviderError(str(exc), retryable=False) from exc
            raise
        finally:
            # No raw suffix is flushed to a client on failure; the canonical
            # completed response is the only durable output.
            self.previews.clear(run_id)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel one active run and wait until its interrupted fact is durable."""
        task = self._run_tasks.get(run_id)
        intent = self._admitted_intent(run_id)
        if (
            task is None
            and intent is not None
            and run_id in self._admission_done
            and not self.store.run_is_terminal(run_id)
        ):
            self._finish_admitted(run_id, asyncio.CancelledError())
            self._release_admission(run_id)
            self.dispatch.finished(run_id)
            record = self.store.get_run(run_id)
            assert record is not None
            return record
        if task is None or task.done():
            record = self.store.get_run(run_id)
            if record is None:
                raise ValueError(f"Run not found: {run_id}")
            raise ValueError(f"Run is not active: {run_id}")
        if task is asyncio.current_task():
            raise ValueError("A run cannot cancel itself")
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        record = self.store.get_run(run_id)
        if record is None:
            raise RuntimeError("Cancelled run was not persisted")
        return record

    async def run(
        self,
        request: RunRequest,
        sink: EventSink | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> RunResult:
        session_id = self.create_session(request.session_id)
        run_id = await self.admit_run(replace(request, session_id=session_id))
        return await self.start_admitted_run(
            run_id, approval_handler=approval_handler, sink=sink,
        )

    async def continue_session(
        self,
        request: ContinuationRequest | str,
        sink: EventSink | None = None,
        approval_handler: ApprovalHandler | None = None,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        session_id = request if isinstance(request, str) else request.session_id
        identity = run_id or f"run_{uuid.uuid4().hex[:16]}"
        await self.admit_continue(session_id, identity)
        return await self.start_admitted_run(
            identity, approval_handler=approval_handler, sink=sink,
        )

    def validate_continue(self, session_id: str) -> WorkspaceRecord:
        """Validate recovery admission before an asynchronous transport accepts it."""
        workspace = self.sessions.workspace(session_id)
        if self.session_execution_target(session_id) != "source":
            self._validate_continue(workspace, self.store.latest_run(session_id))
            return workspace
        root = Path(workspace.path)
        if not root.is_dir() or root.resolve() != root:
            raise ValueError("Workspace missing or path identity changed")
        self.sessions.working_directory(session_id)
        self._validate_continue(workspace, self.store.latest_run(session_id))
        return workspace

    def _execution_binding(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        return next((event["payload"]["execution_binding"]
                     for event in self.store.run_events(run_id)
                     if event["type"] in {"run.admitted", "run.started"}
                     and "execution_binding" in event["payload"]), None)

    @staticmethod
    def _execution_key(run_id: str, intent: dict[str, Any]) -> str:
        binding = intent.get("execution_binding", {})
        if binding.get("kind") == "evaluation":
            return f"evaluation_run:{run_id}"
        if binding.get("kind") == "snapshot":
            return f"snapshot_run:{run_id}"
        if binding.get("kind") == "worktree":
            return str(binding["execution_workspace_id"])
        return str(intent["workspace_id"])

    def session_execution_target(self, session_id: str) -> str:
        run = self.store.latest_run_summary(session_id)
        binding = self._execution_binding(run["id"]) if run else None
        return binding["kind"] if binding else "source"

    def runtime_busy(self) -> bool:
        return bool(self._active_tasks or self._admission_done
                    or self.dispatch.scheduler.counts()["preparing_admissions"])

    def session_busy(self, session_id: str) -> bool:
        return self._session_locks[session_id].locked() or any(
            admission.session_id == session_id for admission in self._admission_done.values()
        )

    def run_execution(self, run_id: str) -> dict[str, Any]:
        binding = self._execution_binding(run_id)
        if not binding:
            return {"kind": "source"}
        if binding["kind"] == "evaluation":
            return {key: binding[key] for key in ("kind", "workspace_id", "evaluation_id")}
        if binding["kind"] == "worktree":
            return {key: binding[key] for key in (
                "kind", "workspace_id", "execution_workspace_id", "working_directory",
            )} | {"name": binding["worktree"]["name"],
                 "base_commit": binding["worktree"]["base_commit"]}
        return {key: binding[key] for key in (
            "kind", "workspace_id", "snapshot_id", "working_directory", "skipped",
        )}

    def _session_worktree(self, session_id: str) -> dict[str, Any] | None:
        return next((event["payload"]["execution_binding"]
                     for event in self.store.session_events(session_id)
                     if event["type"] == "run.admitted"
                     and event["payload"].get("execution_binding", {}).get("kind") == "worktree"),
                    None)

    def _execution_paths(self, binding: dict[str, Any]) -> tuple[Path, Path]:
        service = (self.execution_snapshots if binding["kind"] == "snapshot"
                   else self.execution_worktrees)
        return service.restore(binding)

    def _execution_checkpoint(self, binding: dict[str, Any]) -> dict[str, Any]:
        service = (self.execution_snapshots if binding["kind"] == "snapshot"
                   else self.execution_worktrees)
        return service.checkpoint(binding)

    @asynccontextmanager
    async def _project_git(self, project_id: str) -> AsyncIterator[None]:
        async with self._project_git_locks[project_id]:
            yield

    @asynccontextmanager
    async def _session_execution(self, session_id: str, run_id: str) -> AsyncIterator[None]:
        lock = self._session_locks[session_id]
        while True:
            await lock.acquire()
            pending = next((value for identity, value in self._admission_done.items()
                            if value.session_id == session_id and identity != run_id), None)
            if pending is None:
                break
            lock.release()
            await pending.done.wait()
        try:
            yield
        finally:
            lock.release()
            self.dispatch.dispatch()

    def workspace_busy(self, workspace_id: str) -> bool:
        return workspace_id in self._admissions or self._workspace_locks[workspace_id].locked()

    async def manage_delivery(self, action: str, **arguments: Any) -> dict[str, Any]:
        from eventide.delivery_runtime import DeliveryController

        return await DeliveryController(self).execute(action, **arguments)

    async def check_project(self, session_id: str, command: str) -> dict[str, Any]:
        """Run one independent bounded check, without asking the model to judge itself."""
        await self.initialize()
        workspace = self.sessions.workspace(session_id)
        if self.session_execution_target(session_id) == "snapshot":
            raise ValueError("Independent commands cannot run inside a Plan snapshot")
        binding = self._session_worktree(session_id)
        if binding:
            workspace = self.store.get_workspace(binding["execution_workspace_id"])
            _, working_root = self._execution_paths(binding)
        else:
            working_root = self.sessions.working_directory(session_id)
        task = asyncio.current_task()
        assert task is not None
        self._active_tasks.add(task)
        run_id: str | None = None
        try:
            async with self._workspace_execution(workspace.workspace_id):
                if self.store.workspace_has_unknown_outcome(workspace.workspace_id):
                    raise ValueError("Workspace has an unresolved tool outcome")
                control = self.create_session(workspace_id=workspace.workspace_id)
                self.store.update_session(control, title="Independent verification", archived=True)
                run_id = f"run_{uuid.uuid4().hex[:16]}"
                self.store.create_run(run_id, control)
                self._run_tasks[run_id] = task
                await self._emit(run_id, "run.started", {"source": "verification"}, None)
                call = ToolCall("verification", "bash", {"command": command})
                await self._emit(
                    run_id,
                    "tool.prepared",
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": self._semantic(call.arguments),
                        "readonly": False,
                    },
                    None,
                )
                executor = ToolExecutor(
                    {"bash": lambda **_args: ""},
                    command_executor=CoordinatedCommandExecutor(
                        LocalCommandExecutor(min(self.settings.command_timeout, 30)),
                        self._project_git_locks[workspace.project_id],
                    ),
                )
                execution = asyncio.create_task(
                    executor.execute(
                        call,
                        ToolContext(
                            working_root, run_id, self.policy
                        ),
                    )
                )
                try:
                    checked = await asyncio.shield(execution)
                except asyncio.CancelledError:
                    execution.cancel()
                    while not execution.done():
                        with suppress(asyncio.CancelledError):
                            await asyncio.shield(execution)
                    raise
                payload = self._semantic(
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "content": checked.content,
                        "is_error": checked.is_error,
                        "metadata": checked.metadata,
                    }
                )
                await self._emit(run_id, "tool.completed", payload, None)
                passed = not checked.is_error and checked.metadata.get("exit_code") == 0
                await self._emit(
                    run_id,
                    "run.completed" if passed else "run.failed",
                    {
                        "output": self._semantic(checked.content),
                        "steps": 0,
                        "tool_calls": 1,
                        "usage": {},
                    },
                    None,
                )
                return {"run_id": run_id, "passed": passed, **payload}
        except BaseException as exc:
            if run_id and not self.store.run_is_terminal(run_id):
                await self._emit(
                    run_id,
                    "run.interrupted" if isinstance(exc, asyncio.CancelledError) else "run.failed",
                    {"error": self._clean(str(exc))},
                    None,
                )
            raise
        finally:
            if run_id:
                self._run_tasks.pop(run_id, None)
            self._active_tasks.discard(task)

    def is_worker_session(self, session_id: str) -> bool:
        return any(
            e["type"] == "run.admitted" and e["payload"].get("parent_run_id")
            for e in self.store.session_events(session_id)
        )

    def is_control_session(self, session_id: str) -> bool:
        return any(
            e["type"] == "control.session" or (
                e["type"] == "run.started"
                and e["payload"].get("source") in {"delivery", "verification"}
            )
            for e in self.store.session_events(session_id)
        )

    @asynccontextmanager
    async def _workspace_execution(
        self, workspace_id: str, run_id: str | None = None
    ) -> AsyncIterator[None]:
        lock = self._workspace_locks[workspace_id]
        while True:
            await lock.acquire()
            reserved = self._admissions.get(workspace_id)
            if reserved is None or reserved == run_id:
                break
            done = self._admission_done[reserved].done
            lock.release()
            await done.wait()
        try:
            yield
        finally:
            lock.release()
            self.dispatch.dispatch()

    def _release_execution_claims(self, run_id: str) -> None:
        for workspace_id, reserved in list(self._admissions.items()):
            if reserved == run_id:
                self._admissions.pop(workspace_id)

    def _release_admission(self, run_id: str) -> None:
        self._release_execution_claims(run_id)
        done = self._admission_done.pop(run_id, None)
        if done is not None:
            done.done.set()

    def _admitted_intent(self, run_id: str) -> dict[str, Any] | None:
        return next(
            (e["payload"] for e in self.store.run_events(run_id) if e["type"] == "run.admitted"),
            None,
        )

    def _skill_owner(self, workspace_id: str) -> str:
        owner = f"skills_control_{workspace_id}"
        if self.store.get_session(owner) is None:
            self.create_session(owner, workspace_id=workspace_id)
            self.store.update_session(owner, archived=True)
            self.store.append_fact(owner, "control.session", {"kind": "skills"})
        return owner

    def skill_status(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.store.get_workspace(workspace_id)
        return self.skills.status(workspace_id, Path(workspace.path))

    def configure_skills(
        self, workspace_id: str, *, enabled_user: dict[str, str],
        disabled_project: list[str], selected: list[str],
    ) -> dict[str, Any]:
        if self._closed:
            raise ValueError("RuntimeHost is closed")
        workspace = self.store.get_workspace(workspace_id)
        return self.skills.configure(
            self._skill_owner(workspace_id), workspace_id, Path(workspace.path),
            enabled_user=enabled_user, disabled_project=disabled_project, selected=selected,
        )

    async def preview_skill(self, source: Path | bytes, *, zipped: bool) -> dict[str, Any]:
        if zipped:
            preview = await asyncio.to_thread(self.skills.packages.preview_zip, source)
        else:
            if not isinstance(source, Path):
                raise SkillPackageError("Directory imports require a local path")
            preview = await asyncio.to_thread(self.skills.packages.preview_directory, source)
        return preview.as_dict()

    async def import_skill(
        self, workspace_id: str, source: Path | bytes, *, zipped: bool, scope: str = "user",
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise ValueError("RuntimeHost is closed")
        workspace = self.store.get_workspace(workspace_id)
        owner = self._skill_owner(workspace_id)
        if scope == "user":
            # Keep ownership until bounded disk IO has really finished on cancellation.
            return dict(await self._admission_check(
                lambda: self.skills.import_user(
                    owner, source, zipped=zipped, expected_version=expected_version,
                )
            ))
        if scope != "project":
            raise ValueError("Skill scope must be project or user")
        if self.workspace_busy(workspace_id):
            raise ValueError("Workspace is busy")
        async with self._workspace_execution(workspace_id):
            def install_project() -> dict[str, Any]:
                if zipped:
                    preview = self.skills.packages.preview_zip(source)
                else:
                    if not isinstance(source, Path):
                        raise SkillPackageError("Directory imports require a local path")
                    preview = self.skills.packages.preview_directory(source)
                if not preview.valid or not preview.name:
                    raise SkillPackageError("; ".join(d.message for d in preview.diagnostics))
                if expected_version is not None and preview.version != expected_version:
                    raise SkillPackageError("Skill changed since preview; review it again")
                root = Path(workspace.path) / "skills"
                if root.is_symlink():
                    raise SkillPackageError("Project skills directory cannot be a symlink")
                root.mkdir(exist_ok=True)
                target = root / preview.name
                if target.exists() or target.is_symlink():
                    raise SkillPackageError("Project Skill already exists; manage updates with Git")
                temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=root))
                try:
                    for resource in preview.resources:
                        path = temporary / resource.path
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(resource.content)
                    temporary.rename(target)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
                payload = {**preview.as_dict(), "workspace_id": workspace_id}
                self.store.append_fact(owner, "skill.project_imported", payload, author="user")
                return payload
            return dict(await self._admission_check(install_project))

    async def _freeze_skills(
        self, workspace: WorkspaceRecord, request: RunRequest | None, *, root: Path | None = None,
    ) -> dict[str, Any]:
        return dict(await self._admission_check(lambda: self.skills.freeze(self.skills.catalog(
            workspace.workspace_id, root or Path(workspace.path),
            selected=request.selected_skills if request else None,
        ))))

    def generation_settings(self, model: str, session_id: str | None = None) -> dict[str, Any]:
        identity = digest([self.settings.provider, self.settings.base_url, model])
        saved = self.store.generation_config("model", identity)
        declared = saved.get("capabilities")
        supported = (
            GenerationCapabilities.model_validate(declared) if declared
            else capabilities(self.settings.provider, self.settings.base_url, model)
        )
        session = (
            self.store.generation_config("session", session_id, identity) if session_id else {}
        )
        record = self.store.get_session(session_id) if session_id else None
        if session.get("session_created_at") is not None and (
            record is None or session["session_created_at"] != record["created_at"]
        ):
            session = {}
        # Session overrides are endpoint/model scoped; changing models does not carry them over.
        overrides = (
            session.get("generation", {}) if session.get("model_identity") == identity else {}
        )
        result = {
            "model": model, "model_identity": identity,
            "defaults": saved.get("generation", {}), "session": overrides,
            "capabilities": supported.model_dump(),
        }
        result["config_hash"] = digest([
            result, self.provider_config_status()["config_hash"],
            record["created_at"] if record else None,
        ])
        return result

    def configure_generation(
        self, model: str, generation: dict[str, Any], *, session_id: str | None = None,
        declared: GenerationCapabilities | None = None,
        expected_config_hash: str | None = None, mutation_id: str | None = None,
    ) -> dict[str, Any]:
        if self._configuring or self._configuration_uncertain:
            raise ValueError("Provider configuration is changing or requires restart")
        if self._closed:
            raise ValueError("RuntimeHost is closed")
        if session_id:
            self.sessions.workspace(session_id)
            if self.is_worker_session(session_id) or self.is_control_session(session_id):
                raise ValueError("Internal sessions cannot configure generation")
        state = self.generation_settings(model, session_id)
        if expected_config_hash is not None and state["config_hash"] != expected_config_hash:
            raise ValueError("Configuration changed; refresh before saving")
        patch = GenerationPatch.model_validate(generation).patch()
        supported = declared or GenerationCapabilities.model_validate(state["capabilities"])
        resolve_generation(
            [state["defaults"] if session_id else {}, patch],
            provider=self.settings.provider, base_url=self.settings.base_url, model=model,
            declared=supported,
        )
        owner = self.configuration_owner()
        record = self.store.get_session(session_id) if session_id else None
        return self.store.commit_configuration(owner, [("generation.configured", {
            "scope": "session" if session_id else "model",
            "identity": session_id or state["model_identity"],
            "model_identity": state["model_identity"],
            "generation": patch,
            "capabilities": supported.model_dump() if not session_id else None,
            "session_created_at": record["created_at"] if record else None,
        })], lambda: self.generation_settings(model, session_id), mutation_id=mutation_id)

    def _run_intent(
        self, request: RunRequest | None, source_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        mode = run_policy.normalize_mode(request.mode) if request else "auto"
        model = request.model.strip() if request and request.model else self.settings.model
        model = model or self.settings.model
        execution_config: dict[str, Any] = {}
        generation: dict[str, Any] | None = None
        skill_snapshot: dict[str, Any] | None = None
        if source_id:
            prior: dict[str, Any] = next(
                (
                    e["payload"]
                    for e in reversed(self.store.run_events(source_id))
                    if e["type"] in {"run.started", "run.admitted"}
                ),
                {},
            )
            mode = "plan" if prior.get("mode") == "plan" else "auto"
            same_endpoint = prior.get("endpoint_hash", digest(self.settings.base_url)) == digest(
                self.settings.base_url
            )
            if prior.get("provider") == self.settings.provider and same_endpoint:
                model = prior.get("model") or model
                generation = prior.get("generation")
            execution_config = prior.get("execution_config") or {}
            old_intent = self._admitted_intent(source_id) or {}
            skill_snapshot = old_intent.get("skill_snapshot") or next((
                e["payload"] for e in self.store.run_events(source_id)
                if e["type"] == "skills.snapshot"
            ), None)
        if not isinstance(model, str) or len(model) > 256:
            raise ValueError("Model name cannot exceed 256 characters")
        state = self.generation_settings(
            model, session_id or (request.session_id if request else None)
        )
        if generation is None:
            layers = [state["defaults"], state["session"], request.generation if request else {}]
        else:
            layers = [generation]
        generation = resolve_generation(
            layers, provider=self.settings.provider, base_url=self.settings.base_url, model=model,
            declared=GenerationCapabilities.model_validate(state["capabilities"]),
        )
        generation.setdefault("max_output_tokens", self.settings.max_tokens)
        return {
            "mode": mode,
            "model": model,
            "provider": self.settings.provider,
            "endpoint_hash": digest(self.settings.base_url),
            "generation": generation,
            **({"skill_snapshot": skill_snapshot} if skill_snapshot is not None else {}),
            **({"execution_config": execution_config} if execution_config else {}),
        }

    async def admit_run(self, request: RunRequest) -> str:
        session_id = self.create_session(request.session_id)
        run_id = request.run_id or f"run_{uuid.uuid4().hex[:16]}"
        await self._reserve_run(session_id, run_id, request)
        return run_id

    async def admit_continue(
        self, session_id: str, run_id: str, *, request_fingerprint: str | None = None,
    ) -> str:
        return await self._reserve_run(
            session_id, run_id, None, request_fingerprint=request_fingerprint,
        )

    async def _admission_check(self, function: Any, *args: Any) -> Any:
        """Retain the Store/Workspace owner until a threaded check has finished."""
        task = asyncio.current_task()
        assert task is not None
        already_active = task in self._active_tasks
        if self._closed and not already_active:
            raise RuntimeError("Host is closed")
        self._active_tasks.add(task)
        try:
            operation = asyncio.create_task(asyncio.to_thread(function, *args))
            cancelled = False
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled = True
            result = operation.result()
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            if not already_active:
                self._active_tasks.discard(task)

    async def _reserve_run(
        self, session_id: str, run_id: str, request: RunRequest | None, *,
        execution_config: dict[str, Any] | None = None,
        request_fingerprint: str | None = None,
    ) -> str:
        """Reserve capacity before preparation; acknowledge only a durable intent."""
        if self.session_busy(session_id):
            raise ValueError("Session is busy")
        ticket = self.dispatch.scheduler.reserve_admission(run_id, session_id, startable=True)
        try:
            result = await self._prepare_run(
                session_id, run_id, request, execution_config=execution_config,
                request_fingerprint=request_fingerprint,
                require_checkpoint=ticket.lane == "waiting",
            )
            self.dispatch.admitted(ticket)
            return result
        except BaseException as exc:
            self._finish_admitted(run_id, exc)
            self._release_admission(run_id)
            self.dispatch.scheduler.release_ticket(ticket)
            raise
        finally:
            # A failed preparation/control lock may have been blocking an older
            # already accepted Run even when no root task has just completed.
            self.dispatch.dispatch()

    async def _prepare_run(
        self,
        session_id: str,
        run_id: str,
        request: RunRequest | None,
        *,
        execution_config: dict[str, Any] | None = None,
        request_fingerprint: str | None = None,
        require_checkpoint: bool = False,
    ) -> str:
        """Own a Workspace and atomically persist the request before transport acceptance."""
        await self.initialize()
        if self.is_worker_session(session_id) or self.is_control_session(session_id):
            raise ValueError("Worker sessions cannot accept ordinary runs or Continue")
        if (self._configuring or self._provider_cleanup_required
                or self._configuration_uncertain):
            raise ValueError("Provider configuration is changing or requires recovery")
        workspace = self.sessions.workspace(session_id)
        if self.state_dir.resolve() == Path(workspace.path):
            raise ValueError("State directory must not be the Workspace root")
        if run_id in self._admission_done or self.store.get_run_identity(run_id):
            raise ValueError("Run identity already used")
        if self.session_busy(session_id):
            raise ValueError("Session is busy")
        if (request and self._session_worktree(session_id)
                and request.execution_target != "worktree"):
            raise ValueError("Session owns a worktree; choose worktree or create a new Session")
        if request and request.execution_target not in {"source", "snapshot", "worktree"}:
            raise ValueError("Unknown execution target")
        if (request and request.execution_target != "source") or (
            request is None and self.session_execution_target(session_id) != "source"
        ):
            return await self._reserve_copy_run(
                session_id, run_id, request, workspace, request_fingerprint,
            )
        if self.workspace_busy(workspace.workspace_id):
            raise ValueError("Workspace is busy")
        root = Path(workspace.path)
        if not root.is_dir() or root.resolve() != root:
            raise ValueError("Workspace missing or path identity changed")
        self.sessions.working_directory(session_id)
        task = asyncio.current_task()
        assert task is not None
        already_active = task in self._active_tasks
        self._active_tasks.add(task)
        try:
            async with self._workspace_locks[workspace.workspace_id]:
                if self.store.workspace_has_unknown_outcome(workspace.workspace_id):
                    raise ValueError(
                        "Workspace has an unresolved tool outcome; execution is blocked"
                    )
                self._admissions[workspace.workspace_id] = run_id
                self._admission_done[run_id] = _Admission(session_id, asyncio.Event())
                try:
                    previous = self.store.latest_run(session_id)
                    if request is None:
                        await self._admission_check(self._validate_continue, workspace, previous)
                    elif previous and previous["status"] == "interrupted":
                        raise ValueError("Session is parked; use Continue")
                    source_id = previous["id"] if request is None and previous else None
                    intent = self._run_intent(request, source_id, session_id)
                    if "skill_snapshot" not in intent:
                        intent["skill_snapshot"] = await self._freeze_skills(workspace, request)
                    if execution_config:
                        intent["execution_config"] = execution_config
                    elif "execution_config" not in intent:
                        saved = self.store.workspace_execution_config(workspace.workspace_id)
                        if saved:
                            intent["execution_config"] = saved
                    checkpoint = await self._admission_check(self._workspace_checkpoint, root)
                    if require_checkpoint and checkpoint is None:
                        raise ValueError("Queued source execution requires a Git checkpoint")
                    ids = list(request.attachment_ids) if request else []
                    # Validation, commit and used-cache write have no intervening await.
                    records = attachments.load_attachments(self.state_dir, session_id, ids)
                    attachments.validate_for_provider(records, self.settings.provider)
                    payload = {
                        **intent,
                        "session_id": session_id,
                        "workspace_id": workspace.workspace_id,
                        "project_id": workspace.project_id,
                        "source_workspace_id": workspace.source_workspace_id,
                        "attachment_ids": ids,
                        "continuation_of": source_id,
                        "request_fingerprint": (
                            request.request_fingerprint if request else request_fingerprint
                        ),
                    }
                    facts = [("run.admitted", self._semantic(payload), "system", "user")]
                    if previous and request is None:
                        facts.extend(self._recovery_facts(previous))
                    if request:
                        facts.append(
                            (
                                "message.user",
                                {
                                    "message": self._semantic(
                                        attachments.compose_message(request.prompt, records)
                                    ),
                                    "attachment_ids": ids,
                                },
                                "user",
                                "user",
                            )
                        )
                    facts.append(
                        ("workspace.checkpoint", {"checkpoint": checkpoint}, "system", "runtime")
                    )
                    turn_id = self.store.admit_run(
                        run_id, session_id, facts, continuation_of=source_id
                    )
                    attachments.mark_attachments_used(self.state_dir, session_id, ids)
                    return turn_id
                except BaseException as exc:
                    self._finish_admitted(run_id, exc)
                    self._release_admission(run_id)
                    raise
        finally:
            if not already_active:
                self._active_tasks.discard(task)

    async def _reserve_copy_run(
        self, session_id: str, run_id: str, request: RunRequest | None,
        workspace: WorkspaceRecord, request_fingerprint: str | None,
    ) -> str:
        target = request.execution_target if request else self.session_execution_target(session_id)
        if target == "snapshot" and request and run_policy.normalize_mode(request.mode) != "plan":
            raise ValueError("Snapshot execution requires Plan mode")
        task = asyncio.current_task()
        assert task is not None
        already_active = task in self._active_tasks
        self._active_tasks.add(task)
        try:
            async with self._session_locks[session_id]:
                previous = self.store.latest_run(session_id)
                if request and previous and previous["status"] == "interrupted":
                    raise ValueError("Session is parked; use Continue")
                source_id = previous["id"] if previous and request is None else None
                self._admission_done[run_id] = _Admission(session_id, asyncio.Event())
                self._admissions[f"snapshot_run:{run_id}"] = run_id
                try:
                    intent = self._run_intent(request, source_id, session_id)
                    if source_id:
                        binding = self._execution_binding(source_id)
                        assert binding is not None
                        key = self._execution_key(source_id, {"execution_binding": binding})
                        if binding["kind"] == "worktree":
                            if self.workspace_busy(key):
                                raise ValueError("Execution worktree is busy")
                            self._admissions[key] = run_id
                        async with self._workspace_execution(key, run_id):
                            await self._admission_check(
                                self._validate_continue, workspace, previous,
                            )
                    elif target == "worktree" and (binding := self._session_worktree(session_id)):
                        copy_id = binding["execution_workspace_id"]
                        if self.workspace_busy(copy_id):
                            raise ValueError("Execution worktree is busy")
                        self._admissions[copy_id] = run_id
                        async with self._workspace_execution(copy_id, run_id):
                            if self.store.workspace_has_unknown_outcome(copy_id):
                                raise ValueError(
                                    "Execution worktree has an unresolved tool outcome"
                                )
                            root, _ = await self._admission_check(self._execution_paths, binding)
                            intent["skill_snapshot"] = await self._freeze_skills(
                                workspace, request, root=root,
                            )
                    else:
                        if (self.workspace_busy(workspace.workspace_id)
                                and workspace.workspace_id not in self._snapshot_preparing):
                            raise ValueError("Workspace is busy")
                        async with (
                            self._workspace_execution(workspace.workspace_id),
                            self._project_git(workspace.project_id),
                        ):
                            self._snapshot_preparing.add(workspace.workspace_id)
                            try:
                                if self.store.workspace_has_unknown_outcome(workspace.workspace_id):
                                    raise ValueError("Workspace has an unresolved tool outcome")
                                root = Path(workspace.path)
                                if not root.is_dir() or root.resolve() != root:
                                    raise ValueError("Workspace missing or path identity changed")
                                self.sessions.working_directory(session_id)
                                session = self.store.get_session(session_id)
                                assert session is not None
                                binding = await self._admission_check(
                                    (self.execution_snapshots.create if target == "snapshot"
                                     else self.execution_worktrees.create),
                                    workspace.workspace_id, root,
                                    session["working_directory"],
                                )
                                if target == "worktree":
                                    copy_root, _ = self._execution_paths(binding)
                                    copy = self.resolve_or_register_workspace(copy_root)
                                    if copy.project_id != workspace.project_id:
                                        raise ValueError("Execution copy has a different project")
                                    binding["execution_workspace_id"] = copy.workspace_id
                                    self._admissions[copy.workspace_id] = run_id
                                else:
                                    copy_root = root
                                intent["skill_snapshot"] = await self._freeze_skills(
                                    workspace, request, root=copy_root,
                                )
                                current = await self._admission_check(
                                    self._workspace_checkpoint, root,
                                )
                                if current != binding["source_checkpoint"]:
                                    raise ValueError(
                                        "Source Workspace changed during snapshot admission"
                                    )
                            finally:
                                self._snapshot_preparing.discard(workspace.workspace_id)
                    if "execution_config" not in intent:
                        saved = self.store.workspace_execution_config(workspace.workspace_id)
                        if saved:
                            intent["execution_config"] = saved
                    if target == "worktree":
                        copy_id = binding["execution_workspace_id"]
                        if self.store.workspace_has_unknown_outcome(copy_id):
                            raise ValueError("Execution worktree has an unresolved tool outcome")
                        self._admissions[copy_id] = run_id
                    checkpoint = await self._admission_check(self._execution_checkpoint, binding)
                    ids = list(request.attachment_ids) if request else []
                    records = attachments.load_attachments(self.state_dir, session_id, ids)
                    attachments.validate_for_provider(records, self.settings.provider)
                    payload = {
                        **intent, "session_id": session_id, "workspace_id": workspace.workspace_id,
                        "project_id": workspace.project_id,
                        "source_workspace_id": workspace.source_workspace_id,
                        "attachment_ids": ids, "continuation_of": source_id,
                        "execution_binding": binding,
                        "request_fingerprint": request.request_fingerprint
                        if request else request_fingerprint,
                    }
                    facts = [("run.admitted", self._semantic(payload), "system", "user")]
                    if previous and request is None:
                        facts.extend(self._recovery_facts(previous))
                    if request:
                        facts.append(("message.user", {
                            "message": self._semantic(
                                attachments.compose_message(request.prompt, records)
                            ), "attachment_ids": ids,
                        }, "user", "user"))
                    facts.append(("workspace.checkpoint", {"checkpoint": checkpoint},
                                  "system", "runtime"))
                    turn_id = self.store.admit_run(
                        run_id, session_id, facts, continuation_of=source_id,
                    )
                    attachments.mark_attachments_used(self.state_dir, session_id, ids)
                    return turn_id
                except BaseException as exc:
                    self._finish_admitted(run_id, exc)
                    self._release_admission(run_id)
                    raise
        finally:
            if not already_active:
                self._active_tasks.discard(task)

    async def _execute_copy_run(
        self, session_id: str, run_id: str, workspace: WorkspaceRecord,
        request: RunRequest | None, sink: EventSink | None,
        approval_handler: ApprovalHandler | None, binding: dict[str, Any],
    ) -> RunResult:
        admitted = self.store.get_run_identity(run_id)
        intent = self._admitted_intent(run_id)
        if not admitted or not intent or self.store.run_is_terminal(run_id):
            raise ValueError("Snapshot Run is not awaiting execution")
        if binding["workspace_id"] != workspace.workspace_id or (
            binding["kind"] == "snapshot" and intent["mode"] != "plan"
        ):
            raise ValueError("Execution copy binding mismatch")
        async with (
            self._session_execution(session_id, run_id),
            self._workspace_execution(self._execution_key(run_id, intent), run_id),
        ):
            await self._admission_check(self._execution_paths, binding)
            if binding["kind"] == "worktree" and self.store.workspace_has_unknown_outcome(
                binding["execution_workspace_id"]
            ):
                raise ValueError("Execution worktree has an unresolved tool outcome")
            if binding["kind"] == "worktree":
                observed = await self._admission_check(self._execution_checkpoint, binding)
                accepted = self.store.get_run(run_id)
                if not accepted or observed != accepted["checkpoint"]:
                    raise ValueError("Execution worktree changed after admission")
            if self._closed:
                raise RuntimeError("Host is closed")
            task = asyncio.current_task()
            assert task is not None
            self._run_tasks[run_id] = task
            return await self._execute(
                session_id, run_id, workspace, request, admitted["continuation_of"], sink,
                approval_handler, existing_turn_id=admitted["turn_id"], execution_binding=binding,
            )

    def _finish_admitted(self, run_id: str, error: BaseException) -> None:
        """Close accepted work that stopped before its execution could write a terminal."""
        if (
            self.store.get_run_identity(run_id)
            and not self.store.run_is_terminal(run_id)
            and self._admitted_intent(run_id) is not None
        ):
            cancelled = isinstance(error, asyncio.CancelledError)
            self.store.append_event(
                run_id,
                "run.interrupted" if cancelled else "run.failed",
                {
                    "error": "Run cancelled before execution; session parked"
                    if cancelled
                    else self._clean(f"{type(error).__name__}: {error}"),
                    "reason": "cancelled" if cancelled else "admission_failed",
                },
            )

    def start_admitted_run(
        self, run_id: str, *, approval_handler: ApprovalHandler | None = None,
        sink: EventSink | None = None,
    ) -> asyncio.Task[RunResult]:
        """Register cancellation ownership synchronously, before the worker can run."""
        run = self.store.get_run_identity(run_id)
        intent = self._admitted_intent(run_id)
        if not run or not intent or self.store.run_is_terminal(run_id):
            raise ValueError("Run is not awaiting execution")
        if self.is_control_session(run["session_id"]):
            raise ValueError("Control Runs require their dedicated controller")
        if run_id in self._run_tasks:
            raise ValueError("Run is already scheduled")
        if run_id not in self._admission_done:
            raise ValueError("Run admission is not owned by this Host")
        if self._closed:
            raise ValueError("Host is closed")
        request = (
            None
            if run["continuation_of"]
            else RunRequest(
                "",
                run["session_id"],
                run_id,
                mode=intent["mode"],
                attachment_ids=list(intent["attachment_ids"]),
                model=intent["model"],
            )
        )
        try:
            task = self.dispatch.start(
                run_id, lambda: self._admit(
                    run["session_id"],
                    request,
                    bool(run["continuation_of"]),
                    sink,
                    approval_handler,
                    run_id=run_id,
                )
            )
        except BaseException as exc:
            self._finish_admitted(run_id, exc)
            self._release_admission(run_id)
            self.dispatch.finished(run_id)
            raise
        self._track_run_task(run_id, task)
        return task

    def _track_run_task(self, run_id: str, task: asyncio.Task[RunResult]) -> None:
        self._run_tasks[run_id] = task
        self._active_tasks.add(task)

        def done(finished: asyncio.Task[RunResult]) -> None:
            if finished.cancelled():
                error: BaseException | None = asyncio.CancelledError()
            else:
                error = finished.exception()
            if error is not None:
                self._finish_admitted(run_id, error)
            self._release_admission(run_id)
            self._run_tasks.pop(run_id, None)
            self._active_tasks.discard(finished)
            self.dispatch.finished(run_id)

        task.add_done_callback(done)

    async def abandon_interruption(
        self,
        session_id: str,
        sink: EventSink | None = None,
    ) -> RunResult:
        """Close unresolved protocol history without claiming unknown tools succeeded."""
        await self.initialize()
        workspace = self.sessions.workspace(session_id)
        task = asyncio.current_task()
        assert task is not None
        if self.is_worker_session(session_id) or self.is_control_session(session_id):
            raise ValueError("Worker sessions cannot accept ordinary runs or Continue")
        self._active_tasks.add(task)
        try:
            async with self._session_execution(session_id, ""):
                previous_run = self.store.latest_run(session_id)
                binding = self._execution_binding(previous_run["id"]) if previous_run else None
                key = (self._execution_key(previous_run["id"], {"execution_binding": binding})
                       if binding and previous_run else workspace.workspace_id)
                async with self._workspace_execution(key):
                    previous = self.store.latest_run(session_id)
                    if not previous or previous["status"] != "interrupted":
                        raise ValueError("Only an interrupted, parked session can be abandoned")
                    started = time.perf_counter()
                    run_id = f"run_{uuid.uuid4().hex[:16]}"
                    turn_id = self.store.create_run(
                        run_id,
                        session_id,
                        continuation_of=previous["id"],
                    )
                    await self._emit(
                        run_id,
                        "run.started",
                        {
                            "session_id": session_id,
                            "workspace_id": workspace.workspace_id,
                            "provider": self.settings.provider,
                            "model": self.settings.model,
                            "continuation_of": previous["id"],
                            "recovery_action": "abandon",
                            **({"execution_binding": self._execution_binding(previous["id"])}
                               if self._execution_binding(previous["id"]) else {}),
                        },
                        sink,
                        author="user",
                    )
                    await self._resolve_interrupted_history(
                        run_id,
                        previous,
                        sink,
                        "Recovery abandoned by user; tool outcome was not assumed or replayed.",
                    )
                    await self._emit(
                        run_id,
                        "recovery.abandoned",
                        {"source_run_id": previous["id"]},
                        sink,
                        author="user",
                    )
                    duration = (time.perf_counter() - started) * 1000
                    await self._emit(
                        run_id,
                        "run.completed",
                        {
                            "output": "",
                            "error": None,
                            "steps": 0,
                            "tool_calls": 0,
                            "duration_ms": duration,
                            "usage": {},
                        },
                        sink,
                    )
                    return RunResult(
                        run_id,
                        session_id,
                        "completed",
                        "",
                        0,
                        0,
                        duration,
                        {},
                        turn_id,
                        previous["id"],
                    )
        finally:
            self._active_tasks.discard(task)

    async def _admit(
        self,
        session_id: str,
        request: RunRequest | None,
        continuation: bool | None,
        sink: EventSink | None,
        approval_handler: ApprovalHandler | None,
        *,
        run_id: str | None = None,
        execution_config: dict[str, Any] | None = None,
    ) -> RunResult:
        if self.is_worker_session(session_id) or self.is_control_session(session_id):
            raise ValueError("Worker sessions cannot accept ordinary runs or Continue")
        admitted_run_id = (
            (request.run_id if request else None) or run_id or f"run_{uuid.uuid4().hex[:16]}"
        )
        task = asyncio.current_task()
        assert task is not None
        owner = self._run_tasks.get(admitted_run_id)
        if owner is not None and owner is not task:
            raise ValueError("Run is already scheduled")
        identity = self.store.get_run_identity(admitted_run_id)
        if identity and identity["session_id"] != session_id:
            raise ValueError("Run/session identity mismatch")
        reservation = self._admission_done.get(admitted_run_id)
        if reservation is not None and (reservation.session_id != session_id or identity is None):
            raise ValueError("Run identity is reserved by another admission")
        self._active_tasks.add(task)
        try:
            await self.initialize()
            if (self._configuring or self._provider_cleanup_required
                    or self._configuration_uncertain):
                raise ValueError("Provider configuration is changing or requires recovery")
            workspace = self.sessions.workspace(session_id)
            binding = self._execution_binding(admitted_run_id)
            if binding:
                return await self._execute_copy_run(
                    session_id, admitted_run_id, workspace, request, sink,
                    approval_handler, binding,
                )
            if request and self._session_worktree(session_id):
                raise ValueError("Session owns a worktree; choose worktree or create a new Session")
            root = Path(workspace.path)
            if not root.is_dir() or root.resolve() != root:
                raise ValueError("Workspace missing or path identity changed")
            self.sessions.working_directory(session_id)
            async with (
                self._session_execution(session_id, admitted_run_id),
                self._workspace_execution(workspace.workspace_id, admitted_run_id),
            ):
                if self._closed:
                    raise RuntimeError("Host is closed")
                if self.store.workspace_has_unknown_outcome(workspace.workspace_id):
                    raise ValueError(
                        "Workspace has an unresolved tool outcome; execution is blocked"
                    )
                admitted = self.store.get_run_identity(admitted_run_id)
                if admitted and (
                    self._admitted_intent(admitted_run_id) is None
                    or self.store.run_is_terminal(admitted_run_id)
                ):
                    raise ValueError("Run identity already used")
                if admitted:
                    observed = await self._admission_check(self._workspace_checkpoint, root)
                    accepted = self.store.get_run(admitted_run_id)
                    if not accepted or observed != accepted["checkpoint"]:
                        raise ValueError("Source Workspace changed after admission")
                    continuation = bool(admitted["continuation_of"])
                previous = (
                    self.store.get_run(admitted["continuation_of"])
                    if admitted and admitted["continuation_of"]
                    else self.store.latest_run(session_id)
                )
                if continuation and not admitted:
                    await self._admission_check(self._validate_continue, workspace, previous)
                elif (
                    not continuation
                    and not admitted
                    and previous
                    and previous["status"] == "interrupted"
                ):
                    raise ValueError("Session is parked; use Continue after resolving interruption")
                source_id = previous["id"] if continuation and previous else None
                self._run_tasks[admitted_run_id] = task
                return await self._execute(
                    session_id,
                    admitted_run_id,
                    workspace,
                    request,
                    source_id,
                    sink,
                    approval_handler,
                    existing_turn_id=admitted["turn_id"] if admitted else None,
                    execution_config=execution_config,
                )
        except BaseException as exc:
            self._finish_admitted(admitted_run_id, exc)
            raise
        finally:
            if self._run_tasks.get(admitted_run_id) is task:
                self._run_tasks.pop(admitted_run_id, None)
            self._release_admission(admitted_run_id)
            self._active_tasks.discard(task)

    def _recover_interrupted_runs(self) -> None:
        """Close out runs the Host died inside, and keep the resumable ones resumable.

        A crash can land between a completed side-effecting tool and the checkpoint
        that records its result, which used to leave the Session parked with Continue
        refusing forever. Append the checkpoint the filesystem can still justify; when
        it cannot justify one, record which paths changed instead, so the user can see
        what to check before deciding to abandon.
        """
        from eventide.evaluation_control import is_evaluation_run

        for run_id in self.store.unfinished_runs():
            payload: dict[str, Any] = {"error": "Host stopped before terminal fact"}
            started = any(event["type"] == "run.started"
                          for event in self.store.run_events(run_id))
            # An unstarted admission already has its authoritative checkpoint.
            # Replacing it with the current filesystem would hide queue-time drift.
            queued = not started and self._admitted_intent(run_id) is not None
            evidence = (None if queued or is_service_probe_run(self.store, run_id)
                        or is_evaluation_run(self.store, run_id)
                        else self._crash_gap_evidence(run_id))
            if queued:
                payload["reason"] = "host_stopped_before_dispatch"
            if evidence is not None:
                files, checkpoint = evidence
                if files:
                    payload["gap_files"] = files
                elif checkpoint is not None:
                    self.store.append_event(
                        run_id, "workspace.checkpoint", {"checkpoint": checkpoint}
                    )
            self.store.append_event(run_id, "run.interrupted", payload)

    def _crash_gap_evidence(self, run_id: str) -> tuple[list[str], dict[str, Any] | None] | None:
        """What the filesystem still knows about a run that died without a checkpoint.

        An empty file list means nothing in the Git-visible tree was touched since the
        run's last durable fact, so the workspace is provably what it was when the Host
        died and the checkpoint returned alongside it is the one that went missing. A
        non-empty list is a change the Runtime cannot account for. `None` means there
        was nothing trustworthy to measure.
        """
        run = self.store.get_run(run_id)
        session = run and self.store.get_session(run["session_id"])
        workspace = session and self.store.get_workspace(session["workspace_id"])
        events = self.store.run_events(run_id)
        if not workspace or not events:
            return None
        binding = self._execution_binding(run_id)
        root = Path(workspace.path)
        if binding:
            try:
                if binding["kind"] == "snapshot":
                    return [], self._execution_checkpoint(binding)
                root, _ = self._execution_paths(binding)
            except (ValueError, OSError):
                return ["execution copy changed or missing"], None
        report = changed_since(
            root, events[-1]["ts"], exclude_untracked=self._checkpoint_files,
            exclude_untracked_directories=self._checkpoint_directories,
        )
        if report is None:
            return None
        files, _truncated = report
        if files:
            return files, None
        checkpoint = self._workspace_checkpoint(root)
        previous = run.get("checkpoint") if run else None
        if checkpoint and checkpoint.get("checkpoint_policy") != (
            previous.get("checkpoint_policy") if previous else None
        ):
            return ["checkpoint policy changed"], None
        return [], checkpoint

    def _validate_continue(
        self,
        workspace: WorkspaceRecord,
        previous: dict[str, Any] | None,
    ) -> None:
        if not previous or previous["status"] != "interrupted":
            raise ValueError("Only an interrupted, parked session can Continue")
        binding = self._execution_binding(previous["id"])
        evidence: dict[str, Any] | None
        if binding:
            evidence = self._execution_checkpoint(binding)
        else:
            if not workspace.git_root:
                raise ValueError("Continue requires a Git workspace")
            evidence = self._workspace_checkpoint(Path(workspace.path))
        if evidence is None or evidence != previous["checkpoint"]:
            raise ValueError("Workspace changed or checkpoint unavailable; session remains parked")
        events = self.store.run_events(previous["id"])
        if any(event["type"] == "tool.outcome_unknown" for event in events):
            raise ValueError("Unknown tool outcome; session remains parked")
        last_checkpoint = max(
            (e["session_seq"] for e in events if e["type"] == "workspace.checkpoint"), default=0
        )
        readonly_calls = {
            event["payload"]["call_id"]
            for event in events
            if event["type"] == "tool.prepared" and event["payload"].get("readonly", False)
        }
        for event in events:
            if (
                event["type"] == "tool.completed"
                and event["session_seq"] > last_checkpoint
                and event["payload"]["call_id"] not in readonly_calls
            ):
                raise ValueError("Missing post-tool checkpoint; session remains parked")
        for operation in previous["tools"].values():
            if operation["status"] == "prepared" and not operation.get("readonly", False):
                raise ValueError("Unknown tool outcome; session remains parked")

    async def _resolve_interrupted_history(
        self,
        run_id: str,
        previous: dict[str, Any],
        sink: EventSink | None,
        content: str = "Abandoned after interruption; not replayed.",
    ) -> None:
        """Pair unresolved calls and close ephemeral approvals in a successor run."""
        for event_type, payload, role, author in self._recovery_facts(previous, content):
            await self._emit(run_id, event_type, payload, sink, role=role, author=author)

    def _recovery_facts(
        self, previous: dict[str, Any], content: str = "Abandoned after interruption; not replayed."
    ) -> list[tuple[str, dict[str, Any], str, str]]:
        pending: dict[str, dict[str, Any]] = {}
        approvals: set[str] = set()
        for event in self.store.session_events(previous["session_id"]):
            if event["partial"]:
                continue
            kind, payload = event["type"], event["payload"]
            if kind == "model.response":
                for block in payload.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        pending[block["id"]] = block
            elif kind in {"tool.completed", "tool.abandoned"}:
                pending.pop(payload["call_id"], None)
            elif kind == "approval.required":
                approvals.add(payload["approval_id"])
            elif kind == "approval.resolved":
                approvals.discard(payload["approval_id"])
        facts = [
            (
                "tool.abandoned",
                {
                    "call_id": block["id"],
                    "name": block["name"],
                    "is_error": True,
                    "content": content,
                },
                "system",
                "runtime",
            )
            for block in pending.values()
        ]
        facts.extend(
            (
                "approval.resolved",
                {"approval_id": identity, "approved": False, "reason": "Source run interrupted"},
                "system",
                "runtime",
            )
            for identity in sorted(approvals)
        )
        return facts

    async def _execute(
        self,
        session_id: str,
        run_id: str,
        workspace: WorkspaceRecord,
        request: RunRequest | None,
        continuation_of: str | None,
        sink: EventSink | None,
        approval_handler: ApprovalHandler | None,
        *,
        existing_turn_id: str | None = None,
        readonly_profile: ReadonlyProfile | None = None,
        snapshot_root: Path | None = None,
        execution_binding: dict[str, Any] | None = None,
        execution_config: dict[str, Any] | None = None,
    ) -> RunResult:
        started = time.perf_counter()
        turn_id = existing_turn_id or self.store.create_run(
            run_id,
            session_id,
            continuation_of=continuation_of,
        )
        if execution_binding:
            workspace_root, working_root = self._execution_paths(execution_binding)
        else:
            workspace_root = snapshot_root or Path(workspace.path)
            working_root = snapshot_root or self.sessions.working_directory(session_id)

        snapshot_execution = bool(execution_binding and execution_binding["kind"] == "snapshot")
        execution_workspace = (
            self.store.get_workspace(execution_binding["execution_workspace_id"])
            if execution_binding and execution_binding["kind"] == "worktree" else workspace
        )

        def execution_checkpoint(_root: Path) -> dict[str, Any] | None:
            if execution_binding:
                return self._execution_checkpoint(execution_binding)
            return self._workspace_checkpoint(_root)

        manager = MCPManager()
        usage = {"input_tokens": 0, "output_tokens": 0}
        output, steps, tool_count = "", 0, 0
        force_compact = False
        prompt = request.prompt if request else None
        admitted_intent = self._admitted_intent(run_id)
        mode = "auto"
        attachment_ids = list(request.attachment_ids) if request else []
        selected_model = self.settings.model
        max_steps = readonly_profile.max_steps if readonly_profile else self.settings.max_steps
        token_limit = (
            readonly_profile.token_limit if readonly_profile else self.settings.run_token_limit
        )
        max_tokens = readonly_profile.max_tokens if readonly_profile else self.settings.max_tokens
        # Why the run parked, if it did; read by the terminal event on every exit path.
        park_kind: str | None = None
        park_message: str | None = None
        try:
            intent = admitted_intent or self._run_intent(request, continuation_of, session_id)
            execution_config = (
                {}
                if readonly_profile
                else (
                    execution_config
                    or intent.get("execution_config")
                    or self.store.workspace_execution_config(workspace.workspace_id)
                    or {}
                )
            )
            command_executor: CommandExecutor = self.executor.command_executor
            if execution_config.get("kind") == "container":
                from eventide.container_executor import ContainerCommandExecutor

                command_executor = ContainerCommandExecutor(
                    execution_config["image"],
                    timeout=self.settings.command_timeout,
                    read_only=False,
                )
            if workspace.git_root:
                command_executor = CoordinatedCommandExecutor(
                    command_executor, self._project_git_locks[workspace.project_id],
                )
            mode, selected_model = intent["mode"], intent["model"]
            generation = {} if readonly_profile else intent.get("generation", {})
            max_tokens = generation.get("max_output_tokens", max_tokens)
            if readonly_profile:
                mode = "plan"
            if len(selected_model) > 256:
                raise ValueError("Model name cannot exceed 256 characters")
            # A parked run resumes the task it was on, so the continuation says which one.
            resumed_task_id = (
                task_plan_active(self.store.task_plan(session_id)) if continuation_of else None
            )
            await self._emit(
                run_id,
                "run.started",
                {
                    "session_id": session_id,
                    "workspace_id": workspace.workspace_id,
                    "project_id": workspace.project_id,
                    "source_workspace_id": workspace.source_workspace_id,
                    "provider": self.settings.provider,
                    "model": selected_model,
                    "mode": mode,
                    "endpoint_hash": intent["endpoint_hash"],
                    "generation": generation,
                    "continuation_of": continuation_of,
                    "resumed_task_id": resumed_task_id,
                    **({"execution_config": execution_config} if execution_config else {}),
                    **({"execution_binding": execution_binding} if execution_binding else {}),
                    **(
                        {
                            "parent_run_id": readonly_profile.parent_run_id,
                            "worker_role": readonly_profile.role,
                            "snapshot_id": readonly_profile.snapshot_id,
                        }
                        if readonly_profile
                        else {}
                    ),
                },
                sink,
            )
            if continuation_of and not admitted_intent:
                previous = self.store.get_run(continuation_of)
                assert previous is not None
                # Resolve every unpaired advertised call, including calls never dispatched.
                await self._resolve_interrupted_history(run_id, previous, sink)
            elif not admitted_intent:
                # Missing or foreign attachments raise ValueError and fail the
                # run loudly instead of silently dropping the user's context.
                attachment_records = attachments.load_attachments(
                    self.settings.state_dir, session_id, attachment_ids
                )
                attachments.validate_for_provider(attachment_records, self.settings.provider)
                await self._emit(
                    run_id,
                    "message.user",
                    {
                        "message": self._semantic(
                            attachments.compose_message(prompt or "", attachment_records)
                        ),
                        "attachment_ids": attachment_ids,
                    },
                    sink,
                    role="user",
                    author="user",
                )
                attachments.mark_attachments_used(
                    self.settings.state_dir, session_id, attachment_ids
                )
            await self._emit(
                run_id,
                "workspace.checkpoint",
                {
                    "checkpoint": await asyncio.to_thread(execution_checkpoint, workspace_root),
                },
                sink,
            )
            if readonly_profile:
                skills = SkillCatalog(workspace_root, ())
            else:
                frozen_skills = intent.get("skill_snapshot")
                if frozen_skills is None:
                    frozen_skills = await self._freeze_skills(workspace, request)
                skills = self.skills.restore(workspace_root, frozen_skills)
                await self._emit(run_id, "skills.snapshot", frozen_skills, sink)
            loaded_skills: set[str] = set()

            async def load_skill(name: str) -> str:
                content = skills.load(name)
                if not content.startswith("Error:"):
                    skill = skills._resolve(name)
                    loaded_skills.add(skill.skill_id)
                    await self._emit(run_id, "skill.loaded", {
                        "skill_id": skill.skill_id, "version": skill.version,
                        "characters": len(content),
                    }, sink)
                return content

            async def read_skill_resource(
                name: str, path: str, offset: int = 0, limit: int = 12_000,
            ) -> str:
                skill = skills._resolve(name)
                if skill.skill_id not in loaded_skills:
                    raise SkillPackageError("Load Skill instructions before reading resources")
                return skills.read_resource(name, path, offset, limit)

            async def run_skill_script(
                name: str, path: str, args: list[str] | None = None,
            ) -> Any:
                skill = skills._resolve(name)
                if skill.skill_id not in loaded_skills:
                    raise SkillPackageError("Load Skill instructions before running scripts")
                return await execute_skill_script(
                    skills, name, path, args or [], working_root, command_executor,
                )
            if (
                self._enable_mcp
                and not readonly_profile
                and not execution_binding
                and not execution_config.get("disable_mcp")
            ):
                configs = manager.load_configs(
                    self._config_path
                    if workspace == self.default_workspace and self._config_path
                    else workspace_root / "mcp.json"
                )
                failures = await manager.connect_all(
                    configs,
                    timeout=self.settings.mcp_timeout,
                )
                for failure in failures:
                    await self._emit(
                        run_id,
                        "mcp.connection_failed",
                        self._clean(failure),
                        sink,
                    )
            if skills and any(tool.get("name") in {
                "load_skill", "read_skill_resource", "run_skill_script",
            } for tool in self.tools):
                raise ValueError("Skill tools are reserved for Runtime Skill loading")
            if any(tool.get("name") == "read_tool_result" for tool in self.tools):
                raise ValueError("read_tool_result is reserved for Runtime tool-result paging")
            if any(tool.get("name") == "todo_write" for tool in self.tools):
                raise ValueError("todo_write is reserved for Runtime task plans")
            if any(tool.get("name") == "search_knowledge" for tool in self.tools):
                raise ValueError("search_knowledge is reserved for Workspace knowledge retrieval")
            if any(tool.get("name") == "delegate_readonly" for tool in self.tools):
                raise ValueError("delegate_readonly is reserved for Runtime workers")
            skill_tools = [skills.tool()] if skills else []
            if any(r.path != "SKILL.md" for s in skills.skills for r in s.resources):
                skill_tools.append(skills.resource_tool())
            if any(r.path.startswith("scripts/") for s in skills.skills for r in s.resources):
                skill_tools.append(script_tool(skills))
            rag_tools: list[dict[str, Any]] = []
            rag_handlers: dict[str, Any] = {}
            if (workspace_root / INDEX_RELATIVE_PATH).is_file():
                try:
                    rag_embedder, rag_profile = provider_from_settings(self.settings)
                    if knowledge_index_available(workspace_root, rag_profile):
                        rag_tools.append(SEARCH_KNOWLEDGE_TOOL)
                        reranker = reranker_from_settings(self.settings)
                        rag_handlers["search_knowledge"] = (
                            remote_search_handler(
                                self, run_id, workspace_root, rag_profile,
                                settings=self.settings, reranker=reranker,
                            ) if rag_profile.provider == "openai_compatible" else
                            knowledge_search_handler(
                                workspace_root, rag_embedder, rag_profile, reranker=reranker,
                            )
                        )
                except LocalModelError as exc:
                    try:
                        fallback = knowledge_sparse_handler(workspace_root, exc.code)
                    except (OSError, RuntimeError, ValueError, sqlite3.Error):
                        fallback = None
                    if fallback is not None:
                        rag_tools.append(SEARCH_KNOWLEDGE_TOOL)
                        rag_handlers["search_knowledge"] = fallback
                    await self._emit(run_id, "retrieval.degraded", {
                        "reason": exc.code, "fallback": "sparse" if fallback else "unavailable",
                    }, sink)
                except (OSError, RuntimeError, ValueError):
                    pass

            async def write_task_plan(todos: object) -> str:
                state = self.store.task_plan_state(session_id)
                normalized, following = plan_update(
                    todos,
                    state["todos"] if state else None,
                    state["next_task_seq"] if state else None,
                )
                await self._emit(
                    run_id,
                    "task.plan_updated",
                    self._semantic(
                        {"todos": normalized, "next_task_seq": following, "step": steps}
                    ),
                    sink,
                    author="model",
                )
                return plan_summary(normalized)

            delegated_count = 0

            async def delegate_readonly(tasks: object) -> str:
                nonlocal delegated_count
                goals = validate_tasks(tasks)
                if delegated_count + len(goals) > 4:
                    raise ValueError("At most four read-only workers are allowed per Run")
                ensure_token_budget()
                remaining = token_limit - sum(usage.values()) if token_limit else 8_000
                child_limit = min(4_000, remaining // len(goals))
                if child_limit < 1:
                    raise ValueError("Insufficient parent token budget for workers")
                delegated_count += len(goals)
                snapshots: list[SourceSnapshot] = []
                children: list[tuple[str, asyncio.Task[RunResult], SourceSnapshot]] = []
                settled_reports: dict[str, tuple[bool, str | None]] = {}
                try:
                    for _goal in goals:

                        def make_snapshot() -> None:
                            snapshots.append(
                                SourceSnapshot.create(
                                    workspace_root, working_root, self.state_dir,
                                    checkpoint_files=self._checkpoint_files,
                                    checkpoint_directories=self._checkpoint_directories,
                                )
                            )

                        async with self._project_git(workspace.project_id):
                            await self._admission_check(make_snapshot)
                        snapshot = snapshots[-1]
                        if snapshot.checkpoint != snapshots[0].checkpoint:
                            raise ValueError("Source changed between worker snapshots")
                    for goal, snapshot in zip(goals, snapshots, strict=True):
                        child_session = self.create_session(
                            workspace_id=execution_workspace.workspace_id,
                            working_directory=str(working_root),
                        )
                        self.store.update_session(
                            child_session, title=f"Worker: {goal['role']}", archived=True
                        )
                        child_id = f"run_{uuid.uuid4().hex[:16]}"
                        profile = ReadonlyProfile(
                            parent_run_id=run_id,
                            role=goal["role"],
                            snapshot_id=snapshot.identity,
                            token_limit=child_limit,
                        )
                        child_intent = {
                            **intent,
                            "workspace_id": execution_workspace.workspace_id,
                            "project_id": workspace.project_id,
                            "source_workspace_id": workspace.source_workspace_id,
                            "session_id": child_session,
                            "mode": "plan",
                            "attachment_ids": [],
                            "parent_run_id": run_id,
                            "worker_role": profile.role,
                            "snapshot_id": snapshot.identity,
                        }
                        child_intent.pop("execution_config", None)
                        child_intent.pop("execution_binding", None)
                        child_turn = self.store.admit_run(
                            child_id,
                            child_session,
                            [
                                ("run.admitted", child_intent, "system", "runtime"),
                                (
                                    "message.user",
                                    {
                                        "message": {
                                            "role": "user",
                                            "content": profile.prompt(goal["goal"]),
                                        }
                                    },
                                    "user",
                                    "runtime",
                                ),
                                (
                                    "workspace.checkpoint",
                                    {"checkpoint": {"available": False}},
                                    "system",
                                    "runtime",
                                ),
                            ],
                        )
                        child_task = asyncio.create_task(
                            self._execute(
                                child_session,
                                child_id,
                                execution_workspace,
                                None,
                                None,
                                None,
                                None,
                                existing_turn_id=child_turn,
                                readonly_profile=profile,
                                snapshot_root=snapshot.root,
                            )
                        )
                        self._track_run_task(child_id, child_task)
                        children.append((child_id, child_task, snapshot))
                        await self._emit(
                            run_id,
                            "worker.started",
                            {
                                "child_run_id": child_id,
                                "child_session_id": child_session,
                                "role": profile.role,
                                "snapshot_id": snapshot.identity,
                                "source_checkpoint": snapshot.checkpoint,
                                "manifest": snapshot.manifest,
                                "skipped_files": snapshot.skipped,
                            },
                            sink,
                        )
                    timed_out = False
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(
                                *(child for _, child, _ in children), return_exceptions=True
                            ),
                            READONLY_WORKER_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        timed_out = True
                    reports = []
                    for child_id, child, snapshot in children:
                        record = self.store.get_run(child_id)
                        assert record is not None
                        report = None
                        report_error = "Worker timed out" if timed_out else record.get("error")
                        if not child.cancelled() and record["status"] == "completed":
                            try:
                                report = validate_report(child.result().output, snapshot)
                            except ValueError as exc:
                                report_error = str(exc)
                        reports.append(
                            {
                                "child_run_id": child_id,
                                "status": record["status"],
                                "snapshot_id": snapshot.identity,
                                "report": report,
                                "error": report_error,
                            }
                        )
                        settled_reports[child_id] = (report is not None, report_error)
                    text = json.dumps({"workers": reports}, ensure_ascii=False)
                    return "Error: " + text if any(r["report"] is None for r in reports) else text
                finally:
                    for _, child, _ in children:
                        if not child.done():
                            child.cancel()
                    if children:
                        settling = asyncio.gather(
                            *(child for _, child, _ in children), return_exceptions=True
                        )
                        while not settling.done():
                            with suppress(asyncio.CancelledError):
                                await asyncio.shield(settling)
                    for snapshot in snapshots:
                        snapshot.cleanup()
                    for child_id, _, _ in children:
                        record = self.store.get_run(child_id)
                        child_usage = record.get("usage", {}) if record else {}
                        for key in usage:
                            usage[key] += max(0, int(child_usage.get(key, 0)))
                        await self._emit(
                            run_id,
                            "worker.completed",
                            {
                                "child_run_id": child_id,
                                "status": record["status"] if record else "failed",
                                "usage": child_usage,
                                "report_valid": settled_reports.get(child_id, (False, None))[0],
                                "error": settled_reports.get(child_id, (False, "Worker stopped"))[
                                    1
                                ],
                            },
                            None,
                        )

            worker_tools = (
                [READONLY_WORKER_TOOL]
                if self.settings.workers_enabled and workspace.git_root
                and not readonly_profile and not snapshot_execution
                else []
            )
            tools = [
                *self.tools,
                READ_TOOL_RESULT_TOOL,
                TODO_WRITE_TOOL,
                *skill_tools,
                *rag_tools,
                *manager.tools,
                *worker_tools,
            ]
            tools = run_policy.filter_tool_catalog(tools, mode)
            handlers = {
                **self.handlers,
                "read_tool_result": tool_result_reader(self.store, session_id),
                "todo_write": write_task_plan,
                **({"load_skill": load_skill, "read_skill_resource": read_skill_resource,
                    "run_skill_script": run_skill_script} if skills else {}),
                **rag_handlers,
                **manager.handlers,
                **({"delegate_readonly": delegate_readonly} if worker_tools else {}),
            }
            if readonly_profile:
                names = {"read_file", "glob", "compact", "read_tool_result"}
                tools = [tool for tool in [*TOOLS, READ_TOOL_RESULT_TOOL] if tool["name"] in names]
                handlers = {
                    name: handler
                    for name, handler in {
                        **HANDLERS,
                        "read_tool_result": tool_result_reader(self.store, session_id),
                    }.items()
                    if name in names
                }
            if mode == "plan":
                # The catalog never advertised side-effecting tools, so a
                # hallucinated call must stay a no-op: restrict execution to the
                # visible names instead of trusting the model to comply.
                visible = {tool.get("name") for tool in tools}
                handlers = {name: handler for name, handler in handlers.items() if name in visible}
            tools = production_tool_catalog(tools)
            executor = ToolExecutor(
                handlers,
                {
                    "read_tool_result",
                    "todo_write",
                    *({"delegate_readonly"} if worker_tools else set()),
                    *({"search_knowledge"} if rag_tools else set()),
                    *manager.readonly_tools,
                    *({"load_skill", "read_skill_resource"} if skills else set()),
                },
                command_executor,
                tools=tools,
            )
            runtime_readonly_tools = set(BASE_RUNTIME_READONLY_TOOLS)
            if worker_tools:
                runtime_readonly_tools.add("delegate_readonly")
            if skills:
                runtime_readonly_tools.update({"load_skill", "read_skill_resource"})
            instructions = self._clean(ContextBuilder.instructions(workspace_root))
            skill_prompt = skills.prompt()
            mode_note = run_policy.mode_system_note(mode)
            base_system = (
                "You are Eventide. Use tools to finish work, respect permissions, and never "
                "claim success without evidence.\n"
                f"Workspace: {workspace_root}\nWorking directory: {working_root}\n"
                f"Project instructions:\n{instructions}"
                + (f"\n\n{skill_prompt}" if skill_prompt else "")
                + (f"\n\n{mode_note}" if mode_note else "")
            )
            catalog_hash = digest(tools)
            tool_catalog_chars = tool_catalog_size(tools)
            await self._emit(
                run_id,
                "context.configured",
                {
                    "instruction_hash": digest(instructions),
                    "skill_catalog_hash": skills.catalog_hash,
                    "skill_count": len(skills.skills),
                    "tool_catalog_hash": catalog_hash,
                },
                sink,
            )

            async def approve(approval_id: str, call: ToolCall, reason: str) -> bool:
                await self._emit(
                    run_id,
                    "approval.required",
                    {
                        "approval_id": approval_id,
                        "tool": call.name,
                        "arguments": self._clean(call.arguments),
                        "reason": reason,
                    },
                    sink,
                )
                if run_policy.should_auto_approve(mode):
                    # Agent mode owns the decision and the audit fact; the
                    # handler (e.g. the HTTP broker) is never consulted, so
                    # nothing waits on a user who was never asked.
                    await self._emit(
                        run_id,
                        "approval.resolved",
                        {"approval_id": approval_id, "approved": True, "auto": True},
                        sink,
                        author="runtime",
                    )
                    return True
                approved = False
                if approval_handler:
                    with suppress(asyncio.TimeoutError):
                        approved = await asyncio.wait_for(
                            approval_handler(approval_id, call, reason),
                            self.settings.approval_timeout,
                        )
                await self._emit(
                    run_id,
                    "approval.resolved",
                    {
                        "approval_id": approval_id,
                        "approved": approved,
                    },
                    sink,
                    author="user" if approval_handler else "runtime",
                )
                return approved

            def record_usage(response: ModelResponse) -> None:
                for key in usage:
                    usage[key] += max(0, int(response.usage.get(key, 0)))

            def ensure_token_budget() -> None:
                totals = build_usage_ledger(
                    self.store.usage_events(run_id=run_id)
                )["reported_totals"]
                usage.update({key: totals.get(key, 0) for key in usage})
                if token_limit and sum(usage.values()) >= token_limit:
                    raise _RunTokenBudgetExceeded(
                        f"Run token budget reached ({sum(usage.values())}/"
                        f"{token_limit}); session parked"
                    )

            async def complete_summary(summary_request: ModelRequest) -> ModelResponse:
                ensure_token_budget()
                await self._emit(
                    run_id,
                    "context.summary_requested",
                    {
                        "model": summary_request.model,
                        "request_chars": request_size(
                            summary_request.system, summary_request.messages, summary_request.tools
                        ),
                        "context_hash": digest(summary_request.messages),
                    },
                    sink,
                )
                response = await self._complete_with_retry(
                    summary_request, run_id, sink, kind="context_summary"
                )
                record_usage(response)
                await self._emit(
                    run_id,
                    "context.summary_generated",
                    {
                        "text": self._semantic(response.text),
                        "stop_reason": response.stop_reason,
                        "usage": response.usage,
                    },
                    sink,
                    author="model",
                )
                return response

            step_budget_exhausted = False
            # The run budget stays the hard ceiling; this only stops one task from eating it.
            task_park: str | None = None
            active_task_id: str | None = None
            active_since_step = 1
            parameter_repair_used = False

            async def record_unknown(call: ToolCall, content: str) -> None:
                await self._emit(
                    run_id,
                    "tool.outcome_unknown",
                    {
                        "call_id": call.id,
                        "name": call.name,
                        "readonly": snapshot_execution,
                        "workspace_id": execution_workspace.workspace_id,
                        **({"snapshot_id": execution_binding["snapshot_id"]}
                           if snapshot_execution and execution_binding else {}),
                        "error": self._semantic(content),
                    },
                    sink,
                )

            resume_note = (
                f"Resuming task {resumed_task_id} after a park. Continue it; do not repeat "
                "completed work."
                if resumed_task_id
                else ""
            )
            # Bound before the loop so the task-plan handler can stamp its own step.
            steps = 0
            for steps in range(1, max_steps + 1):
                ensure_token_budget()
                plan_state = self.store.task_plan_state(session_id)
                plan_todos = (plan_state["todos"] if plan_state else None) or None
                current_task = task_plan_active(plan_todos)
                if current_task != active_task_id:
                    active_task_id, active_since_step = current_task, steps
                if (
                    self.settings.task_max_steps
                    and active_task_id
                    and steps - active_since_step + 1 > self.settings.task_max_steps
                ):
                    task_park = self._clean(
                        f"Task {active_task_id} reached its step budget "
                        f"({self.settings.task_max_steps}); session parked"
                    )
                    break
                plan_context = task_plan_prompt(plan_todos)
                if resume_note:
                    plan_context = f"{resume_note}\n\n{plan_context}"
                system = base_system + (f"\n\n{plan_context}" if plan_context else "")
                messages, compacted, trimmed = await self.context_builder.build(
                    session_id,
                    provider=self._provider(),
                    provider_name=self.settings.provider,
                    model=selected_model,
                    budget=self.settings.context_limit,
                    force=force_compact,
                    secrets=(self.settings.api_key or "",),
                    system=system,
                    tools=tools,
                    complete_summary=complete_summary,
                )
                force_compact = False
                if compacted:
                    await self._emit(
                        run_id,
                        "context.compacted",
                        {key: value for key, value in compacted.items() if key != "usage"},
                        sink,
                    )
                if trimmed:
                    await self._emit(run_id, "context.trimmed", trimmed, sink)
                await self._emit(
                    run_id,
                    "model.request",
                    {
                        "step": steps,
                        "message_count": len(messages),
                        "tool_count": len(tools),
                        "context_hash": digest(messages),
                        "tool_catalog_hash": catalog_hash,
                        "model": selected_model,
                        "request_chars": request_size(system, messages, tools),
                        "tool_catalog_chars": tool_catalog_chars,
                    },
                    sink,
                )
                ensure_token_budget()
                provider_messages = attachments.hydrate_messages(
                    self.settings.state_dir, session_id, messages
                )
                response = await self._complete_with_retry(
                    ModelRequest(
                        system,
                        provider_messages,
                        tools,
                        selected_model,
                        max_tokens,
                        dict(generation),
                    ),
                    run_id,
                    sink,
                )
                record_usage(response)
                # Persist exactly the normalized response that the next model request sees.
                raw_content = response.content_blocks()
                calls = [
                    ToolCall(b["id"], b["name"], b["input"])
                    for b in raw_content
                    if b["type"] == "tool_use"
                ]
                content = self._semantic(raw_content)
                prior_ids = {
                    b["id"]
                    for e in self.store.run_events(run_id)
                    if e["type"] == "model.response"
                    for b in e["payload"].get("message", {}).get("content", [])
                    if b.get("type") == "tool_use"
                }
                if len({c.id for c in calls}) != len(calls) or prior_ids & {c.id for c in calls}:
                    raise ValueError("Duplicate tool call IDs in model response")
                await self._emit(
                    run_id,
                    "model.response",
                    {
                        "step": steps,
                        "text": self._semantic(response.text),
                        "message": {"role": "assistant", "content": content},
                        "stop_reason": response.stop_reason,
                        "tool_calls": len(calls),
                        "usage": response.usage,
                    },
                    sink,
                    role="assistant",
                    author="model",
                )
                if response.stop_reason in TRUNCATION_REASONS:
                    # Even syntactically complete arguments in a truncated response
                    # are not a dispatch authorization. Pair them as unexecuted errors.
                    for call in calls:
                        await self._emit(run_id, "tool.abandoned", {
                            "call_id": call.id, "name": call.name, "is_error": True,
                            "content": "Tool not dispatched: model response was incomplete",
                            "reason": "response_truncated", "dispatched": False,
                        }, sink)
                    raise RuntimeError(
                        f"Model output truncated ({response.stop_reason}); raise "
                        f"max_output_tokens / EVENTIDE_MAX_TOKENS (currently {max_tokens})"
                    )
                if not calls:
                    output = self._semantic(response.text)
                    break
                invalid_arguments = False
                for call in calls:
                    tool_count += 1
                    # MCP and unknown/custom tools are always conservative recovery boundaries.
                    readonly = call.name in runtime_readonly_tools
                    await self._emit(
                        run_id,
                        "tool.prepared",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": self._semantic(call.arguments),
                            "readonly": readonly,
                        },
                        sink,
                    )
                    execution = asyncio.create_task(
                        executor.execute(
                            call,
                            ToolContext(working_root, run_id, self.policy, approve),
                        )
                    )
                    try:
                        result = await asyncio.shield(execution)
                    except asyncio.CancelledError:
                        # Async commands/MCP calls can stop cooperatively. Sync file/custom
                        # handlers run in threads, so retain ownership until they finish.
                        if call.name == "bash" or inspect.iscoroutinefunction(
                            handlers.get(call.name)
                        ):
                            execution.cancel()
                        while not execution.done():
                            with suppress(asyncio.CancelledError):
                                await asyncio.shield(execution)
                        if not execution.cancelled():
                            settled = execution.result()
                            if settled.metadata.get("outcome_unknown"):
                                await record_unknown(call, settled.content)
                            else:
                                await self._emit(
                                    run_id,
                                    "tool.completed",
                                    {
                                        "call_id": call.id,
                                        "name": call.name,
                                        "content": self._semantic(settled.content),
                                        "is_error": settled.is_error,
                                        "metadata": self._semantic(settled.metadata),
                                    },
                                    sink,
                                )
                        raise
                    if result.metadata.get("outcome_unknown"):
                        await record_unknown(call, result.content)
                        raise _UnknownToolOutcome(result.content)
                    if result.metadata.get("code") == "invalid_arguments":
                        invalid_arguments = True
                    await self._emit(
                        run_id,
                        "tool.completed",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "content": self._semantic(result.content),
                            "is_error": result.is_error,
                            "metadata": self._semantic(result.metadata),
                        },
                        sink,
                    )
                    if result.metadata.get("code") == "budget_exceeded":
                        raise BudgetExceeded(result.metadata.get("violations", []))
                    if call.name == "compact" and not result.is_error:
                        force_compact = True
                    if not readonly:
                        await self._emit(
                            run_id,
                            "workspace.checkpoint",
                            {
                                "checkpoint": await asyncio.to_thread(
                                    execution_checkpoint, workspace_root
                                ),
                            },
                            sink,
                        )
                if invalid_arguments:
                    if parameter_repair_used:
                        raise RuntimeError(
                            "Tool argument repair failed; no further automatic repair"
                        )
                    parameter_repair_used = True
            else:
                step_budget_exhausted = True
            if step_budget_exhausted:
                park_kind = "step_budget"
                park_message = self._clean(f"Maximum agent steps exceeded ({max_steps})")
            else:
                park_kind = "task_step_budget" if task_park else None
                park_message = task_park
            if park_kind:
                # Every tool result is already committed, so park instead of failing:
                # the user can Continue into a fresh turn with a new step budget.
                events = self.store.run_events(run_id)
                if not events or events[-1]["type"] != "workspace.checkpoint":
                    await self._emit(
                        run_id,
                        "workspace.checkpoint",
                        {
                            "checkpoint": await asyncio.to_thread(
                                execution_checkpoint, workspace_root
                            ),
                        },
                        sink,
                    )
                status, error = "interrupted", park_message
            else:
                if execution_binding:
                    await asyncio.to_thread(self._execution_paths, execution_binding)
                status, error = "completed", None
        except _UnknownToolOutcome as exc:
            status, error, park_kind = "interrupted", str(exc), "unknown_tool_outcome"
        except (_RunTokenBudgetExceeded, BudgetExceeded) as exc:
            await self._emit(
                run_id,
                "workspace.checkpoint",
                {"checkpoint": await asyncio.to_thread(execution_checkpoint, workspace_root)},
                sink,
            )
            steps = max(
                (
                    e["payload"].get("step", 0)
                    for e in self.store.run_events(run_id)
                    if e["type"] == "model.response"
                ),
                default=0,
            )
            status, error, park_kind = "interrupted", str(exc), "token_budget"
        except asyncio.CancelledError:
            # A cancel can land between a completed side-effecting tool and the
            # checkpoint that records its result: they are two separate facts, and
            # only the first one gets written. Record the workspace here so the parked
            # run stays resumable, exactly as the step-budget park path does. This
            # does not loosen recovery -- Continue still compares the checkpoint
            # against the live workspace and refuses if it changed.
            with suppress(asyncio.CancelledError):
                events = self.store.run_events(run_id)
                if not events or events[-1]["type"] != "workspace.checkpoint":
                    await self._emit(
                        run_id,
                        "workspace.checkpoint",
                        {
                            "checkpoint": await asyncio.to_thread(
                                execution_checkpoint, workspace_root
                            ),
                        },
                        sink,
                    )
            unknown = (
                any(event["type"] == "tool.outcome_unknown"
                    for event in self.store.run_events(run_id))
                if execution_binding else
                self.store.workspace_has_unknown_outcome(execution_workspace.workspace_id)
            )
            await self._emit(
                run_id,
                "run.interrupted",
                {
                    "error": "Run cancelled; tool outcome unknown, execution blocked"
                    if unknown
                    else "Run cancelled; session parked",
                    "reason": "unknown_tool_outcome"
                    if unknown
                    else "cancelled",
                    "usage": usage,
                    "steps": steps,
                    "tool_calls": tool_count,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                },
                sink,
            )
            raise
        except Exception as exc:
            status, error = "failed", self._clean(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await manager.close()
            except Exception as exc:
                # Closing an optional integration must not overwrite completed work.
                with suppress(Exception):
                    await self._emit(
                        run_id,
                        "mcp.close_failed",
                        {"error": self._clean(f"MCP close failed: {exc}")},
                        sink,
                    )
        duration = (time.perf_counter() - started) * 1000
        await self._emit(
            run_id,
            f"run.{status}",
            {
                "output": output,
                "error": error,
                "steps": steps,
                "tool_calls": tool_count,
                "duration_ms": duration,
                "usage": usage,
                "reason": park_kind,
            },
            sink,
        )
        return RunResult(
            run_id,
            session_id,
            status,
            error or output,
            steps,
            tool_count,
            duration,
            usage,
            turn_id,
            continuation_of,
        )
