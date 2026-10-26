"""Local, idempotent task-graph control on exact root Run identities."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from eventide.agent_graph import GraphDefinition
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.run_mutations import mutate_run_action
from eventide.http_control import mutation_key


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter()
    host = context.runtime

    @router.get("/api/runs/{run_id}/graphs")
    async def list_graphs(run_id: str) -> dict[str, Any]:
        from eventide.agent_graph import projections

        if host.store.get_run_identity(run_id) is None:
            raise HTTPException(404, "Run not found")
        return {
            "run_id": run_id,
            "graphs": [g for g in projections(host.store) if g["root_run_id"] == run_id],
        }

    @router.get("/api/graphs/{graph_id}")
    async def get_graph(graph_id: str) -> dict[str, Any]:
        try:
            return host.agent_graphs.get(graph_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/runs/{run_id}/graphs", status_code=201)
    async def create_graph(run_id: str, body: GraphDefinition, request: Request) -> dict[str, Any]:
        require_local_request(request)
        key = mutation_key(request)

        async def execute() -> dict[str, Any]:
            try:
                return host.agent_graphs.create(run_id, body, key)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        return await mutate_run_action(
            host, request, "graph_create", {"run_id": run_id, **body.model_dump()}, execute
        )

    @router.post("/api/graphs/{graph_id}/advance")
    async def advance_graph(graph_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            try:
                return await host.agent_graphs.advance(graph_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        return await mutate_run_action(
            host, request, "graph_advance", {"graph_id": graph_id}, execute
        )

    @router.post("/api/graphs/{graph_id}/cancel")
    async def cancel_graph(graph_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            try:
                return await host.agent_graphs.cancel(graph_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        return await mutate_run_action(
            host, request, "graph_cancel", {"graph_id": graph_id}, execute
        )

    return router
