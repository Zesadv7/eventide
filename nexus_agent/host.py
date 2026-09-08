"""Single execution owner for Workspace-bound sessions and canonical facts."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Protocol

from nexus_agent.config import SUPPORTED_PROVIDERS, Settings, normalize_provider
from nexus_agent.context_builder import ContextBuilder
from nexus_agent.executor import ApprovalHandler, ToolContext, ToolExecutor
from nexus_agent.mcp.client import MCPManager
from nexus_agent.models import (
    ContinuationRequest,
    ModelRequest,
    ModelResponse,
    RunRequest,
    RunResult,
    ToolCall,
    WorkspaceRecord,
    WorkspaceTarget,
)
from nexus_agent.normalization import normalize, redact
from nexus_agent.policy import PolicyEngine
from nexus_agent.providers import Provider, ProviderError, build_provider
from nexus_agent.secrets import SecretBox, SecretKeyError
from nexus_agent.store import RuntimeStore
from nexus_agent.tools.runtime_catalog import HANDLERS, TOOLS
from nexus_agent.workspace import (
    HostLease,
    canonical_workspace,
    digest,
    git_metadata,
    workspace_checkpoint,
)

# stop_reason values that mean the provider cut the answer off at max_tokens.
TRUNCATION_REASONS = {"max_tokens", "length", "max_output_tokens", "incomplete"}


class EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class SessionManager:
    def __init__(self, store: RuntimeStore):
        self.store = store

    def create(self, workspace_id: str, session_id: str | None = None) -> str:
        identity = session_id or f"session_{uuid.uuid4().hex[:16]}"
        self.store.create_session(identity, workspace_id)
        return identity

    def workspace(self, session_id: str) -> WorkspaceRecord:
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        return self.store.get_workspace(session["workspace_id"])


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
        self._lease = HostLease(store.path.parent if store else self.settings.state_dir)
        try:
            self.store = store or RuntimeStore(self.settings.state_dir / "runtime.sqlite")
            self.store.recover_interrupted()
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
        self.tools = list(TOOLS if tools is None else tools)
        self.handlers = dict(HANDLERS if handlers is None else handlers)
        self.policy = PolicyEngine()
        self.executor = ToolExecutor(self.handlers)
        self._workspace_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._configuring = False
        self._config_path: Path | None = None
        self._enable_mcp = enable_mcp

    def _provider(self) -> Provider:
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
                return self.store.get_workspace(target.workspace_id)
            target = str(target.path)
        if isinstance(target, str) and target.startswith("ws_"):
            return self.store.get_workspace(target)
        root, git_root = canonical_workspace(Path(target))
        return self.store.register_workspace(root, git_root)

    def create_session(
        self, session_id: str | None = None, *, workspace_id: str | None = None
    ) -> str:
        if session_id and self.store.get_session(session_id) and workspace_id is None:
            return session_id
        return self.sessions.create(workspace_id or self.default_workspace.workspace_id, session_id)

    def session_status(self, session_id: str) -> dict[str, Any]:
        workspace = self.sessions.workspace(session_id)
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        run = self.store.latest_run(session_id)
        events = self.store.session_events(session_id)
        first_intent = next(
            (
                event["payload"].get("message", {}).get("content")
                for event in events
                if event["type"] in {"message.user", "message.imported"}
            ),
            None,
        )
        title = str(first_intent or "New session").replace("\n", " ").strip()
        updated_at = max((event["ts"] for event in events), default=session["created_at"])
        return {
            "session_id": session_id,
            "workspace_id": workspace.workspace_id,
            "created_at": session["created_at"],
            "updated_at": updated_at,
            "title": title[:96],
            "status": "parked"
            if run and run["status"] == "interrupted"
            else run["status"]
            if run
            else "idle",
            "latest_run": run,
        }

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.store.get_workspace(workspace_id)
        metadata = git_metadata(Path(workspace.path))
        return {**asdict(workspace), **metadata}

    def session_runs(self, session_id: str) -> list[dict[str, Any]]:
        self.sessions.workspace(session_id)
        return self.store.list_runs(session_id)

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
        if closer is not None:
            result = closer()
            if inspect.isawaitable(result):
                await result

    def _load_persisted_provider(self) -> None:
        record = self.store.get_provider_config()
        if not record:
            return
        api_key = self._startup_settings.api_key
        encrypted = record.get("api_key_ciphertext")
        self._provider_secret_error = None
        if encrypted:
            try:
                api_key = self._secret_box.decrypt(str(encrypted))
            except SecretKeyError as exc:
                api_key = None
                self._provider_secret_error = str(exc)
        self.settings = replace(
            self._startup_settings,
            provider=str(record["provider"]),
            base_url=str(record["base_url"]) if record["base_url"] else None,
            model=str(record["model"]),
            api_key=api_key,
        )

    def provider_config_status(self) -> dict[str, Any]:
        record = self.store.get_provider_config()
        encrypted = bool(record and record.get("api_key_ciphertext"))
        if self._provider_secret_error:
            key_status = "decrypt_error"
            key_source = "decrypt_error"
        elif self.settings.api_key:
            key_status = "configured"
            key_source = "sqlite_encrypted" if encrypted else "environment"
        else:
            key_status = "missing"
            key_source = "none"
        return {
            "provider": self.settings.provider,
            "base_url": self.settings.base_url,
            "model": self.settings.model,
            "api_key_configured": key_status == "configured",
            "api_key_status": key_status,
            "api_key_source": key_source,
            "persistence": "sqlite_encrypted" if record else "environment",
            "updated_at": record.get("updated_at") if record else None,
            "configuration_error": self._provider_secret_error,
        }

    @contextmanager
    def _configuration_change(self) -> Iterator[None]:
        if self._configuring or self._active_tasks:
            raise ValueError("Wait for active runs or configuration changes")
        self._configuring = True
        try:
            yield
        finally:
            self._configuring = False

    async def configure_provider(
        self,
        *,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        model: str,
        persist: bool = False,
        clear_api_key: bool = False,
    ) -> None:
        """Replace live provider settings and optionally persist an encrypted credential."""
        with self._configuration_change():
            if self._active_tasks:
                raise ValueError("Wait for active runs before changing provider configuration")
            normalized = normalize_provider(provider)
            if normalized not in SUPPORTED_PROVIDERS:
                raise ValueError(f"Unsupported provider '{provider}'")
            if not model.strip():
                raise ValueError("Model cannot be empty")
            normalized_url = base_url.strip() if base_url else None
            selected_key = api_key.strip() if api_key and api_key.strip() else self.settings.api_key
            if persist:
                current = self.store.get_provider_config() or {}
                encrypted = current.get("api_key_ciphertext")
                if self._provider_secret_error and encrypted and not api_key and not clear_api_key:
                    raise SecretKeyError(
                        "已保存的 API Key 无法解密；请输入新密钥覆盖，或先清除旧密钥"
                    )
                if clear_api_key:
                    encrypted = None
                    selected_key = self._startup_settings.api_key
                elif api_key and api_key.strip():
                    encrypted = self._secret_box.encrypt(api_key.strip())
                self.store.save_provider_config(
                    provider=normalized,
                    base_url=normalized_url,
                    model=model.strip(),
                    api_key_ciphertext=encrypted,
                )
            await self._close_provider()
            self.settings = replace(
                self.settings,
                provider=normalized,
                api_key=selected_key,
                base_url=normalized_url,
                model=model.strip(),
            )
            self._provider_secret_error = None
            self.provider = None

    async def reset_provider_configuration(self) -> None:
        with self._configuration_change():
            if self._active_tasks:
                raise ValueError("Wait for active runs before changing provider configuration")
            await self._close_provider()
            self.store.delete_provider_config()
            self.settings = self._startup_settings
            self._provider_secret_error = None
            self.provider = None

    async def probe_provider(
        self,
        *,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        model: str,
    ) -> ModelResponse:
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
        probe = build_provider(probe_settings)
        try:
            return await probe.complete(
                ModelRequest(
                    system="你是连接检查助手。",
                    messages=[{"role": "user", "content": "请只回复：连接成功"}],
                    tools=[],
                    model=probe_settings.model,
                    max_tokens=32,
                )
            )
        finally:
            closer = getattr(probe, "close", None) or getattr(probe, "aclose", None)
            if closer:
                result = closer()
                if inspect.isawaitable(result):
                    await result

    def _clean(self, value: Any) -> Any:
        return normalize(redact(value), (self.settings.api_key or "",))

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
        self, request: ModelRequest, run_id: str, sink: EventSink | None
    ) -> ModelResponse:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await self._provider().complete(request)
            except ProviderError as exc:
                last_error = exc
                await self._emit(
                    run_id,
                    "model.retry",
                    {
                        "attempt": attempt + 1,
                        "retryable": exc.retryable,
                        "error": self._clean(str(exc)),
                    },
                    sink,
                )
                if not exc.retryable or attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError(str(last_error))

    async def run(
        self,
        request: RunRequest,
        sink: EventSink | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> RunResult:
        session_id = self.create_session(request.session_id)
        return await self._admit(session_id, request, None, sink, approval_handler)

    async def continue_session(
        self,
        request: ContinuationRequest | str,
        sink: EventSink | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> RunResult:
        session_id = request if isinstance(request, str) else request.session_id
        return await self._admit(session_id, None, True, sink, approval_handler)

    async def _admit(
        self,
        session_id: str,
        request: RunRequest | None,
        continuation: bool | None,
        sink: EventSink | None,
        approval_handler: ApprovalHandler | None,
    ) -> RunResult:
        await self.initialize()
        if self._configuring:
            raise ValueError("Provider configuration is changing")
        workspace = self.sessions.workspace(session_id)
        root = Path(workspace.path)
        if not root.is_dir() or root.resolve() != root:
            raise ValueError("Workspace missing or path identity changed")
        task = asyncio.current_task()
        assert task is not None
        self._active_tasks.add(task)
        try:
            async with self._workspace_locks[workspace.workspace_id]:
                if self._closed:
                    raise RuntimeError("Host is closed")
                previous = self.store.latest_run(session_id)
                if continuation:
                    await asyncio.to_thread(self._validate_continue, workspace, previous)
                elif previous and previous["status"] == "interrupted":
                    raise ValueError("Session is parked; use Continue after resolving interruption")
                run_id = (
                    request.run_id if request and request.run_id else f"run_{uuid.uuid4().hex[:16]}"
                )
                source_id = previous["id"] if continuation and previous else None
                return await self._execute(
                    session_id,
                    run_id,
                    workspace,
                    request.prompt if request else None,
                    source_id,
                    sink,
                    approval_handler,
                )
        finally:
            self._active_tasks.discard(task)

    def _validate_continue(
        self,
        workspace: WorkspaceRecord,
        previous: dict[str, Any] | None,
    ) -> None:
        if not previous or previous["status"] != "interrupted":
            raise ValueError("Only an interrupted, parked session can Continue")
        if not workspace.git_root:
            raise ValueError("Continue requires a Git workspace")
        evidence = workspace_checkpoint(Path(workspace.path))
        if evidence is None or evidence != previous["checkpoint"]:
            raise ValueError("Workspace changed or checkpoint unavailable; session remains parked")
        events = self.store.run_events(previous["id"])
        last_checkpoint = max(
            (e["session_seq"] for e in events if e["type"] == "workspace.checkpoint"), default=0
        )
        for event in events:
            if event["type"] == "tool.completed" and event["session_seq"] > last_checkpoint:
                name = event["payload"]["name"]
                if name not in {"read_file", "glob", "compact"}:
                    raise ValueError("Missing post-tool checkpoint; session remains parked")
        for operation in previous["tools"].values():
            if operation["status"] == "prepared" and not operation.get("readonly", False):
                raise ValueError("Unknown tool outcome; session remains parked")

    async def _execute(
        self,
        session_id: str,
        run_id: str,
        workspace: WorkspaceRecord,
        prompt: str | None,
        continuation_of: str | None,
        sink: EventSink | None,
        approval_handler: ApprovalHandler | None,
    ) -> RunResult:
        started = time.perf_counter()
        turn_id = self.store.create_run(run_id, session_id, continuation_of=continuation_of)
        root = Path(workspace.path)
        manager = MCPManager()
        usage = {"input_tokens": 0, "output_tokens": 0}
        output, steps, tool_count = "", 0, 0
        force_compact = False
        try:
            await self._emit(
                run_id,
                "run.started",
                {
                    "session_id": session_id,
                    "workspace_id": workspace.workspace_id,
                    "provider": self.settings.provider,
                    "model": self.settings.model,
                    "continuation_of": continuation_of,
                },
                sink,
            )
            if continuation_of:
                previous = self.store.get_run(continuation_of)
                assert previous is not None
                # Resolve every unpaired advertised call, including calls never dispatched.
                old_events = self.store.run_events(continuation_of)
                completed = {
                    e["payload"]["call_id"] for e in old_events if e["type"] == "tool.completed"
                }
                for event in old_events:
                    if event["type"] != "model.response":
                        continue
                    for block in event["payload"].get("message", {}).get("content", []):
                        if block.get("type") == "tool_use" and block["id"] not in completed:
                            await self._emit(
                                run_id,
                                "tool.abandoned",
                                {
                                    "call_id": block["id"],
                                    "name": block["name"],
                                    "is_error": True,
                                    "content": "Abandoned after interruption; not replayed.",
                                },
                                sink,
                            )
                for approval_id in previous["pending_approvals"]:
                    await self._emit(
                        run_id,
                        "approval.resolved",
                        {
                            "approval_id": approval_id,
                            "approved": False,
                            "reason": "Host interrupted",
                        },
                        sink,
                        author="recovery",
                    )
            else:
                await self._emit(
                    run_id,
                    "message.user",
                    {
                        "message": self._clean({"role": "user", "content": prompt}),
                    },
                    sink,
                    role="user",
                    author="user",
                )
            await self._emit(
                run_id,
                "workspace.checkpoint",
                {
                    "checkpoint": await asyncio.to_thread(workspace_checkpoint, root),
                },
                sink,
            )
            if self._enable_mcp:
                configs = manager.load_configs(
                    self._config_path
                    if workspace == self.default_workspace and self._config_path
                    else root / "mcp.json"
                )
                await manager.connect_all(configs)
            tools = [*self.tools, *manager.tools]
            handlers = {**self.handlers, **manager.handlers}
            executor = ToolExecutor(
                handlers, manager.readonly_tools, self.executor.command_executor
            )
            instructions = self._clean(ContextBuilder.instructions(root))
            system = (
                "You are Nexus Agent. Use tools to finish work, respect permissions, and never "
                "claim success without evidence.\n"
                f"Workspace: {root}\nProject instructions:\n{instructions}"
            )
            catalog_hash = digest(tools)
            await self._emit(
                run_id,
                "context.configured",
                {
                    "instruction_hash": digest(instructions),
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

            step_budget_exhausted = False
            for steps in range(1, self.settings.max_steps + 1):
                messages, compacted, trimmed = await self.context_builder.build(
                    session_id,
                    provider=self._provider(),
                    provider_name=self.settings.provider,
                    model=self.settings.model,
                    budget=self.settings.context_limit,
                    force=force_compact,
                    secrets=(self.settings.api_key or "",),
                )
                force_compact = False
                if compacted:
                    await self._emit(run_id, "context.compacted", compacted, sink)
                    for key in usage:
                        usage[key] += int(compacted.get("usage", {}).get(key, 0))
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
                    },
                    sink,
                )
                response = await self._complete_with_retry(
                    ModelRequest(
                        system,
                        messages,
                        tools,
                        self.settings.model,
                        self.settings.max_tokens,
                    ),
                    run_id,
                    sink,
                )
                # Persist exactly the normalized response that the next model request sees.
                content = self._clean(response.content_blocks())
                calls = [
                    ToolCall(b["id"], b["name"], b["input"])
                    for b in content
                    if b["type"] == "tool_use"
                ]
                prior_ids = {
                    b["id"]
                    for e in self.store.run_events(run_id)
                    if e["type"] == "model.response"
                    for b in e["payload"].get("message", {}).get("content", [])
                    if b.get("type") == "tool_use"
                }
                if len({c.id for c in calls}) != len(calls) or prior_ids & {c.id for c in calls}:
                    raise ValueError("Duplicate tool call IDs in model response")
                for key in usage:
                    usage[key] += int(response.usage.get(key, 0))
                await self._emit(
                    run_id,
                    "model.response",
                    {
                        "step": steps,
                        "text": self._clean(response.text),
                        "message": {"role": "assistant", "content": content},
                        "stop_reason": response.stop_reason,
                        "tool_calls": len(calls),
                        "usage": response.usage,
                    },
                    sink,
                    role="assistant",
                    author="model",
                )
                if not calls:
                    if response.stop_reason in TRUNCATION_REASONS:
                        # A truncated final answer is not a finished task; fail loudly
                        # instead of reporting success with half the output.
                        raise RuntimeError(
                            f"Model output truncated ({response.stop_reason}); raise "
                            f"NEXUS_MAX_TOKENS (currently {self.settings.max_tokens})"
                        )
                    output = self._clean(response.text)
                    break
                for call in calls:
                    tool_count += 1
                    # MCP and unknown/custom tools are always conservative recovery boundaries.
                    readonly = call.name in {"read_file", "glob", "compact"}
                    await self._emit(
                        run_id,
                        "tool.prepared",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "readonly": readonly,
                        },
                        sink,
                    )
                    execution = asyncio.create_task(
                        executor.execute(
                            call,
                            ToolContext(root, run_id, self.policy, approve),
                        )
                    )
                    try:
                        result = await asyncio.shield(execution)
                    except asyncio.CancelledError:
                        # Do not release workspace ownership while a host thread still writes.
                        await execution
                        raise
                    await self._emit(
                        run_id,
                        "tool.completed",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "content": self._clean(result.content),
                            "is_error": result.is_error,
                        },
                        sink,
                    )
                    if call.name == "compact" and not result.is_error:
                        force_compact = True
                    if not readonly:
                        await self._emit(
                            run_id,
                            "workspace.checkpoint",
                            {
                                "checkpoint": await asyncio.to_thread(workspace_checkpoint, root),
                            },
                            sink,
                        )
            else:
                step_budget_exhausted = True
            if step_budget_exhausted:
                # Every tool result is already committed, so park instead of failing:
                # the user can Continue into a fresh turn with a new step budget.
                events = self.store.run_events(run_id)
                if not events or events[-1]["type"] != "workspace.checkpoint":
                    await self._emit(
                        run_id,
                        "workspace.checkpoint",
                        {
                            "checkpoint": await asyncio.to_thread(workspace_checkpoint, root),
                        },
                        sink,
                    )
                status, error = "interrupted", self._clean(
                    f"Maximum agent steps exceeded ({self.settings.max_steps})"
                )
            else:
                status, error = "completed", None
        except asyncio.CancelledError:
            await self._emit(
                run_id, "run.interrupted", {"error": "Run cancelled; session parked"}, sink
            )
            raise
        except Exception as exc:
            status, error = "failed", self._clean(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await manager.close()
            except Exception as exc:
                status, error = "failed", self._clean(f"MCP close failed: {exc}")
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
