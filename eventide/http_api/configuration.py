"""Durable configuration receipts; only secret fingerprints are persisted."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request

from eventide.host import ProviderCleanupError, RuntimeHost
from eventide.http_control import begin_mutation, fail_mutation, mutation_key
from eventide.secrets import SecretKeyError


async def mutate_configuration(
    host: RuntimeHost, request: Request, operation: str, payload: dict[str, Any],
    execute: Callable[[str], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    mutation_key(request)
    receipt = begin_mutation(host, request, host.configuration_owner(), operation, payload)
    if receipt.result is not None:
        return receipt.result
    try:
        # Host/Store commit both the configuration and completion in one transaction.
        return await execute(receipt.identity)
    except ProviderCleanupError:
        message = "旧模型客户端清理失败，配置未写入；请刷新后显式重试清理"
        fail_mutation(host, receipt, message, 503, rejected=True)
        raise HTTPException(
            503, message, headers={"X-Eventide-Mutation-Outcome": "rejected"},
        ) from None
    except (ValueError, SecretKeyError) as exc:
        if any(e["type"] == "http.mutation_completed"
               and e["payload"].get("identity") == receipt.identity
               for e in host.store.session_events(receipt.owner)):
            raise HTTPException(503, "配置操作结果未确认，请保留原请求身份核对") from None
        code = 409 if any(word in str(exc) for word in (
            "Configuration changed", "configuration", "active runs", "closed",
        )) else 422
        # Validation errors are fixed descriptions, never caller/SDK exception bodies.
        message = ("配置已变化或服务忙，请刷新配置后重试" if code == 409
                   else "配置参数无效或密钥无法保存，请检查模型能力与密钥状态")
        fail_mutation(host, receipt, message, code, rejected=True)
        raise HTTPException(
            code, message, headers={"X-Eventide-Mutation-Outcome": "rejected"},
        ) from None
    except Exception:
        # A storage failure may have happened after SQLite committed. Never overwrite
        # a successful receipt or mark an unknown result as definitely rejected.
        raise HTTPException(503, "配置操作结果未确认，请保留原请求身份核对") from None
