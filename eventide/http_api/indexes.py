"""Same-origin idempotent index admission, status and exact cancellation."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import MUTATION_OUTCOME_HEADER, mutation_key, run_receipt
from eventide.index_control import IndexController
from eventide.retrieval_runtime import index_status
from eventide.scheduler import QueueFull
from eventide.utils import owned_local_work


class IndexBuildBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    force: bool = False
    code_structure: bool = False
    workspace_id: str | None = None


class IndexReceipt(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    workspace_id: str
    status_url: str
    events_url: str


class IndexConfiguration(BaseModel):
    provider: str
    model: str
    algorithm_version: str
    dimension: int | None
    config_hash: str


class IndexStatistics(BaseModel):
    files: int
    rebuilt_files: int
    deleted_files: int
    chunks: int
    skipped: int
    duration_ms: float
    structured_files: int
    structure_fallbacks: dict[str, int]
    chunking_fingerprint: str
    generation: int


class IndexPublication(BaseModel):
    generation: int
    stats: IndexStatistics


class IndexDetail(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    workspace_id: str
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    configuration: IndexConfiguration
    force: bool
    code_structure: bool
    published: IndexPublication | None
    error: str | None
    events_url: str


class WorkspaceIndexStatus(BaseModel):
    status: Literal["available", "unavailable"]
    workspace_id: str
    location: Literal["managed", "legacy"]
    reason: str | None = None
    detail: str | None = None
    model: str | None = None
    generation: int | None = None
    embedding_fingerprint: str | None = None
    config_hash: str | None = None


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["indexes"])
    controller = IndexController(context.runtime)

    @router.get("/api/workspaces/{workspace_id}/index-status", response_model=WorkspaceIndexStatus)
    async def status(workspace_id: str) -> dict[str, Any]:
        try:
            workspace = context.runtime.store.get_workspace(workspace_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return await owned_local_work(index_status, context.runtime, workspace)

    @router.get("/api/indexes/configuration", response_model=IndexConfiguration)
    async def configuration() -> dict[str, Any]:
        try:
            return controller.configuration()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/projects/{project_id}/indexes", status_code=202,
                 response_model=IndexReceipt)
    async def build(project_id: str, body: IndexBuildBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        try:
            owner = controller.owner(project_id, create=False, workspace_id=body.workspace_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        payload = body.model_dump()
        if body.workspace_id is None:
            payload.pop("workspace_id")
        receipt = run_receipt(context.runtime, request, owner, "index", payload)
        if not receipt.existing:
            try:
                await controller.admit(project_id, receipt.run_id, **body.model_dump(),
                                       request_fingerprint=receipt.fingerprint)
            except (ValueError, RuntimeError) as exc:
                rejected = context.runtime.store.get_run_identity(receipt.run_id) is None
                raise HTTPException(429 if isinstance(exc, QueueFull) else 409, str(exc),
                                    headers={MUTATION_OUTCOME_HEADER: "rejected"}
                                    if rejected else None) from exc
            controller.start(receipt.run_id)
        return {"run_id": receipt.run_id, "session_id": owner, "project_id": project_id,
                "workspace_id": context.runtime.sessions.workspace(owner).workspace_id,
                "status_url": f"/api/indexes/{receipt.run_id}",
                "events_url": f"/api/runs/{receipt.run_id}/events"}

    @router.get("/api/projects/{project_id}/indexes", response_model=list[IndexDetail])
    async def history(project_id: str, limit: int = Query(default=20, ge=1, le=100),
                      before: str | None = None) -> list[dict[str, Any]]:
        try:
            return controller.list(project_id, limit=limit, before=before)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/indexes/{run_id}", response_model=IndexDetail)
    async def detail(run_id: str) -> dict[str, Any]:
        try:
            return controller.detail(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/indexes/{run_id}/cancel", response_model=IndexDetail)
    async def cancel(run_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = mutation_key(request)
        try:
            controller.detail(run_id)
            if not context.runtime.store.run_is_terminal(run_id):
                from eventide.workspace import digest

                marker = digest(key)
                if not any(e["type"] == "index.cancel_requested"
                           and e["payload"].get("request") == marker
                           for e in context.runtime.store.run_events(run_id)):
                    context.runtime.store.append_event(run_id, "index.cancel_requested", {
                        "request": marker,
                    })
                try:
                    await context.runtime.cancel_run(run_id)
                except ValueError:
                    if not context.runtime.store.run_is_terminal(run_id):
                        raise
            return controller.detail(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
