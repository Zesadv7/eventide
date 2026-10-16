"""Sessions HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import SessionBody, SessionDeleteBody, SessionUpdateBody
from eventide.http_api.session_management import create_session as create_session_mutation
from eventide.http_api.session_management import mutate_session
from eventide.usage import build_usage_ledger


class ImportedMessage(BaseModel):
    seq: int
    role: str
    content: Any


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["sessions"])
    agent_runtime = context.runtime
    get_workspace = context.get_workspace

    @router.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        request: Request,
        body: SessionBody | None = None,
    ) -> dict[str, Any]:
        require_local_request(request)
        return create_session_mutation(agent_runtime, request, (body or SessionBody()).model_dump())

    @router.get("/api/workspaces/{workspace_id}/sessions")
    async def list_sessions(
        workspace_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await get_workspace(workspace_id)
        return [
            agent_runtime.session_status(s["id"])
            for s in agent_runtime.store.list_sessions(
                workspace_id,
                include_archived=include_archived,
            )
        ]

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            return agent_runtime.session_status(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/api/sessions/{session_id}")
    async def update_session(
        session_id: str, body: SessionUpdateBody, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        return mutate_session(agent_runtime, request, session_id, body.model_dump())

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(
        session_id: str,
        body: SessionDeleteBody,
        request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        return mutate_session(agent_runtime, request, session_id, body.model_dump(), delete=True)

    @router.get("/api/sessions/{session_id}/messages")
    async def get_messages(session_id: str) -> list[dict[str, Any]]:
        await get_session(session_id)
        try:
            return agent_runtime.store.load_messages(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/sessions/{session_id}/legacy-messages", response_model=list[ImportedMessage])
    async def imported_messages(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        before: int | None = Query(default=None, ge=1),
    ) -> list[dict[str, Any]]:
        try:
            return agent_runtime.store.imported_messages(session_id, limit=limit, before=before)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/sessions/{session_id}/usage")
    async def session_usage(session_id: str, include_children: bool = True) -> dict[str, Any]:
        await get_session(session_id)
        return build_usage_ledger(
            agent_runtime.store.usage_events(
                session_id=session_id,
                include_children=include_children,
            )
        )

    return router
