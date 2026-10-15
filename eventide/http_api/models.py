"""Models HTTP routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.schemas import GenerationBody


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["models"])
    agent_runtime = context.runtime
    get_session = context.get_session

    @router.get("/api/generation/defaults")
    async def generation_defaults(model: str | None = None) -> dict[str, Any]:
        return agent_runtime.generation_settings(model or agent_runtime.settings.model)

    @router.put("/api/generation/defaults")
    async def update_generation_defaults(body: GenerationBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        try:
            declared = body.capabilities
            if declared:
                declared = declared.model_copy(update={"source": "declared"})
            return agent_runtime.configure_generation(
                body.model,
                body.generation.patch(),
                declared=declared,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/sessions/{session_id}/generation")
    async def session_generation(session_id: str, model: str | None = None) -> dict[str, Any]:
        await get_session(session_id)
        return agent_runtime.generation_settings(model or agent_runtime.settings.model, session_id)

    @router.put("/api/sessions/{session_id}/generation")
    async def update_session_generation(
        session_id: str,
        body: GenerationBody,
        request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        await get_session(session_id)
        if body.capabilities:
            raise HTTPException(status_code=422, detail="Declare capabilities in model defaults")
        try:
            return agent_runtime.configure_generation(
                body.model,
                body.generation.patch(),
                session_id=session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/models")
    async def list_models() -> dict[str, Any]:
        return await agent_runtime.available_models()

    return router
