"""Config HTTP routes."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.config import normalize_provider
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import ProviderConfigBody
from eventide.normalization import normalize, redact
from eventide.secrets import SecretKeyError

_ERROR_BY_CLASS = {
    "AuthenticationError": ("authentication", "鉴权失败，请检查 API Key"),
    "PermissionDeniedError": ("permission_denied", "账号无权限，请检查 API Key 的访问范围"),
    "NotFoundError": ("model_not_found", "模型不存在或当前账号无权访问"),
    "RateLimitError": ("rate_limit", "请求受到限流，请稍后重试"),
    "BadRequestError": ("bad_request", "请求被模型服务拒绝，请检查模型名称与参数"),
}


def probe_error(exc: Exception) -> tuple[str, str]:
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
            error_type, message = probe_error(exc)
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

    return router
