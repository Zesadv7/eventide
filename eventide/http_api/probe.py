"""At-most-once HTTP connection checks backed by control Run evidence."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request

from eventide.config import normalize_provider
from eventide.host import RuntimeHost
from eventide.http_api.schemas import ProviderConfigBody
from eventide.http_control import MutationReceipt, begin_mutation, finish_mutation, mutation_key
from eventide.normalization import normalize, redact
from eventide.provider_probe import probe_error, probe_owner
from eventide.usage import build_usage_ledger
from eventide.workspace import digest

PROBE_TIMEOUT = 30
RUN_ID_HEADER = "X-Eventide-Run-ID"


async def connection_probe(
    host: RuntimeHost, body: ProviderConfigBody, request: Request, active: set[str],
) -> dict[str, Any]:
    request_key = mutation_key(request)  # Reject before creating a control owner.
    owner = probe_owner(host)
    candidate_key = body.api_key.get_secret_value().strip() if body.api_key else None
    candidate_key = candidate_key or None
    key = candidate_key or host.settings.api_key
    payload: dict[str, Any] = {
        "provider": normalize_provider(body.provider), "model": body.model.strip(),
        "base_url": body.base_url.strip() if body.base_url else None,
        "api_key": candidate_key, "clear_api_key": body.clear_api_key,
    }
    identity = "mutation_" + digest([owner, "provider.probe", request_key])
    run_id = "probe_" + identity.removeprefix("mutation_")
    secrets = (key or "", host.settings.api_key or "")

    def clean(value: Any) -> Any:
        return normalize(redact(value), secrets)

    def evidence() -> dict[str, Any]:
        exists = host.store.get_run_identity(run_id) is not None
        return {
            "run_id": run_id if exists else None,
            "usage": build_usage_ledger(host.store.usage_events(run_id=run_id) if exists else []),
        }

    base = {"provider": payload["provider"], "model": clean(payload["model"])}
    try:
        receipt = begin_mutation(host, request, owner, "provider.probe", payload)
    except HTTPException as exc:
        started = next((event for event in host.store.session_events(owner)
                        if event["type"] == "http.mutation_started"
                        and event["payload"].get("identity") == identity), None)
        if (exc.status_code != 409 or started is None
                or started["payload"].get("fingerprint") != digest(payload)):
            raise
        if identity in active:
            raise HTTPException(409, "Connection check is still running; retry the same request",
                                headers={RUN_ID_HEADER: run_id}) from None
        events = host.store.run_events(run_id)
        terminal = next((event for event in reversed(events)
                         if event["type"] in {"run.completed", "run.failed", "run.interrupted"}),
                        None)
        # A matching receipt without a Run cannot have dispatched a request. A
        # nonterminal Run remains unknown; only canonical terminal facts settle it.
        if events and terminal is None:
            raise HTTPException(409, "Connection check outcome is unknown; inspect its Run",
                                headers={RUN_ID_HEADER: run_id}) from None
        success = terminal is not None and terminal["type"] == "run.completed"
        final = terminal["payload"] if terminal else {}
        result = clean({
            **base, **evidence(), "success": success, "latency_ms": None,
            "checked_at": None, "recovered": True,
            "message": "已恢复连接成功记录" if success else final.get("error") or (
                "已恢复停止记录；未确认用量仍保留预留" if terminal
                else "上次连接检查未派发，已恢复接收记录"
            ),
            "error_type": None if success else final.get("reason") or (
                "interrupted" if terminal else "not_dispatched"
            ),
        })
        return finish_mutation(host, MutationReceipt(identity, owner), result)
    if receipt.result is not None:
        return receipt.result
    active.add(identity)
    started_at = time.perf_counter()
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        try:
            await asyncio.wait_for(host.probe_provider(
                provider=payload["provider"], api_key=key, base_url=payload["base_url"],
                model=payload["model"], run_id=run_id,
            ), timeout=PROBE_TIMEOUT)
        except Exception as exc:
            category, message = probe_error(exc)
            result = {"success": False, "message": message, "error_type": category,
                      "detail": str(exc)}
        else:
            result = {"success": True, "message": "连接成功", "error_type": None}
        return finish_mutation(host, receipt, clean({
            **base, **evidence(), **result,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "checked_at": checked_at, "recovered": False,
        }))
    finally:
        active.discard(identity)
