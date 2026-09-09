"""FastAPI surface and approval broker for Eventide."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from eventide.config import Settings, normalize_provider
from eventide.models import RunRequest, ToolCall
from eventide.normalization import normalize, redact
from eventide.runtime import AgentRuntime
from eventide.secrets import SecretKeyError

WEB_DIR = Path(__file__).with_name("web")


class RunBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class SessionBody(BaseModel):
    workspace_id: str | None = None
    working_directory: str | None = Field(default=None, min_length=1)


class SessionUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=96)
    archived: bool | None = None


class WorkspaceBody(BaseModel):
    path: str = Field(min_length=1)


class ApprovalBody(BaseModel):
    approved: bool


class ProviderConfigBody(BaseModel):
    provider: Literal["anthropic", "openai_compatible", "openai-compatible", "openai_responses"]
    api_key: SecretStr | None = None
    base_url: str | None = Field(default=None, max_length=2_048)
    model: str = Field(min_length=1, max_length=256)
    clear_api_key: bool = False


def _require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host == "testclient":
        return
    try:
        is_local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_local = host.lower() == "localhost"
    if not is_local:
        raise HTTPException(status_code=403, detail="模型凭据只能从运行服务的本机管理")


_ERROR_BY_CLASS = {
    "AuthenticationError": ("authentication", "鉴权失败，请检查 API Key"),
    "PermissionDeniedError": ("permission_denied", "账号无权限，请检查 API Key 的访问范围"),
    "NotFoundError": ("model_not_found", "模型不存在或当前账号无权访问"),
    "RateLimitError": ("rate_limit", "请求受到限流，请稍后重试"),
    "BadRequestError": ("bad_request", "请求被模型服务拒绝，请检查模型名称与参数"),
}


def _probe_error(exc: Exception) -> tuple[str, str]:
    # 优先按异常类型分类：adapter 用 `raise ProviderError(...) from exc` 把原始
    # SDK 异常挂在 __cause__ 链上；openai/anthropic 共用稳定类名，故按类名匹配。
    node: BaseException | None = exc
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        name = type(node).__name__
        if name in _ERROR_BY_CLASS:
            return _ERROR_BY_CLASS[name]
        if name in ("APIConnectionError", "ConnectionError"):
            return "network", "无法连接到模型服务，请检查 Base URL 和网络"
        if name in ("APITimeoutError", "TimeoutError"):
            return "timeout", "连接超时，请检查接口地址或网络"
        node = node.__cause__ or node.__context__
    # 回退到字符串匹配，覆盖非 SDK 或文本透传的错误。
    text = str(exc).lower()
    if "no model api key" in text or "api key configured" in text:
        return "missing_api_key", "尚未配置 API Key"
    if "无法解密" in text or "eventide_secret_key" in text:
        return "decrypt_error", "已保存的 API Key 无法解密，请重新输入或恢复主密钥"
    if any(token in text for token in ("401", "unauthorized", "authentication", "invalid api key")):
        return "authentication", "鉴权失败，请检查 API Key"
    if any(token in text for token in ("model_not_found", "model not found", "does not exist")):
        return "model_not_found", "模型不存在或当前账号无权访问"
    if "529" in text or "overloaded" in text:
        return "overloaded", "模型服务过载，请稍后重试"
    if "429" in text or "rate limit" in text:
        return "rate_limit", "请求受到限流，请稍后重试"
    if "timeout" in text or "timed out" in text:
        return "timeout", "连接超时，请检查接口地址或网络"
    if any(
        token in text
        for token in (
            "connection",
            "connect error",
            "dns",
            "name resolution",
            "ssl",
            "certificate",
            "tls",
        )
    ):
        return "network", "无法连接到模型服务，请检查 Base URL 和网络"
    if "unsupported provider" in text:
        return "provider_error", "不支持的 Provider，请检查选择"
    return "provider_error", "模型服务返回错误，请检查 Provider、模型名称和接口地址"


class ApprovalBroker:
    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def request(self, approval_id: str, _call: ToolCall, _reason: str) -> bool:
        future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = future
        try:
            return await future
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool) -> bool:
        future = self.pending.get(approval_id)
        if not future or future.done():
            return False
        future.set_result(approved)
        return True


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    agent_runtime = runtime or AgentRuntime(Settings.from_env())
    broker = ApprovalBroker()
    tasks: set[asyncio.Task[Any]] = set()
    reserved: set[str] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await agent_runtime.initialize()
        yield
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await agent_runtime.close()

    app = FastAPI(
        title="Eventide Runtime",
        version="0.3.0",
        description="Traceable, policy-aware execution for tool-using agents.",
        lifespan=lifespan,
    )
    app.state.runtime = agent_runtime
    app.state.approvals = broker
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def console() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime": "eventide"}

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(body: SessionBody | None = None) -> dict[str, str]:
        try:
            return {
                "session_id": agent_runtime.create_session(
                    workspace_id=body.workspace_id if body else None,
                    working_directory=body.working_directory if body else None,
                )
            }
        except (ValueError, OSError) as exc:
            code = 404 if str(exc).startswith("Workspace not found") else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.post("/api/workspaces", status_code=201)
    async def register_workspace(body: WorkspaceBody, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        try:
            workspace = agent_runtime.resolve_or_register_workspace(body.path)
            return agent_runtime.workspace_status(workspace.workspace_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/workspaces")
    async def list_workspaces() -> list[dict[str, Any]]:
        return [
            agent_runtime.workspace_status(w.workspace_id)
            for w in agent_runtime.store.list_workspaces()
        ]

    @app.get("/api/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> dict[str, Any]:
        try:
            return agent_runtime.workspace_status(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/workspaces/{workspace_id}")
    async def remove_workspace(workspace_id: str, request: Request) -> dict[str, str]:
        _require_local_request(request)
        await get_workspace(workspace_id)
        try:
            agent_runtime.store.remove_workspace(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"removed": workspace_id}

    @app.get("/api/workspaces/{workspace_id}/sessions")
    async def list_sessions(
        workspace_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await get_workspace(workspace_id)
        return [
            agent_runtime.session_status(s["id"])
            for s in agent_runtime.store.list_sessions(
                workspace_id,
                include_archived=include_archived,
            )
        ]

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            return agent_runtime.session_status(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/sessions/{session_id}")
    async def update_session(
        session_id: str, body: SessionUpdateBody, request: Request
    ) -> dict[str, Any]:
        _require_local_request(request)
        await get_session(session_id)
        try:
            agent_runtime.store.update_session(
                session_id,
                title=body.title,
                archived=body.archived,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return agent_runtime.session_status(session_id)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request) -> dict[str, str]:
        _require_local_request(request)
        await get_session(session_id)
        try:
            agent_runtime.store.delete_session(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": session_id}

    @app.get("/api/sessions/{session_id}/messages")
    async def get_messages(session_id: str) -> list[dict[str, Any]]:
        await get_session(session_id)
        try:
            return agent_runtime.store.load_messages(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/runs")
    async def list_runs(
        session_id: str,
        limit: int = 100,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        await get_session(session_id)
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        return agent_runtime.session_runs(session_id, limit=limit, before=before)

    @app.post(
        "/api/sessions/{session_id}/continue",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def continue_session(session_id: str) -> dict[str, str]:
        # Continue validation is part of admission; a rejected request returns 409,
        # without creating a run that could hide the parked source run.
        record = await get_session(session_id)
        workspace_id = record["workspace_id"]
        if workspace_id in reserved:
            raise HTTPException(status_code=409, detail="Workspace is busy")
        reserved.add(workspace_id)
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        try:
            await agent_runtime.admit_continue(session_id, run_id)
        except ValueError as exc:
            reserved.discard(workspace_id)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BaseException:
            reserved.discard(workspace_id)
            raise

        async def execute() -> None:
            try:
                await agent_runtime.continue_session(
                    session_id,
                    approval_handler=broker.request,
                    run_id=run_id,
                )
            finally:
                reserved.discard(workspace_id)

        task = asyncio.create_task(execute())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @app.post("/api/sessions/{session_id}/abandon")
    async def abandon_interruption(session_id: str) -> dict[str, Any]:
        record = await get_session(session_id)
        workspace_id = record["workspace_id"]
        if workspace_id in reserved:
            raise HTTPException(status_code=409, detail="Workspace is busy")
        reserved.add(workspace_id)
        try:
            return asdict(await agent_runtime.abandon_interruption(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            reserved.discard(workspace_id)

    @app.get("/api/config/provider")
    async def get_provider_config() -> dict[str, Any]:
        return agent_runtime.provider_config_status()

    @app.put("/api/config/provider")
    async def set_provider_config(body: ProviderConfigBody, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        if any(not task.done() for task in tasks):
            raise HTTPException(
                status_code=409,
                detail="Wait for active runs to finish before changing provider configuration",
            )
        key = body.api_key.get_secret_value() if body.api_key else None
        try:
            await agent_runtime.configure_provider(
                provider=body.provider,
                api_key=key,
                base_url=body.base_url,
                model=body.model,
                persist=True,
                clear_api_key=body.clear_api_key,
            )
        except (ValueError, SecretKeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return agent_runtime.provider_config_status()

    @app.delete("/api/config/provider")
    async def reset_provider_config(request: Request) -> dict[str, Any]:
        _require_local_request(request)
        if any(not task.done() for task in tasks):
            raise HTTPException(
                status_code=409,
                detail="请等待当前任务结束后再恢复启动配置",
            )
        await agent_runtime.reset_provider_configuration()
        return agent_runtime.provider_config_status()

    @app.post("/api/config/provider/test")
    async def test_provider_config(body: ProviderConfigBody, request: Request) -> dict[str, Any]:
        _require_local_request(request)
        key = body.api_key.get_secret_value() if body.api_key else None
        started = time.perf_counter()
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            await asyncio.wait_for(
                agent_runtime.probe_provider(
                    provider=body.provider,
                    api_key=key,
                    base_url=body.base_url,
                    model=body.model,
                ),
                timeout=30,
            )
        except Exception as exc:
            error_type, message = _probe_error(exc)
            return {
                "success": False,
                "provider": normalize_provider(body.provider),
                "model": body.model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "checked_at": checked_at,
                "message": message,
                "error_type": error_type,
                "detail": normalize(redact(str(exc)), (key or "",)),
            }
        return {
            "success": True,
            "provider": normalize_provider(body.provider),
            "model": body.model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "checked_at": checked_at,
            "message": "连接成功",
            "error_type": None,
        }

    @app.post("/api/sessions/{session_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(session_id: str, body: RunBody) -> dict[str, str]:
        # Keep the v0.2 behavior for callers that supply their own session identity.
        agent_runtime.create_session(session_id)
        record = agent_runtime.session_status(session_id)
        workspace_id = record["workspace_id"]
        if workspace_id in reserved or record["status"] == "parked":
            raise HTTPException(status_code=409, detail="Workspace busy or session parked")
        reserved.add(workspace_id)
        run_id = f"run_{uuid.uuid4().hex[:16]}"

        async def execute() -> None:
            try:
                await agent_runtime.run(
                    RunRequest(body.prompt, session_id=session_id, run_id=run_id),
                    approval_handler=broker.request,
                )
            finally:
                reserved.discard(workspace_id)

        task = asyncio.create_task(execute())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        if not agent_runtime.store.get_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            return await agent_runtime.cancel_run(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        if not agent_runtime.store.get_run_identity(run_id):
            raise HTTPException(status_code=404, detail="Run not found")

        async def stream() -> AsyncIterator[str]:
            cursor = max(after, 0)
            idle_ticks = 0
            while not await request.is_disconnected():
                events = agent_runtime.store.get_events(run_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    encoded = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {encoded}\n\n"
                if (
                    agent_runtime.store.run_is_terminal(run_id)
                    and not events
                ):
                    break
                if not events:
                    idle_ticks += 1
                    if idle_ticks % 20 == 0:
                        yield ": keepalive\n\n"
                else:
                    idle_ticks = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        if approval_id not in record["pending_approvals"]:
            raise HTTPException(status_code=404, detail="Approval does not belong to this run")
        if not broker.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="Approval is no longer pending")
        return {"approval_id": approval_id, "approved": body.approved}

    return app
