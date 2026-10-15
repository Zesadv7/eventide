"""Runs HTTP routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from eventide.http_api.context import ApiContext
from eventide.http_api.schemas import ApprovalBody, RunBody
from eventide.http_control import run_receipt
from eventide.models import RunRequest
from eventide.usage import build_usage_ledger


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["runs"])
    agent_runtime = context.runtime
    get_session = context.get_session
    broker = context.approvals
    tasks = context.tasks
    reserved = context.reserved

    @router.get("/api/sessions/{session_id}/runs")
    async def list_runs(
        session_id: str,
        limit: int = 100,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        await get_session(session_id)
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        return agent_runtime.session_runs(session_id, limit=limit, before=before)

    @router.post(
        "/api/sessions/{session_id}/continue",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def continue_session(session_id: str, request: Request) -> dict[str, str]:
        # Continue validation is part of admission; a rejected request returns 409,
        # without creating a run that could hide the parked source run.
        record = await get_session(session_id)
        receipt = run_receipt(agent_runtime, request, session_id, "continue", {})
        if receipt.existing:
            return {"run_id": receipt.run_id, "session_id": session_id, "status": "accepted"}
        workspace_id = record["workspace_id"]
        if workspace_id in reserved or agent_runtime.workspace_busy(workspace_id):
            raise HTTPException(status_code=409, detail="Workspace is busy")
        reserved.add(workspace_id)
        run_id = receipt.run_id
        try:
            await agent_runtime.admit_continue(
                session_id,
                run_id,
                **({"request_fingerprint": receipt.fingerprint} if receipt.fingerprint else {}),
            )
        except ValueError as exc:
            reserved.discard(workspace_id)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BaseException:
            reserved.discard(workspace_id)
            raise

        task = agent_runtime.start_admitted_run(run_id, approval_handler=broker.request)
        tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            tasks.discard(finished)
            reserved.discard(workspace_id)

        task.add_done_callback(done)
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @router.post("/api/sessions/{session_id}/abandon")
    async def abandon_interruption(session_id: str) -> dict[str, Any]:
        record = await get_session(session_id)
        workspace_id = record["workspace_id"]
        if workspace_id in reserved or agent_runtime.workspace_busy(workspace_id):
            raise HTTPException(status_code=409, detail="Workspace is busy")
        reserved.add(workspace_id)
        try:
            return asdict(await agent_runtime.abandon_interruption(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            reserved.discard(workspace_id)

    @router.post("/api/sessions/{session_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(session_id: str, body: RunBody, request: Request) -> dict[str, str]:
        # Keep custom Session identities while making transport acceptance durable.
        agent_runtime.create_session(session_id)
        record = agent_runtime.session_status(session_id)
        payload = {**body.model_dump(), "generation": body.generation.patch()}
        receipt = run_receipt(agent_runtime, request, session_id, "run", payload)
        if receipt.existing:
            return {"run_id": receipt.run_id, "session_id": session_id, "status": "accepted"}
        workspace_id = record["workspace_id"]
        if workspace_id in reserved or agent_runtime.workspace_busy(workspace_id):
            raise HTTPException(status_code=409, detail="Workspace is busy")
        if record["status"] == "parked":
            raise HTTPException(status_code=409, detail="Session is parked")
        reserved.add(workspace_id)
        run_id = receipt.run_id
        try:
            await agent_runtime.admit_run(
                RunRequest(
                    body.prompt,
                    session_id,
                    run_id,
                    mode=body.mode,
                    attachment_ids=list(body.attachment_ids),
                    model=body.model,
                    generation=body.generation.patch(),
                    request_fingerprint=receipt.fingerprint,
                )
            )
            task = agent_runtime.start_admitted_run(run_id, approval_handler=broker.request)
        except (ValueError, OSError) as exc:
            reserved.discard(workspace_id)
            code = (
                409
                if any(word in str(exc) for word in ("busy", "parked", "configuration"))
                else 422
            )
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        except BaseException:
            reserved.discard(workspace_id)
            raise
        tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            tasks.discard(finished)
            reserved.discard(workspace_id)

        task.add_done_callback(done)
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        if not agent_runtime.store.get_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            return await agent_runtime.cancel_run(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        if approval_id not in record["pending_approvals"]:
            raise HTTPException(status_code=404, detail="Approval does not belong to this run")
        if not broker.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="Approval is no longer pending")
        return {"approval_id": approval_id, "approved": body.approved}

    @router.get("/api/runs/{run_id}/usage")
    async def run_usage(run_id: str, include_children: bool = True) -> dict[str, Any]:
        try:
            return build_usage_ledger(
                agent_runtime.store.usage_events(
                    run_id=run_id,
                    include_children=include_children,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
