"""GET-only attention center routes; user actions reuse the existing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from eventide.attention import collect_attention
from eventide.http_api.context import ApiContext


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["attention"])
    runtime = context.runtime

    @router.get("/api/attention")
    async def attention_center(
        approvals_limit: int = Query(default=50, ge=1, le=200),
        runs_limit: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        return collect_attention(
            runtime, approval_limit=approvals_limit, run_limit=runs_limit,
        )

    @router.get("/api/projects/{project_id}/attention")
    async def project_attention(
        project_id: str,
        approvals_limit: int = Query(default=50, ge=1, le=200),
        runs_limit: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        try:
            return collect_attention(
                runtime, project_id=project_id,
                approval_limit=approvals_limit, run_limit=runs_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
