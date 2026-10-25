"""Workspace tool catalog search (MK-16).

The endpoint projects the deterministic always-available base catalog through
the same ``ToolCatalog`` snapshot used inside runs. On-demand MCP catalogs and
loaded receipts are run-scoped and not persisted in this slice, so nothing here
pretends to be loaded; ``loaded`` stays false until run-scoped loading is
exposed through persisted facts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from eventide.host import BASE_RUNTIME_READONLY_TOOLS, READ_TOOL_RESULT_TOOL, TODO_WRITE_TOOL
from eventide.http_api.context import ApiContext
from eventide.tool_discovery import ToolCatalog


class ToolCatalogItem(BaseModel):
    name: str
    source: str
    version: str
    description: str
    effect: str
    environment: str
    status: str
    disabled_reason: str | None
    provided: bool
    loaded: bool


class ToolCatalogResponse(BaseModel):
    catalog_version: str
    total: int
    offset: int
    limit: int
    items: list[ToolCatalogItem]
    loading_note: str


def base_catalog_tools(runtime: Any) -> list[dict[str, Any]]:
    return [*runtime.tools, READ_TOOL_RESULT_TOOL, TODO_WRITE_TOOL]


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["tools"])

    @router.get("/api/workspaces/{workspace_id}/tools", response_model=ToolCatalogResponse)
    async def search_tools(
        workspace_id: str,
        query: str = "",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> ToolCatalogResponse:
        await context.get_workspace(workspace_id)
        catalog = ToolCatalog(
            base_catalog_tools(context.runtime),
            [],
            readonly_names=BASE_RUNTIME_READONLY_TOOLS,
        )
        page = catalog.search(query, offset, limit)
        return ToolCatalogResponse(
            catalog_version=page["catalog_version"],
            total=page["total"],
            offset=page["offset"],
            limit=page["limit"],
            items=[ToolCatalogItem(**item, loaded=False) for item in page["items"]],
            loading_note=(
                "On-demand MCP tools and loaded receipts are run-scoped; this endpoint "
                "lists the always-available base catalog only and never reports a "
                "persisted loaded state."
            ),
        )

    return router
