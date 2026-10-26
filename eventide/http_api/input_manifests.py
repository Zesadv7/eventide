"""Read-only pagination of committed adapter-input manifests."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from eventide.http_api.context import ApiContext


class InstructionsInfo(BaseModel):
    source: str
    scope: str
    loaded_characters: int
    truncated: bool
    file_bytes: int | None
    limit: int


class InputComposition(BaseModel):
    method: str
    unit: str
    total_characters: int
    components: dict[str, int]


class InputManifest(BaseModel):
    manifest_version: int
    request_id: str
    attempt_id: str
    kind: str
    model: str
    seq: int
    at: float
    system_hash: str
    messages_hash: str
    tools_hash: str
    message_count: int
    tool_count: int
    tool_names: list[str]
    tool_result_ids: list[str]
    composition: InputComposition
    instructions: InstructionsInfo | None
    instruction_hash: str | None


class InputManifestPage(BaseModel):
    run_id: str
    entries: list[InputManifest]
    next_before: int | None
    recorded_count: int
    high_water: int


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["input-diagnostics"])

    @router.get("/api/runs/{run_id}/input-manifests", response_model=InputManifestPage)
    async def manifests(
        run_id: str, before: int | None = Query(None, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        if context.runtime.store.get_run_identity(run_id) is None:
            raise HTTPException(404, "Run not found")
        events = context.runtime.store.run_events(run_id)
        configured = None
        entries = []
        for event in events:
            if before is not None and event["session_seq"] >= before:
                break
            if event["type"] == "context.configured":
                configured = event["payload"]
            if event["type"] == "model.input_manifest":
                rules = configured if event["payload"]["kind"] == "agent" else None
                entries.append({
                    **event["payload"], "seq": event["session_seq"], "at": event["ts"],
                    "instructions": rules.get("instructions") if rules else None,
                    "instruction_hash": rules.get("instruction_hash") if rules else None,
                })
        page = entries[-limit:]
        return {
            "run_id": run_id, "entries": page,
            "next_before": page[0]["seq"] if len(entries) > limit else None,
            "recorded_count": len(entries),
            "high_water": events[-1]["session_seq"] if events else 0,
        }

    return router
