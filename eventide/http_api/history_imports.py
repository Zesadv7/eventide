"""Same-origin legacy history preview, review and durable import."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from eventide.history_import_control import HistoryImportController
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import MUTATION_OUTCOME_HEADER, mutation_key, run_receipt
from eventide.scheduler import QueueFull


class HistoryPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(min_length=1, max_length=4096)


class HistoryImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_true(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicit confirmation must be true")
        return True


class HistoryCounts(BaseModel):
    sessions: int
    runs: int
    messages: int
    events: int


class HistoryPreviewEvidence(BaseModel):
    sha256: str
    bytes: int
    counts: HistoryCounts


class HistoryImportResult(HistoryCounts):
    source: str
    workspace_id: str
    provider_config_imported: bool
    api_key_imported: bool
    read_only_history: bool
    first_session_id: str | None = None


class HistoryImportDetail(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    action: Literal["preview", "import"]
    imported: bool = False
    preview_id: str | None = None
    archive_sha256: str | None = None
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    preview: HistoryPreviewEvidence | None
    result: HistoryImportResult | None
    error: str | None
    events_url: str


class HistoryImportReceipt(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    status_url: str
    events_url: str


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["history-imports"])
    controller = HistoryImportController(context.runtime)

    async def accept(project_id: str, request: Request, action: str,
                     body: dict[str, Any]) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        try:
            owner = controller.owner(project_id, create=False)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        receipt = run_receipt(context.runtime, request, owner, "history_import",
                              {"action": action, **body})
        if not receipt.existing:
            try:
                await controller.admit(
                    project_id, receipt.run_id, action=action, **body,
                    request_fingerprint=receipt.fingerprint,
                )
            except (ValueError, RuntimeError, OSError) as exc:
                rejected = context.runtime.store.get_run_identity(receipt.run_id) is None
                raise HTTPException(429 if isinstance(exc, QueueFull) else 409, str(exc),
                                    headers={MUTATION_OUTCOME_HEADER: "rejected"}
                                    if rejected else None) from exc
            controller.start(receipt.run_id)
        return {"run_id": receipt.run_id, "session_id": owner, "project_id": project_id,
                "status_url": f"/api/history-imports/{receipt.run_id}",
                "events_url": f"/api/runs/{receipt.run_id}/events"}

    @router.post("/api/projects/{project_id}/history-imports/preview", status_code=202,
                 response_model=HistoryImportReceipt)
    async def preview(project_id: str, body: HistoryPreviewBody,
                      request: Request) -> dict[str, Any]:
        return await accept(project_id, request, "preview", body.model_dump())

    @router.post("/api/projects/{project_id}/history-imports", status_code=202,
                 response_model=HistoryImportReceipt)
    async def apply(project_id: str, body: HistoryImportBody,
                    request: Request) -> dict[str, Any]:
        return await accept(project_id, request, "import",
                            body.model_dump(exclude={"confirmed"}))

    @router.get("/api/projects/{project_id}/history-imports",
                response_model=list[HistoryImportDetail])
    async def history(project_id: str, limit: int = Query(default=20, ge=1, le=100),
                      before: str | None = None) -> list[dict[str, Any]]:
        try:
            return controller.history(project_id, limit=limit, before=before)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/history-imports/{run_id}", response_model=HistoryImportDetail)
    async def detail(run_id: str) -> dict[str, Any]:
        try:
            return controller.detail(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/history-imports/{run_id}/cancel", response_model=HistoryImportDetail)
    async def cancel(run_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = mutation_key(request)
        try:
            controller.detail(run_id)
            if not context.runtime.store.run_is_terminal(run_id):
                from eventide.workspace import digest

                marker = digest(key)
                if not any(e["type"] == "history.cancel_requested"
                           and e["payload"].get("request") == marker
                           for e in context.runtime.store.run_events(run_id)):
                    context.runtime.store.append_event(run_id, "history.cancel_requested",
                                                       {"request": marker})
                try:
                    await context.runtime.cancel_run(run_id)
                except ValueError:
                    if not context.runtime.store.run_is_terminal(run_id):
                        raise
            return controller.detail(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
    return router
