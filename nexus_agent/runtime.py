"""Async, provider-neutral Nexus Agent runtime."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

from nexus_agent.config import Settings
from nexus_agent.executor import ApprovalHandler, ToolContext, ToolExecutor
from nexus_agent.mcp.client import MCPManager
from nexus_agent.models import ModelRequest, ModelResponse, RunRequest, RunResult, ToolCall
from nexus_agent.observability import TraceStore
from nexus_agent.policy import PolicyEngine
from nexus_agent.providers import Provider, ProviderError, build_provider
from nexus_agent.tools.registry import BUILTIN_HANDLERS, BUILTIN_TOOLS


class EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class AgentRuntime:
    """Owns session isolation, the model/tool loop, policy, and tracing."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider: Provider | None = None,
        store: TraceStore | None = None,
        tools: list[dict[str, Any]] | None = None,
        handlers: dict[str, Any] | None = None,
    ):
        self.settings = settings or Settings.from_env()
        self.provider = provider
        self._owns_store = store is None
        self.store = store or TraceStore(self.settings.state_dir / "nexus.db")
        self.tools = list(tools or BUILTIN_TOOLS)
        self.handlers = dict(handlers or BUILTIN_HANDLERS)
        self.policy = PolicyEngine()
        self.executor = ToolExecutor(self.handlers)
        self.mcp = MCPManager()
        self._initialized = False
        self._session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _provider(self) -> Provider:
        if self.provider is None:
            self.provider = build_provider(self.settings)
        return self.provider

    def create_session(self, session_id: str | None = None) -> str:
        value = session_id or f"session_{uuid.uuid4().hex[:16]}"
        self.store.create_session(value)
        return value

    async def initialize(self, config_path: Path | None = None) -> None:
        if self._initialized:
            return
        configs = self.mcp.load_configs(config_path or (self.settings.workdir / "mcp.json"))
        await self.mcp.connect_all(configs)
        self.tools.extend(self.mcp.tools)
        self.handlers.update(self.mcp.handlers)
        self.executor = ToolExecutor(self.handlers, self.mcp.readonly_tools)
        self._initialized = True

    async def close(self) -> None:
        await self.mcp.close()
        if self._owns_store:
            self.store.close()

    async def _emit(
        self, run_id: str, event_type: str, payload: dict[str, Any], sink: EventSink | None
    ) -> dict[str, Any]:
        event = self.store.append_event(run_id, event_type, payload)
        if sink:
            await sink.emit(event)
        return event

    def _system_prompt(self) -> str:
        names = ", ".join(tool["name"] for tool in self.tools)
        return (
            "You are Nexus Agent, a coding agent. The model decides; the harness executes. "
            "Use tools when they improve correctness, respect permission results, and finish with "
            "a concise account of the outcome. Never claim an action succeeded without a tool "
            "result.\n\n"
            f"Workspace: {self.settings.workdir}\nAvailable tools: {names}"
        )

    def _compact(
        self, messages: list[dict[str, Any]], *, force: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        encoded = json.dumps(messages, ensure_ascii=False, default=str)
        if not force and len(encoded) <= self.settings.context_limit:
            return messages, 0
        # Preserve the latest user turn, but compact at least one older
        # message whenever serialized history exceeds the budget.
        start = max(1, len(messages) - 16) if len(messages) > 1 else 0
        while start < len(messages):
            content = messages[start].get("content")
            is_result = (
                messages[start].get("role") == "user"
                and isinstance(content, list)
                and any(
                    item.get("type") == "tool_result" for item in content if isinstance(item, dict)
                )
            )
            if not is_result:
                break
            start += 1
        tail = messages[start:]
        marker = {
            "role": "user",
            "content": f"[Earlier context compacted: {start} messages omitted.]",
        }
        return [marker, *tail], start

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
                    {"attempt": attempt + 1, "retryable": exc.retryable, "error": str(exc)},
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
        run_id = request.run_id or f"run_{uuid.uuid4().hex[:16]}"
        await self.initialize()
        async with self._session_locks[session_id]:
            return await self._run_locked(request, session_id, run_id, sink, approval_handler)

    async def _run_locked(
        self,
        request: RunRequest,
        session_id: str,
        run_id: str,
        sink: EventSink | None,
        approval_handler: ApprovalHandler | None,
    ) -> RunResult:
        started = time.perf_counter()
        self.store.create_run(run_id, session_id)
        await self._emit(
            run_id,
            "run.started",
            {
                "session_id": session_id,
                "provider": self.settings.provider,
                "model": self.settings.model,
            },
            sink,
        )
        messages = self.store.load_messages(session_id)
        user_message = {"role": "user", "content": request.prompt}
        messages.append(user_message)
        self.store.append_message(session_id, user_message)
        usage = {"input_tokens": 0, "output_tokens": 0}
        tool_count = 0
        output = ""
        steps = 0

        async def approve(approval_id: str, call: ToolCall, reason: str) -> bool:
            await self._emit(
                run_id,
                "approval.required",
                {
                    "approval_id": approval_id,
                    "tool": call.name,
                    "arguments": call.arguments,
                    "reason": reason,
                },
                sink,
            )
            approved = False
            if approval_handler:
                try:
                    approved = await asyncio.wait_for(
                        approval_handler(approval_id, call, reason),
                        timeout=self.settings.approval_timeout,
                    )
                except asyncio.TimeoutError:
                    approved = False
            await self._emit(
                run_id,
                "approval.resolved",
                {"approval_id": approval_id, "approved": approved},
                sink,
            )
            return approved

        try:
            for steps in range(1, self.settings.max_steps + 1):
                prepared, removed = self._compact(messages)
                if removed:
                    await self._emit(
                        run_id,
                        "context.compacted",
                        {
                            "removed_messages": removed,
                            "remaining_messages": len(prepared),
                        },
                        sink,
                    )
                model_request = ModelRequest(
                    system=self._system_prompt(),
                    messages=prepared,
                    tools=self.tools,
                    model=self.settings.model,
                    max_tokens=self.settings.max_tokens,
                )
                model_started = time.perf_counter()
                await self._emit(
                    run_id,
                    "model.request",
                    {"step": steps, "message_count": len(prepared), "tool_count": len(self.tools)},
                    sink,
                )
                response = await self._complete_with_retry(model_request, run_id, sink)
                elapsed = (time.perf_counter() - model_started) * 1000
                for key in usage:
                    usage[key] += int(response.usage.get(key, 0))
                assistant_message = {"role": "assistant", "content": response.content_blocks()}
                messages.append(assistant_message)
                self.store.append_message(session_id, assistant_message)
                await self._emit(
                    run_id,
                    "model.response",
                    {
                        "step": steps,
                        "duration_ms": round(elapsed, 2),
                        "stop_reason": response.stop_reason,
                        "text": response.text,
                        "tool_calls": len(response.tool_calls),
                        "usage": response.usage,
                    },
                    sink,
                )
                if not response.tool_calls:
                    output = response.text
                    break
                results: list[dict[str, Any]] = []
                for call in response.tool_calls:
                    tool_count += 1
                    await self._emit(
                        run_id,
                        "tool.request",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                        sink,
                    )
                    tool_started = time.perf_counter()
                    if call.name == "compact":
                        messages, removed_now = self._compact(messages, force=True)
                        await self._emit(
                            run_id,
                            "context.compacted",
                            {
                                "removed_messages": removed_now,
                                "remaining_messages": len(messages),
                                "requested_by_tool": True,
                            },
                            sink,
                        )
                        result_content = f"Compacted {removed_now} earlier messages."
                        from nexus_agent.models import ToolResult

                        result = ToolResult(call.id, call.name, result_content)
                    else:
                        result = await self.executor.execute(
                            call,
                            ToolContext(self.settings.workdir, run_id, self.policy, approve),
                        )
                    await self._emit(
                        run_id,
                        "tool.result",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "content": result.content,
                            "is_error": result.is_error,
                            "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2),
                        },
                        sink,
                    )
                    results.append(result.as_block())
                result_message = {"role": "user", "content": results}
                messages.append(result_message)
                self.store.append_message(session_id, result_message)
            else:
                raise RuntimeError(f"Maximum agent steps exceeded ({self.settings.max_steps})")
            duration = (time.perf_counter() - started) * 1000
            self.store.finish_run(
                run_id,
                status="completed",
                output=output,
                steps=steps,
                tool_calls=tool_count,
                duration_ms=duration,
                usage=usage,
            )
            await self._emit(
                run_id,
                "run.completed",
                {
                    "output": output,
                    "steps": steps,
                    "tool_calls": tool_count,
                    "duration_ms": round(duration, 2),
                    "usage": usage,
                },
                sink,
            )
            return RunResult(
                run_id, session_id, "completed", output, steps, tool_count, duration, usage
            )
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            error = f"{type(exc).__name__}: {exc}"
            self.store.finish_run(
                run_id,
                status="failed",
                output=output,
                steps=steps,
                tool_calls=tool_count,
                duration_ms=duration,
                usage=usage,
                error=error,
            )
            await self._emit(
                run_id, "run.failed", {"error": error, "duration_ms": round(duration, 2)}, sink
            )
            return RunResult(
                run_id, session_id, "failed", error, steps, tool_count, duration, usage
            )
