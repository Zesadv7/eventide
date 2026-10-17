"""Local, same-origin, revisioned preference management with atomic receipts."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import begin_mutation, fail_mutation, mutation_key


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: Literal["global", "project"]
    project_id: str | None = None
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    content: str = Field(min_length=1, max_length=500)


class MemoryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class MemoryUpdate(MemoryRevision):
    content: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["active", "paused", "revoked"] | None = None


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    enabled: bool


class MemoryResolve(MemoryRevision):
    action: Literal["accept", "dismiss"]


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["memory"])
    host = context.runtime

    def mutate(
        request: Request, operation: str, body: BaseModel, target: str | None = None
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        owner = host.configuration_owner()
        data = body.model_dump()
        receipt = begin_mutation(
            host, request, owner, "memory_" + operation, {"target": target, **data}
        )
        if receipt.result is not None:
            return receipt.result
        try:
            return host.memory.mutate(operation, data, identity=receipt.identity, target=target)
        except ValueError as exc:
            message = str(exc)
            code = (
                404
                if "not found" in message.lower()
                else (409 if any(s in message.lower() for s in ("conflict", "revived")) else 422)
            )
            fail_mutation(host, receipt, message, code, rejected=True)
            raise HTTPException(
                code, message, headers={"X-Eventide-Mutation-Outcome": "rejected"}
            ) from exc
        except Exception:
            raise HTTPException(
                503, "Memory mutation outcome is unknown; retry original identity"
            ) from None

    @router.get("/api/memory")
    async def get_memory(
        project_id: str | None = None, include_revoked: bool = False
    ) -> dict[str, Any]:
        try:
            return host.memory.state(project_id, include_revoked=include_revoked)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/memory", status_code=201)
    async def create_memory(request: Request, body: MemoryCreate) -> dict[str, Any]:
        return mutate(request, "create", body)

    # Static route must precede /{memory_id}.
    @router.patch("/api/memory/settings")
    async def settings(request: Request, body: MemorySettings) -> dict[str, Any]:
        return mutate(request, "settings", body)

    @router.patch("/api/memory/{memory_id}")
    async def update_memory(memory_id: str, request: Request, body: MemoryUpdate) -> dict[str, Any]:
        return mutate(request, "update", body, memory_id)

    @router.delete("/api/memory/{memory_id}")
    async def delete_memory(
        memory_id: str, request: Request, body: MemoryRevision
    ) -> dict[str, Any]:
        return mutate(request, "delete", body, memory_id)

    @router.post("/api/memory/suggestions/{suggestion_id}/resolve")
    async def resolve_suggestion(
        suggestion_id: str, request: Request, body: MemoryResolve
    ) -> dict[str, Any]:
        return mutate(request, "resolve", body, suggestion_id)

    @router.get("/api/runs/{run_id}/memory")
    async def run_memory(run_id: str) -> dict[str, Any]:
        try:
            return host.memory.run_details(run_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
