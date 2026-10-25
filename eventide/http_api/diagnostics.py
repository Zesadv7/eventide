"""Read-only, fact-derived run diagnostics report with bounded downloads.

Both endpoints project the same committed events under one high-water mark.
Generating a report never runs models or tools, never appends events, and an
older cut never picks up events committed after it. Markdown is rendered from
the same JSON facts; neither format includes conversation bodies, source code,
full tool outputs, or automatic export to external services.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from eventide.diagnostics import (
    build_run_diagnostics,
    collect_run_diagnostics,
    render_diagnostics_markdown,
)
from eventide.http_api.context import ApiContext
from eventide.normalization import normalize


class DiagnosticReference(BaseModel):
    event_type: str | None = None
    at: float | None = None
    call_id: str | None = None
    attempt_id: str | None = None


class KnownCause(BaseModel):
    code: str
    label: str
    detail: str | None = None
    ref: DiagnosticReference | None = None


class PendingItem(BaseModel):
    code: str
    label: str
    detail: str | None = None
    ref: DiagnosticReference | None = None


class ToolFact(BaseModel):
    call_id: str
    name: str
    readonly: bool
    status: str
    detail: str | None = None
    duration_ms: float | None = None
    at: float | None = None


class AttemptFact(BaseModel):
    attempt_id: str
    attempt: int | None = None
    kind: str | None = None
    status: str
    usage_source: str
    model: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class RunSummary(BaseModel):
    what_happened: str
    where_stopped: str | None
    completed: str
    unknowns: list[str]
    next_steps: list[str]


class RunDiagnosticsReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    report_version: int
    source: str
    run_id: str
    high_water: int
    facts_available: bool
    status: str
    stop_reason: str | None = None
    mode: str | None = None
    provider: str | None = None
    model: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    counts: dict[str, int]
    summary: RunSummary
    known_causes: list[KnownCause]
    pending: list[PendingItem]
    tools: list[ToolFact]
    attempts: list[AttemptFact]
    unavailable: list[str]
    notes: list[str]
    truncated: bool


_FORMATS = {"json", "markdown"}


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])
    runtime = context.runtime

    def _report(run_id: str, high_water: int | None) -> dict[str, Any]:
        if high_water is not None and high_water < 0:
            raise HTTPException(422, "high_water must be a non-negative integer")
        try:
            events, bound = collect_run_diagnostics(runtime.store, run_id, high_water)
            report = build_run_diagnostics(events, run_id=run_id, high_water=bound)
        except ValueError as exc:
            message = str(exc)
            if message == "Run not found":
                raise HTTPException(404, "Run not found") from exc
            raise HTTPException(422, message) from exc
        return normalize(report, (runtime.settings.api_key or "",))

    def _safe_name(run_id: str) -> str:
        keep = "".join(c for c in run_id if c.isalnum() or c in "-_")[:40]
        return keep or "run"

    @router.get(
        "/api/runs/{run_id}/diagnostics",
        response_model=RunDiagnosticsReport,
        responses={404: {"description": "Run not found"}},
    )
    async def run_diagnostics(run_id: str, high_water: int | None = None) -> Any:
        return _report(run_id, high_water)

    @router.get("/api/runs/{run_id}/diagnostics/download", response_class=Response)
    async def run_diagnostics_download(
        run_id: str, format: str = "json", high_water: int | None = None
    ) -> Response:
        if format not in _FORMATS:
            raise HTTPException(422, "format must be json or markdown")
        report = _report(run_id, high_water)
        stem = f"eventide-diagnostics-{_safe_name(run_id)}-{report['high_water']}"
        if format == "markdown":
            body = render_diagnostics_markdown(report)
            media, suffix = "text/markdown; charset=utf-8", ".md"
        else:
            body = json.dumps(report, ensure_ascii=False, allow_nan=False)
            media, suffix = "application/json", ".json"
        return Response(
            body,
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="{stem}{suffix}"',
                "Cache-Control": "no-store",
            },
        )

    return router
