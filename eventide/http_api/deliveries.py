"""Durable, same-origin delivery operations and explicitly reviewed adoption."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide.delivery_control import WebDeliveryController
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import MUTATION_OUTCOME_HEADER, mutation_key, run_receipt
from eventide.scheduler import QueueFull


class DeliveryPrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")


class DeliveryRepairBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=20_000)
    checks: list[str] = Field(min_length=1, max_length=4)
    image: str | None = Field(default=None, min_length=1, max_length=512)
    max_rounds: int = Field(default=3, ge=1, le=3)
    observed_token_limit: int = Field(default=60_000, ge=1, le=1_000_000)
    knowledge_index: Literal["existing", "prepare"] = "existing"


class DeliveryApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeliveryOperation(BaseModel):
    run_id: str
    session_id: str
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    accepted: dict[str, Any]
    error: str | None
    reason: str | None


class DeliveryReceipt(BaseModel):
    run_id: str
    delivery_id: str
    project_id: str
    status_url: str
    events_url: str


class DeliveryCheck(BaseModel):
    call_id: str
    name: str
    content: str
    is_error: bool
    metadata: dict[str, Any]


class DeliveryRound(BaseModel):
    delivery_id: str
    round: int
    worker_run_id: str
    patch_sha256: str
    status: str
    checks: list[DeliveryCheck]


class DeliveryUsage(BaseModel):
    model_config = ConfigDict(extra="allow")
    attempt_count: int
    reported_totals: dict[str, int]
    estimated_totals: dict[str, int]
    unknown_attempts: int
    incomplete_attempts: int
    complete: bool


class DeliveryRepairResult(BaseModel):
    delivery_id: str
    control_run_id: str
    reason: str
    verified_candidates: int
    usage: DeliveryUsage


class DeliveryDetail(BaseModel):
    delivery_id: str
    workspace_id: str
    name: str
    receipt: dict[str, Any]
    repair: dict[str, Any] | None
    repair_result: DeliveryRepairResult | None
    repair_status: Literal["running", "passed", "stopped", "interrupted"] | None
    rounds: list[DeliveryRound]
    verification: dict[str, Any] | None
    applied: bool


class DeliveryPatch(BaseModel):
    delivery_id: str
    patch_sha256: str
    patch: str


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["deliveries"])
    controller = WebDeliveryController(context.runtime)

    async def accept(
        project_id: str,
        action: str,
        body: dict[str, Any],
        request: Request,
        delivery_id: str | None = None,
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        try:
            owner = controller.owner(project_id, delivery_id, create=False)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        receipt = run_receipt(context.runtime, request, owner, f"delivery_{action}", body)
        if not receipt.existing:
            try:
                await controller.admit(
                    project_id,
                    receipt.run_id,
                    action=action,
                    delivery_id=delivery_id,
                    **body,
                    request_fingerprint=receipt.fingerprint,
                )
            except (ValueError, RuntimeError) as exc:
                rejected = context.runtime.store.get_run_identity(receipt.run_id) is None
                raise HTTPException(
                    429 if isinstance(exc, QueueFull) else 409,
                    str(exc),
                    headers={MUTATION_OUTCOME_HEADER: "rejected"} if rejected else None,
                ) from exc
            controller.start(receipt.run_id)
        accepted = controller.operation(receipt.run_id)["accepted"]
        return {
            "run_id": receipt.run_id,
            "delivery_id": accepted["delivery_id"],
            "project_id": project_id,
            "status_url": f"/api/delivery-operations/{receipt.run_id}",
            "events_url": f"/api/runs/{receipt.run_id}/events",
        }

    def project_for(delivery_id: str) -> str:
        try:
            value = controller.delivery.load(delivery_id)
            return context.runtime.store.get_workspace(value["workspace_id"]).project_id
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post(
        "/api/projects/{project_id}/deliveries", status_code=202, response_model=DeliveryReceipt
    )
    async def prepare(
        project_id: str, body: DeliveryPrepareBody, request: Request
    ) -> dict[str, Any]:
        return await accept(project_id, "prepare", body.model_dump(), request)

    @router.post(
        "/api/deliveries/{delivery_id}/repair", status_code=202, response_model=DeliveryReceipt
    )
    async def repair(
        delivery_id: str, body: DeliveryRepairBody, request: Request
    ) -> dict[str, Any]:
        return await accept(
            project_for(delivery_id), "repair",
            body.model_dump(exclude=(
                ({"observed_token_limit"} if body.observed_token_limit == 60_000 else set())
                | ({"knowledge_index"} if body.knowledge_index == "existing" else set())
            )),
            request, delivery_id,
        )

    @router.post(
        "/api/deliveries/{delivery_id}/apply", status_code=202, response_model=DeliveryReceipt
    )
    async def apply(delivery_id: str, body: DeliveryApplyBody, request: Request) -> dict[str, Any]:
        return await accept(
            project_for(delivery_id), "apply", body.model_dump(), request, delivery_id
        )

    @router.get("/api/projects/{project_id}/deliveries", response_model=list[DeliveryDetail])
    async def history(project_id: str) -> list[dict[str, Any]]:
        try:
            return controller.list(project_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/deliveries/{delivery_id}", response_model=DeliveryDetail)
    async def detail(delivery_id: str) -> dict[str, Any]:
        try:
            return controller.delivery.load(delivery_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/deliveries/{delivery_id}/patch", response_model=DeliveryPatch)
    async def patch(delivery_id: str) -> dict[str, Any]:
        try:
            text = controller.patch(delivery_id)
            value = controller.delivery.load(delivery_id)
            return {
                "delivery_id": delivery_id,
                "patch_sha256": value["verification"]["patch_sha256"],
                "patch": text,
            }
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get(
        "/api/projects/{project_id}/delivery-operations", response_model=list[DeliveryOperation]
    )
    async def operations(
        project_id: str, limit: int = Query(default=20, ge=1, le=100), before: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            return controller.operations(project_id, limit=limit, before=before)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/api/delivery-operations/{run_id}", response_model=DeliveryOperation)
    async def operation(run_id: str) -> dict[str, Any]:
        try:
            return controller.operation(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/delivery-operations/{run_id}/cancel", response_model=DeliveryOperation)
    async def cancel(run_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = mutation_key(request)
        try:
            controller.operation(run_id)
            if not context.runtime.store.run_is_terminal(run_id):
                from eventide.workspace import digest

                marker = digest(key)
                if not any(
                    e["type"] == "delivery.cancel_requested"
                    and e["payload"].get("request") == marker
                    for e in context.runtime.store.run_events(run_id)
                ):
                    context.runtime.store.append_event(
                        run_id,
                        "delivery.cancel_requested",
                        {
                            "request": marker,
                        },
                    )
                try:
                    await context.runtime.cancel_run(run_id)
                except ValueError:
                    if not context.runtime.store.run_is_terminal(run_id):
                        raise
            return controller.operation(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
