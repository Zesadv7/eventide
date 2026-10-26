"""Compare saved summaries with their bounded, reconstructable source evidence."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from eventide.context_builder import SUMMARY_CLAIM, summary_history
from eventide.context_summary import is_structured_summary, parse_structured_summary
from eventide.http_api.context import ApiContext
from eventide.projections import TERMINALS
from eventide.workspace import digest

SUMMARY_DISPLAY_LIMIT = 24000


class SummaryVersion(BaseModel):
    id: int
    covered_seq: int
    source_digest: str
    policy_version: int
    provider: str
    model: str
    summary_characters: int


class SummaryPage(BaseModel):
    session_id: str
    versions: list[SummaryVersion]
    next_before: int | None


class SummaryDetail(SummaryVersion):
    summary_hash: str
    summary: str
    summary_truncated: bool
    structured: dict[str, Any] | None
    source_verified: bool
    source_problem: str | None
    input_verification: str
    history_hash: str | None
    history_message_count: int | None
    citations_problem: str | None


class SummaryEvidence(BaseModel):
    checkpoint_id: int
    source_digest: str
    history_hash: str
    input_verification: str
    message_index: int
    offset: int
    content: str
    total_characters: int
    next_offset: int | None


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["context-summaries"])
    store = context.runtime.store

    def source(record: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None, str]:
        events = store.session_events(record["session_id"])
        prefix = [event for event in events if event["session_seq"] <= record["covered_seq"]]
        if (not prefix or prefix[-1]["session_seq"] != record["covered_seq"]
                or prefix[-1]["type"] not in TERMINALS or prefix[-1]["partial"]
                or digest(prefix) != record["source_digest"]):
            return None, "历史覆盖范围或内容指纹不匹配，不能核对引用", "unavailable"
        if record["policy_version"] != 1:
            return None, "不支持该摘要投影版本，保留原摘要", "unavailable"
        try:
            history = summary_history(prefix)
        except ValueError as exc:
            return None, str(exc), "unavailable"
        claim = next((event["payload"] for event in events
                      if event["type"] == SUMMARY_CLAIM
                      and event["payload"]["covered_seq"] == record["covered_seq"]
                      and event["payload"]["provider"] == record["provider"]
                      and event["payload"]["model"] == record["model"]), None)
        verification = (
            "claim_missing" if claim is None else
            "matched" if claim["prefix_hash"] == digest(history) else "projection_changed"
        )
        return history, None, verification

    def get_record(session_id: str, checkpoint_id: int) -> dict[str, Any]:
        record = store.checkpoint_record(session_id, checkpoint_id)
        if record is None:
            raise HTTPException(404, "Summary not found in this session")
        return record

    @router.get("/api/sessions/{session_id}/context-summaries", response_model=SummaryPage)
    async def versions(
        session_id: str, before: int | None = Query(None, ge=1),
        limit: int = Query(20, ge=1, le=50),
    ) -> dict[str, Any]:
        if store.get_session(session_id) is None:
            raise HTTPException(404, "Session not found")
        rows = store.checkpoint_page(session_id, before=before, limit=limit + 1)
        return {"session_id": session_id, "versions": rows[:limit],
                "next_before": rows[limit - 1]["id"] if len(rows) > limit else None}

    @router.get(
        "/api/sessions/{session_id}/context-summaries/{checkpoint_id}",
        response_model=SummaryDetail,
    )
    async def detail(session_id: str, checkpoint_id: int) -> dict[str, Any]:
        record = get_record(session_id, checkpoint_id)
        history, problem, verification = source(record)
        text = record["summary"]
        parsed, citation_problem = None, None
        if len(text) <= SUMMARY_DISPLAY_LIMIT and is_structured_summary(text):
            if history is not None:
                parsed, citation_problem = parse_structured_summary(text, history)
            else:
                citation_problem = "原始证据不可核对，保留摘要文本"
        return {
            **record, "summary_characters": len(text), "summary_hash": digest(text),
            "summary": text[:SUMMARY_DISPLAY_LIMIT],
            "summary_truncated": len(text) > SUMMARY_DISPLAY_LIMIT,
            "structured": parsed, "source_verified": history is not None,
            "source_problem": problem, "input_verification": verification,
            "history_hash": digest(history) if history is not None else None,
            "history_message_count": len(history) if history is not None else None,
            "citations_problem": citation_problem,
        }

    @router.get(
        "/api/sessions/{session_id}/context-summaries/{checkpoint_id}/messages/{message_index}",
        response_model=SummaryEvidence,
    )
    async def message(
        session_id: str, checkpoint_id: int, message_index: int,
        offset: int = Query(0, ge=0), limit: int = Query(8000, ge=1, le=12000),
    ) -> dict[str, Any]:
        record = get_record(session_id, checkpoint_id)
        history, problem, verification = source(record)
        if history is None:
            raise HTTPException(409, problem)
        if not 0 <= message_index < len(history):
            raise HTTPException(404, "Summary source message not found")
        text = json.dumps(history[message_index], ensure_ascii=False, indent=2)
        if offset > len(text):
            raise HTTPException(422, "Offset exceeds message length")
        end = min(len(text), offset + limit)
        return {"checkpoint_id": checkpoint_id, "source_digest": record["source_digest"],
                "history_hash": digest(history), "input_verification": verification,
                "message_index": message_index, "offset": offset, "content": text[offset:end],
                "total_characters": len(text), "next_offset": end if end < len(text) else None}

    return router
