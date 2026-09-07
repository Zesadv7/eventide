"""FastAPI surface and approval broker for Nexus Agent."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from nexus_agent.config import Settings
from nexus_agent.models import RunRequest, ToolCall
from nexus_agent.runtime import AgentRuntime

WEB_DIR = Path(__file__).with_name("web")


class RunBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class ApprovalBody(BaseModel):
    approved: bool


class ApprovalBroker:
    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def request(self, approval_id: str, _call: ToolCall, _reason: str) -> bool:
        future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = future
        try:
            return await future
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool) -> bool:
        future = self.pending.get(approval_id)
        if not future or future.done():
            return False
        future.set_result(approved)
        return True


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    agent_runtime = runtime or AgentRuntime(Settings.from_env())
    broker = ApprovalBroker()
    tasks: set[asyncio.Task[Any]] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await agent_runtime.initialize()
        yield
        for task in list(tasks):
            task.cancel()
        await agent_runtime.close()

    app = FastAPI(
        title="Nexus Agent Runtime",
        version="0.2.0",
        description="Traceable, policy-aware execution for tool-using agents.",
        lifespan=lifespan,
    )
    app.state.runtime = agent_runtime
    app.state.approvals = broker
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    async def console() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime": "nexus-agent"}

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session() -> dict[str, str]:
        return {"session_id": agent_runtime.create_session()}

    @app.post("/api/sessions/{session_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(session_id: str, body: RunBody) -> dict[str, str]:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        task = asyncio.create_task(
            agent_runtime.run(
                RunRequest(body.prompt, session_id=session_id, run_id=run_id),
                approval_handler=broker.request,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": run_id, "session_id": session_id, "status": "accepted"}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            cursor = max(after, 0)
            idle_ticks = 0
            while not await request.is_disconnected():
                events = agent_runtime.store.get_events(run_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    encoded = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {encoded}\n\n"
                record = agent_runtime.store.get_run(run_id)
                if record and record["status"] in {"completed", "failed"} and not events:
                    break
                if not events:
                    idle_ticks += 1
                    if idle_ticks % 20 == 0:
                        yield ": keepalive\n\n"
                else:
                    idle_ticks = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        record = agent_runtime.store.get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")
        if not broker.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="Approval is no longer pending")
        return {"approval_id": approval_id, "approved": body.approved}

    return app
