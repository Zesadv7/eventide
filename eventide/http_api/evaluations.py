"""Same-origin, idempotent admission of reviewed bundled evaluation batches."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide.evaluation_control import EvaluationController
from eventide.evaluation_quote import evaluation_quote
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import MUTATION_OUTCOME_HEADER, mutation_key, run_receipt
from eventide.scheduler import QueueFull


class EvaluationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    suite_id: Literal["engineering-v1"] = "engineering-v1"
    suite_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    split: Literal["development", "holdout", "all"] = "development"
    mode: Literal["offline", "live"] = "offline"
    quote_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = False


class EvaluationSuite(BaseModel):
    id: str
    benchmark_id: str
    version: int
    description: str
    split: None = None
    total: int
    splits: dict[str, int]
    manifest_sha256: str
    suite_sha256: str
    inputs_sha256: str
    supported_modes: list[Literal["offline", "live"]] = ["offline", "live"]
    default_mode: Literal["offline"] = "offline"


class EvaluationReceipt(BaseModel):
    evaluation_id: str
    run_id: str
    session_id: str
    project_id: str
    status: Literal["accepted"] = "accepted"
    status_url: str
    events_url: str


class EvaluationCase(BaseModel):
    id: str
    category: str
    split: Literal["development", "holdout"]
    status: Literal["pending", "running", "completed", "failed", "timed_out", "unknown",
                    "interrupted"]
    passed: bool | None
    reasons: list[str]
    duration_ms: float | None


class EvaluationDetail(BaseModel):
    evaluation_id: str
    run_id: str
    session_id: str
    project_id: str
    suite_id: str
    suite_version: int
    suite_sha256: str
    split: Literal["development", "holdout", "all"]
    mode: Literal["offline", "live"]
    measurement_kind: Literal["scripted_mechanism_validation", "provider_experiment"]
    billing_applicable: bool
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    dispatch_state: Literal["queued", "starting", "running", "finishing"] | None
    queue_position: int | None
    total: int
    attempted: int
    passed: int
    failed: int
    remaining: int
    current_case: str | None
    cases: list[EvaluationCase]
    report_available: bool
    report_url: str
    events_url: str
    error: str | None


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["evaluations"])
    controller = EvaluationController(context.runtime)

    @router.get("/api/evaluations/suites", response_model=list[EvaluationSuite])
    async def suites() -> list[dict[str, Any]]:
        from eventide.bundled_evaluations import builtin_suite_catalog

        return builtin_suite_catalog()

    @router.get("/api/projects/{project_id}/evaluations/quote")
    async def quote(
        project_id: str, split: Literal["development", "holdout", "all"] = "development",
    ) -> dict[str, Any]:
        try:
            return evaluation_quote(context.runtime, project_id, split=split)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/projects/{project_id}/evaluations", status_code=202,
                 response_model=EvaluationReceipt)
    async def create(project_id: str, body: EvaluationBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        try:
            owner = controller.owner(project_id, create=False)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        payload = body.model_dump()
        if body.mode == "offline":
            # Preserve the identity of pre-live queued and retried offline requests.
            payload.pop("quote_hash")
            payload.pop("confirmed")
        receipt = run_receipt(context.runtime, request, owner, "evaluation", payload)
        if not receipt.existing:
            try:
                await controller.admit(
                    project_id, receipt.run_id, suite_id=body.suite_id,
                    suite_sha256=body.suite_sha256, split=body.split,
                    mode=body.mode, quote_hash=body.quote_hash, confirmed=body.confirmed,
                    request_fingerprint=receipt.fingerprint,
                )
            except ValueError as exc:
                code = 429 if isinstance(exc, QueueFull) else 409
                rejected = context.runtime.store.get_run_identity(receipt.run_id) is None
                raise HTTPException(code, str(exc), headers={MUTATION_OUTCOME_HEADER: "rejected"}
                                    if rejected else None) from exc
            controller.start(receipt.run_id)
        return {
            "evaluation_id": receipt.run_id, "run_id": receipt.run_id,
            "session_id": owner, "project_id": project_id, "status": "accepted",
            "status_url": f"/api/evaluations/{receipt.run_id}",
            "events_url": f"/api/runs/{receipt.run_id}/events",
        }

    @router.get("/api/projects/{project_id}/evaluations", response_model=list[EvaluationDetail])
    async def list_evaluations(project_id: str, limit: int = Query(default=20, ge=1, le=100),
                               before: str | None = None) -> list[dict[str, Any]]:
        try:
            return controller.list(project_id, limit=limit, before=before)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/evaluations/{evaluation_id}", response_model=EvaluationDetail)
    async def detail(evaluation_id: str) -> dict[str, Any]:
        try:
            return controller.detail(evaluation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/evaluations/{evaluation_id}/cancel", response_model=EvaluationDetail)
    async def cancel(evaluation_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = mutation_key(request)
        try:
            return await controller.cancel(evaluation_id, key)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/evaluations/{evaluation_id}/report")
    async def report(evaluation_id: str) -> dict[str, Any]:
        try:
            controller.detail(evaluation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            return controller.report(evaluation_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
