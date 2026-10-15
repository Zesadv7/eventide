"""Read the Host queue; cancellation uses the existing exact Run endpoint."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from eventide.http_api.context import ApiContext


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["queue"])

    @router.get("/api/queue")
    async def get_queue(
        project_id: str | None = Query(default=None, min_length=1),
    ) -> dict[str, Any]:
        try:
            return context.runtime.dispatch.snapshot(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
