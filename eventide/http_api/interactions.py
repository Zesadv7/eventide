"""In-run supplement routes on the trusted local path."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide import interactions
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.run_mutations import mutate_run_action
from eventide.http_control import mutation_key


class SupplementBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body: str = Field(min_length=1, max_length=interactions.SUPPLEMENT_BODY_LIMIT)
    body_version: int = Field(default=1, ge=1, le=9)


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter()
    agent_runtime = context.runtime

    @router.post("/api/runs/{run_id}/supplements")
    async def add_supplement(
        run_id: str, body: SupplementBody, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            run = agent_runtime.store.get_run(run_id)
            if not run:
                raise HTTPException(404, "Run not found")
            try:
                normalized = interactions.normalize_supplement_body(body.body)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            status = interactions.supplement_status(run["status"])
            supplement_id = f"sup_{uuid.uuid4().hex[:16]}"
            agent_runtime.store.record_supplement(run_id, {
                "supplement_id": supplement_id,
                "run_id": run_id,
                "session_id": run["session_id"],
                "body": normalized,
                "body_version": body.body_version,
                "status": status,
            })
            return {
                "supplement_id": supplement_id,
                "run_id": run_id,
                "status": status,
                "consumed": False,
            }

        return await mutate_run_action(
            agent_runtime, request, "run_supplement",
            {"run_id": run_id, **body.model_dump()}, execute,
        )

    @router.get("/api/runs/{run_id}/supplements")
    async def list_supplements(run_id: str) -> dict[str, Any]:
        if not agent_runtime.store.get_run_identity(run_id):
            raise HTTPException(404, "Run not found")
        supplements = agent_runtime.store.interaction_supplements(run_id)
        return {
            "run_id": run_id,
            "supplements": supplements,
            "pending": sum(
                1 for s in supplements if s["status"] == "pending"
            ),
        }

    return router
