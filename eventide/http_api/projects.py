"""Project aggregates over immutable Workspace and Session identities."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from eventide.http_api.context import ApiContext


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["projects"])
    host = context.runtime

    @router.get("/api/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return [host.project_status(project.project_id) for project in host.store.list_projects()]

    @router.get("/api/projects/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        try:
            return host.project_status(project_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/projects/{project_id}/workspaces")
    async def list_workspaces(project_id: str) -> list[dict[str, Any]]:
        await get_project(project_id)
        return [
            host.workspace_status(workspace.workspace_id)
            for workspace in host.store.list_project_workspaces(project_id)
        ]

    @router.get("/api/projects/{project_id}/sessions")
    async def list_sessions(
        project_id: str,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        await get_project(project_id)
        return [
            host.session_status(session["id"])
            for session in host.store.list_project_sessions(
                project_id,
                include_archived=include_archived,
            )
        ]

    return router
