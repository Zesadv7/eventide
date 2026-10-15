"""Config HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.probe import connection_probe
from eventide.http_api.schemas import ProviderConfigBody
from eventide.provider_probe import probe_error as probe_error
from eventide.secrets import SecretKeyError


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["config"])
    agent_runtime = context.runtime
    active_probes: set[str] = set()

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
        return await connection_probe(agent_runtime, body, request, active_probes)

    return router
