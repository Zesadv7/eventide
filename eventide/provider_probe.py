"""Audited connection checks using real control Runs and the shared model budget."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from eventide.budgets import BudgetExceeded
from eventide.config import Settings
from eventide.models import ModelRequest, ModelResponse
from eventide.normalization import normalize, redact
from eventide.providers import Provider, ProviderError
from eventide.workspace import digest

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


class ProbeError(ProviderError):
    def __init__(self, category: str, message: str):
        super().__init__(message, retryable=False)
        self.category = category


_ERROR_BY_CLASS = {
    "AuthenticationError": ("authentication", "鉴权失败，请检查 API Key"),
    "PermissionDeniedError": ("permission_denied", "账号无权限，请检查 API Key 的访问范围"),
    "NotFoundError": ("model_not_found", "模型不存在或当前账号无权访问"),
    "RateLimitError": ("rate_limit", "请求受到限流，请稍后重试"),
    "BadRequestError": ("bad_request", "请求被模型服务拒绝，请检查模型名称与参数"),
}


def probe_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ProbeError):
        return exc.category, str(exc)
    if isinstance(exc, BudgetExceeded):
        return "budget_exceeded", "连接检查超过已配置的 Token 预算"
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



def probe_owner(host: RuntimeHost) -> str:
    for event in host.store.facts_by_type(("control.session",)):
        if event["payload"].get("kind") == "connection_probe":
            return str(event["session_id"])
    owner = host.create_session(f"probe_control_{uuid.uuid4().hex}")
    host.store.append_fact(owner, "control.session", {"kind": "connection_probe"})
    host.store.update_session(owner, title="连接检查", archived=True)
    return owner


async def run_probe(
    host: RuntimeHost, settings: Settings, factory: Callable[[Settings], Provider],
    *, run_id: str | None = None,
) -> ModelResponse:
    """A control Run owns one request, including client cleanup; never runs Agent tools."""
    owner = probe_owner(host)
    task = asyncio.current_task()
    assert task is not None
    host._active_tasks.add(task)
    try:
        # Control Sessions are serial too, without taking a source checkout lock.
        async with host._session_locks[owner]:
            return await _run_owned_probe(host, settings, factory, owner, run_id, task)
    finally:
        host._active_tasks.discard(task)


async def _run_owned_probe(
    host: RuntimeHost, settings: Settings, factory: Callable[[Settings], Provider],
    owner: str, run_id: str | None, task: asyncio.Task[Any],
) -> ModelResponse:
    identity = run_id or f"probe_{uuid.uuid4().hex}"
    secrets = (settings.api_key or "", host.settings.api_key or "")
    def clean(value: Any) -> Any:
        return normalize(redact(value), secrets)
    host.store.create_run(identity, owner)
    host._run_tasks[identity] = task
    host.store.append_event(identity, "run.started", clean({
        "source": "connection_probe", "billing_scope": "service", "project_id": None,
        "provider": settings.provider, "model": settings.model,
        "endpoint_hash": digest(settings.base_url),
    }))
    probe: Provider | None = None
    response: ModelResponse | None = None
    error: BaseException | None = None
    try:
        probe = factory(settings)
        request = ModelRequest(
            "你是连接检查助手。", [{"role": "user", "content": "请只回复：连接成功"}],
            [], settings.model, 32,
        )
        async with host.dispatch.scheduler.model_slot():
            attempt = clean({
                "request_id": f"request_{uuid.uuid4().hex}",
                "attempt_id": f"attempt_{uuid.uuid4().hex}", "attempt": 1,
                "kind": "connection_probe", "provider": settings.provider,
                "model": settings.model, "generation": {}, "max_output_tokens": 32,
            })
            started = time.perf_counter()
            try:
                receipt = host.model_budget.begin(identity, request, attempt)
            except BudgetExceeded as exc:
                host.store.append_event(identity, "budget.blocked", {
                    "request_id": attempt["request_id"], "violations": exc.violations,
                })
                raise
            if receipt.warnings:
                host.store.append_event(identity, "budget.warning", {
                    "attempt_id": attempt["attempt_id"], "budgets": receipt.warnings,
                })
            try:
                response = await asyncio.wait_for(
                    probe.complete(request), timeout=settings.model_timeout,
                )
            except BaseException as exc:
                host.model_budget.finish(identity, {
                    **attempt, "status": "cancelled" if isinstance(exc, asyncio.CancelledError)
                    else "failed", "usage_source": "unknown",
                    "duration_ms": (time.perf_counter() - started) * 1000,
                })
                raise
            else:
                settled = host.model_budget.finish(identity, clean({
                    **attempt, "status": "completed", "usage": response.usage,
                    "usage_source": "reported" if response.usage else "unknown",
                    "stop_reason": response.stop_reason,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }))
                if settled.warnings:
                    host.store.append_event(identity, "budget.warning", {
                        "attempt_id": attempt["attempt_id"], "budgets": settled.warnings,
                    })
    except BaseException as exc:
        error = exc
    finally:
        try:
            closer = getattr(probe, "close", None) or getattr(probe, "aclose", None)
            if closer:
                result = closer()
                if inspect.isawaitable(result):
                    cleanup = asyncio.ensure_future(result)
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError as exc:
                            error = error or exc
                    await cleanup
        except BaseException as exc:
            error = error or exc
        category, message = probe_error(error) if isinstance(error, Exception) else (
            "cancelled", "连接检查已取消，未确认用量保留预留"
        )
        category, message = clean(category), clean(message)
        terminal = "interrupted" if isinstance(error, (asyncio.CancelledError, BudgetExceeded)) \
            else "failed" if error else "completed"
        host.store.append_event(identity, f"run.{terminal}", {
            "source": "connection_probe", "kind": "connection_probe",
            **({"error": message, "reason": category} if error else {}),
        })
        host._run_tasks.pop(identity, None)
    if isinstance(error, (asyncio.CancelledError, BudgetExceeded)):
        raise error
    if isinstance(error, Exception):
        category, message = probe_error(error)
        # No raw SDK cause/context is exposed to HTTP or Python traceback output.
        raise ProbeError(clean(category), clean(message)) from None
    if error is not None:
        raise error
    assert response is not None
    return replace(
        response, text=clean(response.text), stop_reason=clean(response.stop_reason),
        tool_calls=tuple(replace(call, id=clean(call.id), name=clean(call.name),
                                 arguments=clean(call.arguments)) for call in response.tool_calls),
        provider_items=tuple(clean(response.provider_items)),
    )
