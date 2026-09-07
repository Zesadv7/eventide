"""FastAPI surface and approval broker for Nexus Agent."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from nexus_agent.config import Settings
from nexus_agent.models import RunRequest, ToolCall
from nexus_agent.runtime import AgentRuntime
from nexus_agent.secrets import SecretKeyError

WEB_DIR = Path(__file__).with_name("web")


class RunBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class ApprovalBody(BaseModel):
    approved: bool


class ProviderConfigBody(BaseModel):
    provider: Literal[
        "anthropic", "openai_compatible", "openai-compatible", "openai_responses"
    ]
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


def _probe_error(exc: Exception) -> tuple[str, str]:
    text = str(exc).lower()
    if "no model api key" in text or "api key configured" in text:
        return "missing_api_key", "尚未配置 API Key"
    if "无法解密" in text or "nexus_secret_key" in text:
        return "decrypt_error", "已保存的 API Key 无法解密，请重新输入或恢复主密钥"
    if any(token in text for token in ("401", "unauthorized", "authentication", "invalid api key")):
        return "authentication", "鉴权失败，请检查 API Key"
    if any(token in text for token in ("model_not_found", "model not found", "does not exist")):
        return "model_not_found", "模型不存在或当前账号无权访问"
    if "429" in text or "rate limit" in text:
        return "rate_limit", "请求受到限流，请稍后重试"
    if isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text:
        return "timeout", "连接超时，请检查接口地址或网络"
    if any(token in text for token in ("connection", "connect error", "dns", "name resolution")):
        return "network", "无法连接到模型服务，请检查 Base URL 和网络"
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await agent_runtime.initialize()
        yield
        for task in list(tasks):
            task.cancel()
        await agent_runtime.close()

    app = FastAPI(
        title="Nexus Agent Runtime",
        version="0.2.0",
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
        return {"status": "ok", "runtime": "nexus-agent"}

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session() -> dict[str, str]:
        return {"session_id": agent_runtime.create_session()}

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
    async def test_provider_config(
        body: ProviderConfigBody, request: Request
    ) -> dict[str, Any]:
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
                "provider": body.provider.replace("-", "_"),
                "model": body.model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "checked_at": checked_at,
                "message": message,
                "error_type": error_type,
            }
        return {
            "success": True,
            "provider": body.provider.replace("-", "_"),
            "model": body.model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "checked_at": checked_at,
            "message": "连接成功",
            "error_type": None,
        }

    @app.post("/api/sessions/{session_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(session_id: str, body: RunBody) -> dict[str, str]:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        task = asyncio.create_task(
            agent_runtime.run(
                RunRequest(body.prompt, session_id=session_id, run_id=run_id),
                approval_handler=broker.request,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            cursor = max(after, 0)
            idle_ticks = 0
            while not await request.is_disconnected():
                events = agent_runtime.store.get_events(run_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    encoded = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {encoded}\n\n"
                record = agent_runtime.store.get_run(run_id)
                if record and record["status"] in {"completed", "failed"} and not events:
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
        if not broker.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="Approval is no longer pending")
        return {"approval_id": approval_id, "approved": body.approved}

    return app
