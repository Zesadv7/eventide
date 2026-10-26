"""Identity-bound paging of redacted shell/skill command logs (MK05)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from eventide.command_logs import PAGE_MAX_CHARS
from eventide.http_api.context import ApiContext


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["command-logs"])

    @router.get("/api/runs/{run_id}/calls/{call_id}/command-log")
    async def command_log(
        run_id: str,
        call_id: str,
        stream: str = Query("stdout", pattern="^(stdout|stderr)$"),
        offset: int = Query(0, ge=0),
        limit: int = Query(4_000, ge=1, le=PAGE_MAX_CHARS),
    ) -> Any:
        runtime = context.runtime
        if runtime.store.get_run_identity(run_id) is None:
            raise HTTPException(404, "Run not found")
        page = runtime.command_logs.page(run_id, call_id, stream, offset, limit)
        if page.get("availability") in {"unknown_run", "not_recorded", "invalid_stream"}:
            raise HTTPException(404, f"Command log not recorded: {page['availability']}")
        # Failed reads keep 200 with an explicit availability body: the fact the
        # log once existed is itself the run's history.
        return page

    return router
