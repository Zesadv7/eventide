"""Run-scoped frozen tool directory projection (MK-16).

Read-only HTTP view over persisted run facts. Discovery and load state come
from the run EventLog only; the endpoint never consults the current MCP
manager, so a restarted store serves the same frozen directory and a changed
MCP server cannot rewrite an old run's history.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from eventide.http_api.context import ApiContext
from eventide.tool_projection import filter_run_tools, project_run_tools, run_tool_detail


class RunToolItem(BaseModel):
    name: str
    source: str
    version: str
    description: str
    effect: str
    environment: str
    status: str
    disabled_reason: str | None
    provided: bool
    schema_status: str
    schema_chars: int | None
    loaded: bool
    loaded_seq: int | None
    load_record: dict[str, Any] | None


class RunToolCatalogResponse(BaseModel):
    run_id: str
    catalog_frozen: bool
    catalog_version: str | None
    frozen_seq: int | None
    total: int
    catalog_total: int
    offset: int
    limit: int
    items: list[RunToolItem]
    loaded_count: int
    failed_loads: list[dict[str, Any]]
    stale_receipts: int
    notes: list[str] = Field(default_factory=list)


class RunToolDetailResponse(RunToolItem):
    input_schema: dict[str, Any] | None


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["tools"])
    store = context.runtime.store

    def projection_for(run_id: str) -> dict[str, Any]:
        if store.get_run_identity(run_id) is None:
            raise HTTPException(404, "Run not found")
        events = store.run_events(run_id)
        return project_run_tools(events, run_id=run_id)

    @router.get("/api/runs/{run_id}/tools", response_model=RunToolCatalogResponse)
    async def run_tool_catalog(
        run_id: str,
        query: str = "",
        source: str = "",
        status: str = "all",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> RunToolCatalogResponse:
        try:
            page = filter_run_tools(
                projection_for(run_id),
                query=query,
                source=source,
                status=status,
                offset=offset,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        items = page.pop("items")
        return RunToolCatalogResponse(**page, items=[RunToolItem(**item) for item in items])

    @router.get("/api/runs/{run_id}/tools/{name}", response_model=RunToolDetailResponse)
    async def run_tool_entry(run_id: str, name: str) -> RunToolDetailResponse:
        detail = run_tool_detail(projection_for(run_id), name)
        if detail is None:
            raise HTTPException(
                404,
                "Tool is not part of this run's frozen directory; discovery never "
                "enumerates the current MCP server for old runs.",
            )
        return RunToolDetailResponse(**detail)

    return router
