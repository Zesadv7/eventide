"""Workspaces HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import WorkspaceBody


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["workspaces"])
    agent_runtime = context.runtime

    @router.post("/api/workspaces", status_code=201)
    async def register_workspace(body: WorkspaceBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        try:
            workspace = agent_runtime.resolve_or_register_workspace(body.path)
            return agent_runtime.workspace_status(workspace.workspace_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/workspaces")
    async def list_workspaces() -> list[dict[str, Any]]:
        return [
            agent_runtime.workspace_status(w.workspace_id)
            for w in agent_runtime.store.list_workspaces()
        ]

    @router.get("/api/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> dict[str, Any]:
        try:
            return agent_runtime.workspace_status(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/workspaces/{workspace_id}/capabilities")
    async def get_workspace_capabilities(workspace_id: str) -> dict[str, Any]:
        try:
            return agent_runtime.workspace_capabilities(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/workspaces/{workspace_id}")
    async def remove_workspace(workspace_id: str, request: Request) -> dict[str, str]:
        require_local_request(request)
        await get_workspace(workspace_id)
        try:
            if agent_runtime.workspace_busy(workspace_id):
                raise ValueError("Workspace is busy")
            agent_runtime.store.remove_workspace(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"removed": workspace_id}

    return router
