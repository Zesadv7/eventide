"""In-run supplement routes on the trusted local path."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide import interactions
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.run_mutations import mutate_run_action
from eventide.http_control import mutation_key


class SupplementBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body: str = Field(min_length=1, max_length=interactions.SUPPLEMENT_BODY_LIMIT)
    body_version: int = Field(default=1, ge=1, le=9)


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    choice: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=interactions.ANSWER_TEXT_LIMIT)


class QuestionAnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers: list[QuestionAnswer] = Field(min_length=1, max_length=3)
    question_revision: int = Field(default=1, ge=1, le=9)


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter()
    agent_runtime = context.runtime

    @router.post("/api/runs/{run_id}/supplements")
    async def add_supplement(
        run_id: str, body: SupplementBody, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            run = agent_runtime.store.get_run(run_id)
            if not run:
                raise HTTPException(404, "Run not found")
            try:
                normalized = interactions.normalize_supplement_body(body.body)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            status = interactions.supplement_status(run["status"])
            supplement_id = f"sup_{uuid.uuid4().hex[:16]}"
            agent_runtime.store.record_supplement(run_id, {
                "supplement_id": supplement_id,
                "run_id": run_id,
                "session_id": run["session_id"],
                "body": normalized,
                "body_version": body.body_version,
                "status": status,
            })
            return {
                "supplement_id": supplement_id,
                "run_id": run_id,
                "status": status,
                "consumed": False,
            }

        return await mutate_run_action(
            agent_runtime, request, "run_supplement",
            {"run_id": run_id, **body.model_dump()}, execute,
        )

    @router.get("/api/runs/{run_id}/supplements")
    async def list_supplements(run_id: str) -> dict[str, Any]:
        if not agent_runtime.store.get_run_identity(run_id):
            raise HTTPException(404, "Run not found")
        supplements = agent_runtime.store.interaction_supplements(run_id)
        return {
            "run_id": run_id,
            "supplements": supplements,
            "pending": sum(
                1 for s in supplements if s["status"] == "pending"
            ),
        }

    @router.get("/api/runs/{run_id}/questions")
    async def list_questions(run_id: str) -> dict[str, Any]:
        if not agent_runtime.store.get_run_identity(run_id):
            raise HTTPException(404, "Run not found")
        questions = agent_runtime.store.interaction_questions(run_id)
        for question in questions:
            if question["status"] == "waiting" and not agent_runtime.has_question_waiter(
                question["question_id"]
            ):
                question["status"] = "expired"
        return {
            "run_id": run_id,
            "questions": questions,
            "waiting": sum(1 for q in questions if q["status"] == "waiting"),
        }

    @router.post("/api/runs/{run_id}/questions/{question_id}/answers")
    async def answer_question(
        run_id: str, question_id: str, body: QuestionAnswerBody, request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            run = agent_runtime.store.get_run(run_id)
            if not run:
                raise HTTPException(404, "Run not found")
            record = next(
                (item for item in agent_runtime.store.interaction_questions(run_id)
                 if item["question_id"] == question_id),
                None,
            )
            if record is None:
                raise HTTPException(404, "Question does not belong to this run")
            if body.question_revision != record.get("revision"):
                raise HTTPException(
                    409, f"Question revision mismatch; expected {record.get('revision')}"
                )
            try:
                normalized = interactions.normalize_answers(
                    record, [answer.model_dump() for answer in body.answers],
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            result = agent_runtime.store.answer_question(
                run_id, question_id,
                answers=agent_runtime._semantic(normalized),
                digest=interactions.answer_digest(normalized),
                answer_version=body.question_revision,
                waiter_present=agent_runtime.has_question_waiter(question_id),
            )
            outcome = result.get("outcome")
            if outcome == "unknown_question":
                raise HTTPException(404, "Question does not belong to this run")
            if outcome == "stale_revision":
                raise HTTPException(409, "Question revision changed")
            if outcome == "conflict":
                raise HTTPException(
                    409, "Question already answered with different content",
                )
            if outcome == "closed":
                raise HTTPException(
                    409, f"Question is closed ({result.get('reason')}); answer kept out"
                )
            if outcome == "not_waiting":
                raise HTTPException(
                    409,
                    "Run is recorded as active but no ask_user waiter is alive in "
                    "this process (service restarted or run halted); the answer is "
                    "refused instead of parked. Recover or stop the run first.",
                )
            # A repeated identical receipt also re-arms the wake in case the
            # first one was lost between the answer fact and the waiter.
            if result.get("status") == "delivered":
                agent_runtime.resolve_question(question_id)
            return result

        receipt = await mutate_run_action(
            agent_runtime, request, "run_question_answer",
            {"run_id": run_id, "question_id": question_id, **body.model_dump()}, execute,
        )
        # Cached transport receipts bypass execute(); re-arm the same live waiter too.
        if receipt["status"] == "delivered":
            agent_runtime.resolve_question(question_id)
        return receipt

    return router
