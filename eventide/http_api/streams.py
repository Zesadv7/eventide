"""Streams HTTP routes."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from eventide.http_api.context import ApiContext


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["streams"])
    agent_runtime = context.runtime

    @router.get("/api/runs/{run_id}/export")
    async def export_run(run_id: str) -> StreamingResponse:
        if not agent_runtime.store.get_run_identity(run_id):
            raise HTTPException(status_code=404, detail="Run not found")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:96] or "run"

        async def stream() -> AsyncIterator[str]:
            cursor = 0
            while True:
                events = agent_runtime.store.run_events(run_id, after=cursor, limit=100)
                if not events:
                    break
                for event in events:
                    cursor = event["session_seq"]
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                await asyncio.sleep(0)

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="eventide-{safe_id}.jsonl"'},
        )

    @router.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        after: int = 0,
        include_preview: bool = False,
    ) -> StreamingResponse:
        if not agent_runtime.store.get_run_identity(run_id):
            raise HTTPException(status_code=404, detail="Run not found")

        async def stream() -> AsyncIterator[str]:
            cursor = max(after, 0)
            idle_ticks = 0
            preview_revision = 0
            preview_active = False
            while not await request.is_disconnected():
                events = agent_runtime.store.get_events(run_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    encoded = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {encoded}\n\n"
                if include_preview:
                    preview = agent_runtime.previews.get(run_id)
                    if preview and preview["revision"] != preview_revision:
                        preview_revision = preview["revision"]
                        preview_active = True
                        encoded = json.dumps(preview, ensure_ascii=False)
                        yield f"event: model.preview\ndata: {encoded}\n\n"
                    elif preview_active and preview is None:
                        preview_active = False
                        cleared = {
                            "type": "model.preview",
                            "run_id": run_id,
                            "active": False,
                            "ephemeral": True,
                            "text": "",
                        }
                        yield f"event: model.preview\ndata: {json.dumps(cleared)}\n\n"
                if agent_runtime.store.run_is_terminal(run_id) and not events:
                    break
                if not events:
                    idle_ticks += 1
                    if idle_ticks % 20 == 0:
                        yield ": keepalive\n\n"
                else:
                    idle_ticks = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
