"""Local human-only file-operation inspection and explicit idempotent undo."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.file_operations import FileOperationUnknown
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import (
    begin_mutation,
    check_mutation_origin,
    fail_mutation,
    finish_mutation,
    mutation_key,
)
from eventide.secrets import SecretKeyError


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["file-operations"])
    host = context.runtime

    def local(request: Request) -> None:
        require_local_request(request)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site file operation access is not allowed")
        if request.headers.get("origin") is not None:
            check_mutation_origin(request, require_origin=True)

    @router.get("/api/sessions/{session_id}/file-operations")
    async def operations(session_id: str, request: Request) -> dict[str, Any]:
        local(request)
        await context.get_session(session_id)
        return {"operations": host.file_operations.records(session_id)}

    @router.get("/api/sessions/{session_id}/file-operations/{operation_id}")
    async def detail(session_id: str, operation_id: str, request: Request) -> dict[str, Any]:
        local(request)
        try:
            host.authorize_file_operation(session_id, operation_id)
            return await host._admission_check(
                host.file_operations.detail, session_id, operation_id
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except SecretKeyError as exc:
            raise HTTPException(409, "备份无法解密，请恢复原安装密钥；未执行撤销") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/sessions/{session_id}/file-operations/{operation_id}/undo")
    async def undo(session_id: str, operation_id: str, request: Request) -> dict[str, Any]:
        local(request)
        mutation_key(request)
        try:
            host.authorize_file_operation(session_id, operation_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
        receipt = begin_mutation(
            host, request, session_id, "file_undo", {"operation_id": operation_id}
        )
        if receipt.result is not None:
            return receipt.result
        try:
            result = await host.undo_file_operation(session_id, operation_id)
        except PermissionError as exc:
            fail_mutation(host, receipt, str(exc), 403, rejected=True)
            raise HTTPException(
                403, str(exc), headers={"X-Eventide-Mutation-Outcome": "rejected"}
            ) from exc
        except (ValueError, OSError) as exc:
            fail_mutation(host, receipt, str(exc), 409, rejected=True)
            raise HTTPException(
                409, str(exc), headers={"X-Eventide-Mutation-Outcome": "rejected"}
            ) from exc
        except FileOperationUnknown as exc:
            raise HTTPException(503, str(exc)) from exc
        except SecretKeyError as exc:
            message = "备份无法解密，请恢复原安装密钥；未执行撤销"
            fail_mutation(host, receipt, message, 409, rejected=True)
            raise HTTPException(
                409, message, headers={"X-Eventide-Mutation-Outcome": "rejected"}
            ) from exc
        return finish_mutation(host, receipt, result)

    return router
