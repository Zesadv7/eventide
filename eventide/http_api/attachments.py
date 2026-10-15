"""Attachments HTTP routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from eventide import attachments
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import AttachmentBody


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["attachments"])
    agent_runtime = context.runtime
    get_session = context.get_session

    @router.post(
        "/api/sessions/{session_id}/attachments",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_attachment(session_id: str, body: AttachmentBody) -> dict[str, Any]:
        # Uploads never auto-create a session: an unknown session is a 404.
        await get_session(session_id)
        if agent_runtime.is_worker_session(session_id):
            raise HTTPException(status_code=409, detail="Worker sessions do not accept attachments")
        try:
            return attachments.save_attachment(
                agent_runtime.settings.state_dir,
                session_id,
                attachments.new_attachment_id(),
                body.name,
                body.content_base64,
                body.media_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/sessions/{session_id}/attachments")
    async def list_session_attachments(
        session_id: str, include_used: bool = False
    ) -> list[dict[str, Any]]:
        await get_session(session_id)
        try:
            records = attachments.list_attachments(
                agent_runtime.settings.state_dir, session_id, include_used=True
            )
            referenced = agent_runtime.store.referenced_attachments(session_id)
            for record in records:
                record["used"] = record["used"] or record["attachment_id"] in referenced
            return records if include_used else [record for record in records if not record["used"]]

        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/sessions/{session_id}/attachments/{attachment_id}")
    async def download_attachment(session_id: str, attachment_id: str) -> Response:
        await get_session(session_id)
        try:
            record = attachments.load_attachment(
                agent_runtime.settings.state_dir, session_id, attachment_id
            )
            content = attachments.attachment_bytes(record)
        except ValueError as exc:
            code = 404 if str(exc).startswith("附件不存在") else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        filename = quote(str(record["name"]), safe="")
        return Response(
            content=content,
            media_type=str(record["media_type"]),
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    @router.delete("/api/sessions/{session_id}/attachments/{attachment_id}")
    async def delete_session_attachment(
        session_id: str, attachment_id: str, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        await get_session(session_id)
        try:
            if attachment_id in agent_runtime.store.referenced_attachments(session_id):
                raise ValueError("附件已用于工作记录，不能删除")
            deleted = attachments.delete_attachment(
                agent_runtime.settings.state_dir, session_id, attachment_id
            )
        except ValueError as exc:
            message = str(exc)
            if message.startswith("附件不存在"):
                code = 404
            else:
                code = 409 if "不能删除" in message else 422
            raise HTTPException(status_code=code, detail=message) from exc
        return {"deleted": deleted["attachment_id"]}

    return router
