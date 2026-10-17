"""Runs HTTP routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.run_mutations import mutate_run_action
from eventide.http_api.schemas import AbandonBody, ApprovalBody, RunBody
from eventide.http_control import (
    MUTATION_OUTCOME_HEADER,
    check_mutation_origin,
    mutation_key,
    run_receipt,
)
from eventide.models import RunRequest
from eventide.scheduler import QueueFull
from eventide.usage import build_usage_ledger


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["runs"])
    agent_runtime = context.runtime
    get_session = context.get_session
    broker = context.approvals

    def admission_error(run_id: str, exc: Exception) -> HTTPException | None:
        # Admission can commit before a cache write or dispatch registration fails.
        # The durable identity is still the receipt; never describe it as rejected.
        if agent_runtime.store.get_run_identity(run_id) is not None:
            return None
        if isinstance(exc, QueueFull):
            code = 429
        elif isinstance(exc, (ValueError, OSError)):
            code = 409 if any(word in str(exc) for word in (
                "busy", "parked", "configuration",
            )) else 422
        else:
            return HTTPException(
                503, "Run admission outcome is unknown; retain the original request",
            )
        return HTTPException(code, str(exc), headers={MUTATION_OUTCOME_HEADER: "rejected"})

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
        require_local_request(request)
        mutation_key(request)
        # Continue validation is part of admission; a rejected request returns 409,
        # without creating a run that could hide the parked source run.
        await get_session(session_id)
        receipt = run_receipt(agent_runtime, request, session_id, "continue", {})
        if receipt.existing:
            return {"run_id": receipt.run_id, "session_id": session_id, "status": "accepted"}
        run_id = receipt.run_id
        try:
            await agent_runtime.admit_continue(
                session_id,
                run_id,
                **({"request_fingerprint": receipt.fingerprint} if receipt.fingerprint else {}),
            )
            agent_runtime.start_admitted_run(run_id, approval_handler=broker.request)
        except Exception as exc:
            error = admission_error(run_id, exc)
            if error is not None:
                # Continue preconditions have always been a conflict, not an
                # ordinary new-Run parameter validation error.
                if error.status_code == 422:
                    error.status_code = 409
                raise error from exc
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @router.post("/api/sessions/{session_id}/abandon")
    async def abandon_interruption(
        session_id: str, body: AbandonBody, request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        async def execute() -> dict[str, Any]:
            record = await get_session(session_id)
            if agent_runtime.session_busy(session_id) or (
                agent_runtime.session_execution_target(session_id) == "source"
                and agent_runtime.workspace_busy(record["workspace_id"])
            ):
                raise HTTPException(409, "Session or Workspace is busy")
            previous = agent_runtime.store.latest_run(session_id)
            if not previous or previous["id"] != body.expected_run_id:
                raise HTTPException(409, "Interrupted Run changed; refresh before abandoning")
            if previous["status"] != "interrupted" or agent_runtime.is_control_session(session_id):
                raise HTTPException(409, "Only an interrupted user Session can be abandoned")
            return asdict(await agent_runtime.abandon_interruption(
                session_id, expected_run_id=body.expected_run_id,
            ))
        return await mutate_run_action(
            agent_runtime, request, "run_abandon", {"session_id": session_id, **body.model_dump()},
            execute,
        )

    @router.post("/api/sessions/{session_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(session_id: str, body: RunBody, request: Request) -> dict[str, str]:
        require_local_request(request)
        mutation_key(request)
        # Keep custom Session identities while making transport acceptance durable.
        agent_runtime.create_session(session_id)
        record = agent_runtime.session_status(session_id)
        payload = {**body.model_dump(), "generation": body.generation.patch()}
        if body.execution_profile == "local" and body.sandbox_image is None:
            # Preserve outstanding legacy local request identities across this upgrade.
            payload.pop("execution_profile")
            payload.pop("sandbox_image")
        receipt = run_receipt(agent_runtime, request, session_id, "run", payload)
        if receipt.existing:
            return {"run_id": receipt.run_id, "session_id": session_id, "status": "accepted"}
        if record["status"] == "parked":
            raise HTTPException(
                409, "Session is parked", headers={MUTATION_OUTCOME_HEADER: "rejected"},
            )
        run_id = receipt.run_id
        try:
            await agent_runtime.admit_run(
                RunRequest(
                    body.prompt,
                    session_id,
                    run_id,
                    mode=body.mode,
                    execution_target=body.execution_target,
                    execution_profile=body.execution_profile,
                    sandbox_image=body.sandbox_image,
                    attachment_ids=list(body.attachment_ids),
                    model=body.model,
                    generation=body.generation.patch(),
                    request_fingerprint=receipt.fingerprint,
                    selected_skills=body.selected_skills,
                )
            )
            agent_runtime.start_admitted_run(run_id, approval_handler=broker.request)
        except Exception as exc:
            error = admission_error(run_id, exc)
            if error is not None:
                raise error from exc
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        return {**record, "execution": agent_runtime.run_execution(run_id)}

    @router.get(
        "/api/runs/{run_id}/sandbox-diff", response_class=Response,
        responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}}},
    )
    async def sandbox_diff(run_id: str, request: Request) -> PlainTextResponse:
        require_local_request(request)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site sandbox export is not allowed")
        if request.headers.get("origin") is not None:
            check_mutation_origin(request, require_origin=True)
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(404, "Run not found")
        binding = agent_runtime._execution_binding(run_id)
        if not binding or binding["kind"] != "sandbox":
            raise HTTPException(422, "Run has no sandbox candidate")
        if agent_runtime.session_busy(record["session_id"]):
            raise HTTPException(409, "Sandbox is busy; wait for execution to stop")
        if any(e["type"] == "tool.outcome_unknown"
               for e in agent_runtime.store.session_events(record["session_id"])):
            raise HTTPException(409, "Sandbox cleanup is unconfirmed; export is blocked")
        try:
            patch = agent_runtime.execution_sandboxes.diff(binding)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, "Sandbox output is unsafe or exceeds export limits") from exc
        return PlainTextResponse(patch, headers={
            "Content-Disposition": 'attachment; filename="candidate.patch"',
            "Cache-Control": "no-store",
        })

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        async def execute() -> dict[str, Any]:
            if not agent_runtime.store.get_run(run_id):
                raise HTTPException(404, "Run not found")
            try:
                return await agent_runtime.cancel_run(run_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return await mutate_run_action(
            agent_runtime, request, "run_cancel", {"run_id": run_id}, execute,
        )

    @router.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(
        run_id: str, approval_id: str, body: ApprovalBody, request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)
        async def execute() -> dict[str, Any]:
            record = agent_runtime.store.get_run(run_id)
            if not record:
                raise HTTPException(404, "Run not found")
            if approval_id not in record["pending_approvals"]:
                raise HTTPException(404, "Approval does not belong to this run")
            if not broker.resolve(approval_id, body.approved):
                raise HTTPException(404, "Approval is no longer pending")
            return {"approval_id": approval_id, "approved": body.approved}
        return await mutate_run_action(
            agent_runtime, request, "run_approval",
            {"run_id": run_id, "approval_id": approval_id, **body.model_dump()}, execute,
        )

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
