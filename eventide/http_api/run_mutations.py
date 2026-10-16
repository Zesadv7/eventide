"""Durable transport identities for user decisions on existing execution facts."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request

from eventide.host import RuntimeHost
from eventide.http_control import begin_mutation, fail_mutation, finish_mutation


async def mutate_run_action(
    host: RuntimeHost,
    request: Request,
    operation: str,
    payload: dict[str, Any],
    execute: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    receipt = begin_mutation(host, request, host.configuration_owner(), operation, payload)
    if receipt.result is not None:
        return receipt.result
    try:
        result = await execute()
    except HTTPException as exc:
        fail_mutation(host, receipt, str(exc.detail), exc.status_code, rejected=True)
        raise HTTPException(
            exc.status_code, exc.detail, headers={"X-Eventide-Mutation-Outcome": "rejected"}
        ) from exc
    except Exception:
        raise HTTPException(
            503, "Run operation outcome is unknown; retain the original request"
        ) from None
    try:
        return finish_mutation(host, receipt, result)
    except Exception:
        raise HTTPException(
            503, "Run operation outcome is unknown; retain the original request"
        ) from None
