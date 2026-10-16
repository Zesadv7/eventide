"""Read-only, privacy-preserving trace projection and explicit JSON download."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from eventide.http_api.context import ApiContext
from eventide.telemetry import build_run_trace, collect_trace_events


class TraceSpan(BaseModel):
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_ns: int
    end_time_ns: int
    status: str
    attributes: dict[str, str | int | float | bool | None]
    links: list[dict[str, Any]]


class TraceReport(BaseModel):
    schema_version: int
    source: str
    spans: list[TraceSpan]


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["traces"])

    @router.get("/api/runs/{run_id}/trace", response_model=TraceReport)
    async def trace(run_id: str, download: bool = False) -> Any:
        if context.runtime.store.get_run_identity(run_id) is None:
            raise HTTPException(404, "Run not found")
        try:
            report = build_run_trace(collect_trace_events(context.runtime.store, run_id),
                                     run_id=run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if download:
            return Response(json.dumps(report, ensure_ascii=False, allow_nan=False),
                            media_type="application/json", headers={
                                "Content-Disposition": 'attachment; filename="eventide-trace.json"',
                                "Cache-Control": "no-store",
                            })
        return report

    return router
