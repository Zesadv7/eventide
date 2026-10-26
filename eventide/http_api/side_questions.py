"""Side-question routes on the trusted local path."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eventide import side_questions
from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_api.run_mutations import mutate_run_action
from eventide.http_control import mutation_key


class SideQuestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=side_questions.SIDE_QUESTION_LIMIT)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=128)


class SideQuestionReturnBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter()
    agent_runtime = context.runtime

    def _view(side_question_id: str) -> dict[str, Any]:
        try:
            side_run_id = side_questions.side_run_id_for(side_question_id)
        except ValueError as exc:
            raise HTTPException(404, "Side question not found") from exc
        record = side_questions.created_record(agent_runtime.store, side_run_id)
        if record is None:
            raise HTTPException(404, "Side question not found")
        return side_questions.side_question_view(agent_runtime.store, record)

    @router.post("/api/sessions/{session_id}/side-questions", status_code=202)
    async def create_side_question(
        session_id: str, body: SideQuestionBody, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            if not agent_runtime.store.get_session(session_id):
                raise HTTPException(404, "Session not found")
            source_run_id = body.source_run_id
            if source_run_id is None:
                runs = agent_runtime.session_runs(session_id, limit=1)
                if not runs:
                    raise HTTPException(409, "Session has no run to ask about")
                # list_runs projects the runs table directly: the identity key is "id".
                source_run_id = runs[0]["id"]
            else:
                identity = agent_runtime.store.get_run_identity(source_run_id)
                if not identity or identity["session_id"] != session_id:
                    raise HTTPException(404, "Source run not found in this session")
            try:
                return await agent_runtime.create_side_question(source_run_id, body.question)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        return await mutate_run_action(
            agent_runtime, request, "side_question_create",
            {"session_id": session_id, **body.model_dump()}, execute,
        )

    @router.get("/api/runs/{run_id}/side-questions")
    async def list_side_questions(run_id: str) -> dict[str, Any]:
        identity = agent_runtime.store.get_run_identity(run_id)
        if not identity:
            raise HTTPException(404, "Run not found")
        # Creation facts are session-scoped: a terminal Run cannot take new
        # run-scoped facts, but users may still ask side questions about it.
        records = [
            event["payload"]
            for event in agent_runtime.store.session_events(identity["session_id"])
            if event["type"] == side_questions.SIDE_QUESTION_EVENT
            and event["payload"].get("source_run_id") == run_id
        ]
        views = [
            side_questions.side_question_view(agent_runtime.store, record)
            for record in records
        ]
        return {"run_id": run_id, "side_questions": views}

    @router.get("/api/side-questions/{side_question_id}")
    async def get_side_question(side_question_id: str) -> dict[str, Any]:
        return _view(side_question_id)

    @router.post("/api/side-questions/{side_question_id}/return")
    async def return_side_question(
        side_question_id: str, body: SideQuestionReturnBody, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        mutation_key(request)

        async def execute() -> dict[str, Any]:
            try:
                return side_questions.return_side_question(
                    agent_runtime, side_question_id, body.answer_fingerprint
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        return await mutate_run_action(
            agent_runtime, request, "side_question_return",
            {"side_question_id": side_question_id, **body.model_dump()}, execute,
        )

    return router
