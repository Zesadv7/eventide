"""Workspaces HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import WorkspaceBody
from eventide.http_api.workspace_management import mutate_workspace


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["workspaces"])
    agent_runtime = context.runtime

    @router.post("/api/workspaces", status_code=201)
    async def register_workspace(body: WorkspaceBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        return mutate_workspace(agent_runtime, request, path=body.path)

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
        return mutate_workspace(agent_runtime, request, workspace_id=workspace_id)

    return router
