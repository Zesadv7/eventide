"""FastAPI composition root; domain routes share one RuntimeHost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.http_api import attachments, config, models, runs, sessions, streams, workspaces
from eventide.http_api.context import ApiContext, ApprovalBroker
from eventide.http_api.errors import ERROR_RESPONSES, error_response, register_error_handlers
from eventide.http_api.schemas import (
    ApprovalBody,
    AttachmentBody,
    GenerationBody,
    ProviderConfigBody,
    RunBody,
    SessionBody,
    SessionUpdateBody,
    WorkspaceBody,
)
from eventide.http_control import check_mutation_origin

# Existing imports remain available while requests are owned by their domain.
__all__ = [
    "ApprovalBody",
    "ApprovalBroker",
    "AttachmentBody",
    "GenerationBody",
    "ProviderConfigBody",
    "RunBody",
    "SessionBody",
    "SessionUpdateBody",
    "WorkspaceBody",
    "create_app",
]

WEB_DIR = Path(__file__).with_name("web")


def _probe_error(exc: Exception) -> tuple[str, str]:
    return config.probe_error(exc)


def create_app(runtime: RuntimeHost | None = None) -> FastAPI:
    context = ApiContext(runtime or RuntimeHost(Settings.from_env()))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await context.runtime.initialize()
        try:
            yield
        finally:
            for task in list(context.tasks):
                task.cancel()
            if context.tasks:
                await asyncio.gather(*context.tasks, return_exceptions=True)
            await context.runtime.close()

    app = FastAPI(
        title="Eventide Runtime",
        version="0.4.0",
        description="Traceable, policy-aware execution for tool-using agents.",
        lifespan=lifespan,
        responses=ERROR_RESPONSES,
    )
    app.state.runtime = context.runtime
    app.state.approvals = context.approvals
    app.state.api_context = context
    register_error_handlers(app)

    @app.middleware("http")
    async def same_origin_mutations(request: Request, call_next: Any) -> Response:
        try:
            check_mutation_origin(request)
        except (HTTPException, ValueError) as exc:
            return error_response(
                403,
                exc.detail if isinstance(exc, HTTPException) else "Invalid origin",
                code="invalid_origin",
            )
        return await call_next(request)

    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def console() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime": "eventide"}

    for domain in (workspaces, sessions, runs, models, config, attachments, streams):
        app.include_router(domain.create_router(context))
    return app
