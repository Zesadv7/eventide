"""Config HTTP routes."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.config import normalize_provider
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import ProviderConfigBody
from eventide.normalization import normalize, redact
from eventide.provider_probe import probe_error
from eventide.secrets import SecretKeyError
from eventide.usage import build_usage_ledger


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["config"])
    agent_runtime = context.runtime

    @router.get("/api/runtime/settings")
    async def get_runtime_settings() -> dict[str, Any]:
        settings = agent_runtime.settings
        return {
            "context_limit": settings.context_limit,
            "approval_timeout": settings.approval_timeout,
            "max_steps": settings.max_steps,
            "task_max_steps": settings.task_max_steps,
            "run_token_limit": settings.run_token_limit,
        }

    @router.get("/api/config/provider")
    async def get_provider_config() -> dict[str, Any]:
        return agent_runtime.provider_config_status()

    @router.put("/api/config/provider")
    async def set_provider_config(body: ProviderConfigBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        if agent_runtime.runtime_busy():
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

    @router.delete("/api/config/provider")
    async def reset_provider_config(request: Request) -> dict[str, Any]:
        require_local_request(request)
        if agent_runtime.runtime_busy():
            raise HTTPException(
                status_code=409,
                detail="请等待当前任务结束后再恢复启动配置",
            )
        await agent_runtime.reset_provider_configuration()
        return agent_runtime.provider_config_status()

    @router.post("/api/config/provider/test")
    async def test_provider_config(body: ProviderConfigBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = body.api_key.get_secret_value() if body.api_key else None
        started = time.perf_counter()
        checked_at = datetime.now(timezone.utc).isoformat()
        safe_model = normalize(body.model, (key or "", agent_runtime.settings.api_key or ""))
        run_id = f"probe_{uuid.uuid4().hex}"

        def accounting() -> dict[str, Any]:
            exists = agent_runtime.store.get_run_identity(run_id) is not None
            return {
                "run_id": run_id if exists else None,
                "usage": build_usage_ledger(
                    agent_runtime.store.usage_events(run_id=run_id) if exists else []
                ),
            }
        try:
            await asyncio.wait_for(
                agent_runtime.probe_provider(
                    provider=body.provider,
                    api_key=key,
                    base_url=body.base_url,
                    model=body.model,
                    run_id=run_id,
                ),
                timeout=30,
            )
        except Exception as exc:
            error_type, message = probe_error(exc)
            return {
                **accounting(),
                "success": False,
                "provider": normalize_provider(body.provider),
                "model": safe_model,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "checked_at": checked_at,
                "message": message,
                "error_type": error_type,
                "detail": normalize(redact(str(exc)), (key or "",)),
            }
        return {
            **accounting(),
            "success": True,
            "provider": normalize_provider(body.provider),
            "model": safe_model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "checked_at": checked_at,
            "message": "连接成功",
            "error_type": None,
        }

    return router
