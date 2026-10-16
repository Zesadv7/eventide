"""User-controlled worktree catalog and durable mutation operations."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import MUTATION_OUTCOME_HEADER, mutation_key, run_receipt
from eventide.scheduler import QueueFull
from eventide.worktree_control import WorktreeController


class WorktreeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["create", "keep", "remove"]
    name: str = Field(min_length=1, max_length=64)
    ref: str = Field(default="HEAD", min_length=1, max_length=256)
    expected_catalog_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorktreeItem(BaseModel):
    name: str
    workspace_id: str
    path: str
    branch: str
    base_commit: str
    repository: str
    git_common_dir: str
    kept: bool


class WorktreeCatalogItem(WorktreeItem):
    history_protected: bool


class WorktreeCatalog(BaseModel):
    project_id: str
    catalog_hash: str
    worktrees: list[WorktreeCatalogItem]


class WorktreeReceipt(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    status_url: str
    events_url: str


class WorktreeDetail(BaseModel):
    run_id: str
    session_id: str
    project_id: str
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    action: Literal["create", "keep", "remove"]
    name: str
    ref: str
    expected_catalog_hash: str
    result: WorktreeItem | None
    error: str | None
    events_url: str


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["worktrees"])
    controller = WorktreeController(context.runtime)

    @router.get("/api/projects/{project_id}/worktrees", response_model=WorktreeCatalog)
    async def catalog(project_id: str) -> dict[str, Any]:
        try:
            return await controller.catalog(project_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/projects/{project_id}/worktrees", status_code=202,
                 response_model=WorktreeReceipt)
    async def manage(project_id: str, body: WorktreeBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        try:
            owner = controller.owner(project_id, create=False)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        receipt = run_receipt(
            context.runtime, request, owner, "worktree_control", body.model_dump(),
        )
        if not receipt.existing:
            try:
                await controller.admit(project_id, receipt.run_id, **body.model_dump(),
                                       request_fingerprint=receipt.fingerprint)
            except (ValueError, RuntimeError, OSError) as exc:
                rejected = context.runtime.store.get_run_identity(receipt.run_id) is None
                raise HTTPException(429 if isinstance(exc, QueueFull) else 409, str(exc),
                                    headers={MUTATION_OUTCOME_HEADER: "rejected"}
                                    if rejected else None) from exc
            controller.start(receipt.run_id)
        return {"run_id": receipt.run_id, "session_id": owner, "project_id": project_id,
                "status_url": f"/api/worktree-operations/{receipt.run_id}",
                "events_url": f"/api/runs/{receipt.run_id}/events"}

    @router.get("/api/projects/{project_id}/worktree-operations",
                response_model=list[WorktreeDetail])
    async def history(project_id: str, limit: int = Query(default=20, ge=1, le=100),
                      before: str | None = None) -> list[dict[str, Any]]:
        try:
            return controller.history(project_id, limit=limit, before=before)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/worktree-operations/{run_id}", response_model=WorktreeDetail)
    async def detail(run_id: str) -> dict[str, Any]:
        try:
            return controller.detail(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/worktree-operations/{run_id}/cancel", response_model=WorktreeDetail)
    async def cancel(run_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = mutation_key(request)
        try:
            controller.detail(run_id)
            if not context.runtime.store.run_is_terminal(run_id):
                from eventide.workspace import digest

                marker = digest(key)
                if not any(e["type"] == "worktree.cancel_requested"
                           and e["payload"].get("request") == marker
                           for e in context.runtime.store.run_events(run_id)):
                    context.runtime.store.append_event(run_id, "worktree.cancel_requested",
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
