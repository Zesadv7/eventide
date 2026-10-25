"""FastAPI composition root; domain routes share one RuntimeHost."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.http_api import (
    attachments,
    budgets,
    config,
    contracts,
    deliveries,
    directories,
    evaluations,
    file_operations,
    history_imports,
    indexes,
    memory,
    models,
    projects,
    queue,
    runs,
    sessions,
    skills,
    streams,
    traces,
    workspaces,
    worktrees,
)
from eventide.http_api.context import ApiContext
from eventide.http_api.errors import ERROR_RESPONSES, error_response, register_error_handlers
from eventide.http_control import check_mutation_origin

WORKBENCH_DIR = Path(__file__).with_name("workbench")


def create_app(runtime: RuntimeHost | None = None) -> FastAPI:
    context = ApiContext(runtime or RuntimeHost(Settings.from_env()))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await context.runtime.initialize()
        try:
            yield
        finally:
            await context.runtime.close()

    app = FastAPI(
        title="Eventide Runtime",
        version="0.5.0",
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

    if (WORKBENCH_DIR / "index.html").is_file():
        app.mount("/workbench", StaticFiles(directory=WORKBENCH_DIR, html=True), name="workbench")
    else:
        @app.get("/workbench/", include_in_schema=False)
        async def missing_workbench() -> None:
            raise HTTPException(
                503, "Workbench assets missing; install a complete Eventide package"
            )

    @app.get("/", include_in_schema=False)
    async def workbench() -> RedirectResponse:
        return RedirectResponse("/workbench/", status_code=307)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime": "eventide"}

    for domain in (
        contracts, projects, workspaces, sessions, runs, models, config, attachments, streams,
        skills, budgets, queue, evaluations, indexes, deliveries, traces, worktrees,
        history_imports, directories, memory, file_operations,
    ):
        app.include_router(domain.create_router(context))
    return app
