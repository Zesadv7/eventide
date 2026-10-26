"""Structured ask_user questions: durable facts, waiting runs, idempotent answers.

Answers are only tool results of their own run — never approvals, never a
resume of a finished run, and never shared across sessions.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from eventide import interactions
from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.run_policy import filter_tool_catalog
from tests.test_runtime import settings_for


def wait_for_status(client: TestClient, run_id: str, statuses: set[str], timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    record: dict = {}
    while time.monotonic() < deadline:
        record = client.get(f"/api/runs/{run_id}").json()
        if record.get("status") in statuses:
            return record
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {statuses}; last={record}")


def wait_for_question(
    client: TestClient, run_id: str, timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listing = client.get(f"/api/runs/{run_id}/questions").json()
        if listing["waiting"]:
            return listing["questions"][0]
        time.sleep(0.01)
    raise AssertionError("no waiting question appeared")


def _client(runtime: RuntimeHost) -> TestClient:
    return TestClient(create_app(runtime))


def _start_run(client: TestClient, prompt: str = "investigate") -> tuple[str, str]:
    session = client.post(
        "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
    ).json()["session_id"]
    accepted = client.post(
        f"/api/sessions/{session}/runs",
        json={"prompt": prompt},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert accepted.status_code == 202, accepted.text
    return session, accepted.json()["run_id"]


ASK_CALL = {
    "id": "q1", "name": "ask_user",
    "arguments": {"questions": [{
        "text": "Which database?",
        "choices": ["sqlite", "postgres"],
        "allow_free_text": True,
    }]},
}


def _answer_body(choice: str = "sqlite", revision: int = 1) -> dict:
    return {
        "answers": [{"choice": choice, "text": None}],
        "question_revision": revision,
    }


def _answer_url(run_id: str, question_id: str) -> str:
    return f"/api/runs/{run_id}/questions/{question_id}/answers"


def test_question_waits_answer_resumes_and_is_not_an_approval(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([
            {"tool_calls": [dict(ASK_CALL)]},
            {"text": "done with the chosen database"},
        ]),
    )
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        assert question["status"] == "waiting"
        assert question["revision"] == interactions.QUESTION_REVISION
        assert question["call_id"] == "q1"
        assert runtime.dispatch.scheduler.counts()["active_models"] == 0
        assert runtime.store.get_run_summary(run_id)["status"] == "waiting_for_user"
        record = wait_for_status(client, run_id, {"waiting_for_user"})
        assert record["pending_approvals"] == {}
        assert len(runtime.provider.requests) == 1

        answered = client.post(
            _answer_url(run_id, question["question_id"]),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert answered.status_code == 200, answered.text
        receipt = answered.json()
        assert receipt == {
            "question_id": question["question_id"],
            "run_id": run_id,
            "status": "delivered",
            "duplicate": False,
        }
        wait_for_status(client, run_id, {"completed"})
        assert len(runtime.provider.requests) == 2
        tool_result = next(
            event["payload"]
            for event in runtime.store.run_events(run_id)
            if event["type"] == "tool.completed"
            and event["payload"]["name"] == "ask_user"
        )
        assert tool_result["content"] == '{"answers": [{"choice": "sqlite", "text": null}]}'
        approval_events = [
            event for event in runtime.store.run_events(run_id)
            if event["type"].startswith("approval.")
        ]
        assert approval_events == []
        listing = client.get(f"/api/runs/{run_id}/questions").json()
        assert listing["waiting"] == 0
        assert listing["questions"][0]["status"] == "answered"
        assert listing["questions"][0]["answers"] == [{"choice": "sqlite", "text": None}]


def test_unanswered_question_never_resumes_and_cancel_closes_it(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([{"tool_calls": [dict(ASK_CALL)]}, {"text": "never sent"}]),
    )
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        wait_for_status(client, run_id, {"waiting_for_user"})
        time.sleep(0.3)
        assert len(runtime.provider.requests) == 1

        cancelled = client.post(
            f"/api/runs/{run_id}/cancel", headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert cancelled.status_code == 200, cancelled.text
        wait_for_status(client, run_id, {"interrupted"})
        assert len(runtime.provider.requests) == 1
        listing = client.get(f"/api/runs/{run_id}/questions").json()
        assert listing["questions"][0]["status"] == "closed"
        assert listing["questions"][0]["closed_reason"] == "cancelled"

        late = client.post(
            _answer_url(run_id, question["question_id"]),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert late.status_code == 409
        assert "closed" in late.json()["detail"]


def test_answer_idempotent_replay_and_content_conflict(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([{"tool_calls": [dict(ASK_CALL)]}, {"text": "done"}]),
    )
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        url = _answer_url(run_id, question["question_id"])
        first = client.post(
            url, json=_answer_body(), headers={"Idempotency-Key": "dup-key-1"},
        )
        assert first.status_code == 200
        assert first.json()["duplicate"] is False

        replay = client.post(url, json=_answer_body(), headers={"Idempotency-Key": "dup-key-1"})
        assert replay.status_code == 200
        assert replay.json() == first.json()

        same_content = client.post(
            url, json=_answer_body(), headers={"Idempotency-Key": "dup-key-2"},
        )
        assert same_content.status_code == 200
        assert same_content.json()["duplicate"] is True
        assert same_content.json()["status"] == "delivered"

        conflict = client.post(
            url, json=_answer_body(choice="postgres"),
            headers={"Idempotency-Key": "dup-key-3"},
        )
        assert conflict.status_code == 409
        assert "different content" in conflict.json()["detail"]


def test_answer_revision_and_run_binding_are_enforced(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([
            {"tool_calls": [dict(ASK_CALL)]},
            {"text": "first done"},
            {"tool_calls": [{**ASK_CALL, "id": "q2"}]},
            {"text": "second done"},
        ]),
    )
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        stale = client.post(
            _answer_url(run_id, question["question_id"]),
            json=_answer_body(revision=2),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert stale.status_code == 409
        malformed = client.post(
            _answer_url(run_id, question["question_id"]),
            json={"answers": [{"choice": "nosuch", "text": None}]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert malformed.status_code == 422
        assert client.get("/api/runs/run_missing/questions").status_code == 404
        assert client.post(
            _answer_url(run_id, "q_missing"),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).status_code == 404

        answered = client.post(
            _answer_url(run_id, question["question_id"]),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert answered.status_code == 200
        wait_for_status(client, run_id, {"completed"})


def test_questions_do_not_leak_across_runs(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([
            {"tool_calls": [dict(ASK_CALL)]},
            {"text": "first done"},
            {"tool_calls": [{**ASK_CALL, "id": "q2"}]},
            {"text": "second done"},
        ]),
    )
    with _client(runtime) as client:
        # One workspace runs one root at a time, so the two askers go in order.
        _, run_a = _start_run(client, prompt="task a")
        question_a = wait_for_question(client, run_a)
        answered = client.post(
            _answer_url(run_a, question_a["question_id"]),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert answered.status_code == 200
        wait_for_status(client, run_a, {"completed"})

        _, run_b = _start_run(client, prompt="task b")
        question_b = wait_for_question(client, run_b)
        assert question_b["question_id"] != question_a["question_id"]

        cross = client.post(
            _answer_url(run_b, question_a["question_id"]),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert cross.status_code == 404
        assert client.get(f"/api/runs/{run_b}/questions").json()["waiting"] == 1
        assert len(runtime.provider.requests) == 3

        cancelled = client.post(
            f"/api/runs/{run_b}/cancel", headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert cancelled.status_code == 200
        wait_for_status(client, run_b, {"interrupted"})
        assert client.get(f"/api/runs/{run_b}/questions").json()[
            "questions"
        ][0]["status"] == "closed"


def test_attention_lists_waiting_questions_only(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([
            {"tool_calls": [dict(ASK_CALL)]},
            {"text": "done"},
        ]),
    )
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        snapshot = client.get("/api/attention").json()
        assert snapshot["counts"]["questions"] == 1
        item = snapshot["questions"][0]
        assert item["dedup_key"] == f"question:{question['question_id']}"
        assert item["actionable"] is True
        assert item["action"]["url"] == _answer_url(run_id, question["question_id"])
        assert item["questions"][0]["text"] == "Which database?"

        answered = client.post(
            _answer_url(run_id, question["question_id"]),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert answered.status_code == 200
        wait_for_status(client, run_id, {"completed"})
        refreshed = client.get("/api/attention").json()
        assert refreshed["counts"]["questions"] == 0
        assert refreshed["questions"] == []


def test_terminal_run_records_late_answer_without_resume(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]),
    )
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        wait_for_status(client, run_id, {"completed"})
        # A crashed process leaves the opened fact with no close and no answer;
        # recovery then marks the run terminal. Project that leftover honestly
        # as expired, and keep a late answer out of the finished run.
        session_id = runtime.store.get_run_identity(run_id)["session_id"]
        runtime.store.append_fact(
            session_id, interactions.QUESTION_OPENED_EVENT,
            {
                "question_id": "q_crash", "run_id": run_id, "call_id": "crashed_call",
                "session_id": session_id,
                "revision": interactions.QUESTION_REVISION,
                "questions": interactions.normalize_questions(
                    [{"text": "Survived a crash?"}]
                ),
            },
        )
        listing = client.get(f"/api/runs/{run_id}/questions").json()
        assert listing["waiting"] == 0
        assert listing["questions"][0]["status"] == "expired"

        late = client.post(
            _answer_url(run_id, "q_crash"),
            json={
                "answers": [{"choice": None, "text": "yes, recovered"}],
                "question_revision": 1,
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert late.status_code == 200
        assert late.json()["status"] == "late"
        assert client.get(f"/api/runs/{run_id}/questions").json()[
            "questions"
        ][0]["status"] == "late"
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"


async def test_reopened_host_keeps_closed_question_identity(isolated_workspace):
    from eventide.models import RunRequest

    settings = settings_for(isolated_workspace)
    host = RuntimeHost(
        settings,
        ScriptedProvider([{"tool_calls": [dict(ASK_CALL)]}, {"text": "unused"}]),
    )
    try:
        session = host.create_session()
        task = asyncio.create_task(host.run(RunRequest("pick one", session)))
        question_id = ""
        run_id = ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            opened = [
                event["payload"] for event in host.store.session_events(session)
                if event["type"] == interactions.QUESTION_OPENED_EVENT
            ]
            if opened:
                question_id, run_id = opened[-1]["question_id"], opened[-1]["run_id"]
                break
            await asyncio.sleep(0.01)
        assert question_id
        await host.cancel_run(run_id)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([{"text": "fresh"}]))
    try:
        listing = reopened.store.interaction_questions(run_id)
        assert [item["question_id"] for item in listing] == [question_id]
        assert listing[0]["status"] == "closed"
        assert listing[0]["closed_reason"] == "cancelled"
        assert listing[0]["questions"][0]["text"] == "Which database?"
    finally:
        await reopened.close()

    runtime = RuntimeHost(settings, ScriptedProvider([{"text": "third"}]))
    with _client(runtime) as client:
        late = client.post(
            _answer_url(run_id, question_id),
            json=_answer_body(),
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert late.status_code == 409
        assert "closed" in late.json()["detail"]


async def test_host_level_answer_wakes_only_its_own_run(isolated_workspace):
    from eventide.models import RunRequest

    provider = ScriptedProvider([
        {"tool_calls": [dict(ASK_CALL)]},
        {"text": "resumed"},
    ])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        session = host.create_session()
        task = asyncio.create_task(host.run(RunRequest("pick one", session)))
        question_id = ""
        run_id = ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            opened = [
                event["payload"] for event in host.store.session_events(session)
                if event["type"] == interactions.QUESTION_OPENED_EVENT
            ]
            if opened:
                question_id, run_id = opened[-1]["question_id"], opened[-1]["run_id"]
                break
            await asyncio.sleep(0.01)
        assert question_id
        normalized = interactions.normalize_answers(
            {"questions": ASK_CALL["arguments"]["questions"]},
            [{"choice": "postgres", "text": None}],
        )
        receipt = host.store.answer_question(
            run_id, question_id, answers=normalized,
            digest=interactions.answer_digest(normalized),
            answer_version=1, waiter_present=True,
        )
        assert receipt["status"] == "delivered"
        assert host.resolve_question(question_id) is True
        result = await asyncio.wait_for(task, 15)
        assert result.status == "completed"
        assert host.resolve_question(question_id) is False
    finally:
        await host.close()


def test_normalize_and_catalog_boundaries():
    questions = [{"text": "Q", "choices": ["a", "b"]}]
    normalized = interactions.normalize_questions(questions)
    assert normalized[0]["allow_free_text"] is False
    for bad in (
        [], [{"text": ""}], [{"text": "x" * 501}], [{"text": "Q", "choices": ["a"] * 7}],
        [{"text": "Q", "allow_free_text": False}],
        [{"text": "Q", "choices": ["same", "same"]}],
        "nope",
    ):
        try:
            interactions.normalize_questions(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected rejection: {bad!r}")
    opened = {"questions": normalized}
    assert interactions.normalize_answers(opened, [{"choice": "a", "text": None}])
    for bad in (
        [], [{"choice": "nope", "text": None}], [{"choice": None, "text": None}],
        [{"choice": "a", "text": "x" * 4001}], [{"choice": None, "text": 5}],
    ):
        try:
            interactions.normalize_answers(opened, bad)
        except ValueError:
            continue
        raise AssertionError(f"expected rejection: {bad!r}")
    assert filter_tool_catalog([dict(interactions.ASK_USER_TOOL)], "plan")
    assert filter_tool_catalog([dict(interactions.ASK_USER_TOOL)], "auto")
    digest_a = interactions.answer_digest([{"choice": "a", "text": None}])
    assert digest_a == interactions.answer_digest([{"choice": "a", "text": None}])
    assert digest_a != interactions.answer_digest([{"choice": "b", "text": None}])


def test_durable_user_stop_precedes_answer_acceptance(isolated_workspace):
    runtime = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"tool_calls": [dict(ASK_CALL)]}, {"text": "must not run"},
    ]))
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        # Capture the window after stop intent is committed, before task cancellation settles.
        runtime._record_user_cancel(run_id)
        response = client.post(
            _answer_url(run_id, question["question_id"]), json=_answer_body(),
            headers={"Idempotency-Key": "after-stop"},
        )
        assert response.status_code == 409
        assert not any(event["type"] == interactions.QUESTION_ANSWERED_EVENT
                       for event in runtime.store.session_events(question["session_id"]))
        assert client.get("/api/attention").json()["questions"] == []
        client.post(f"/api/runs/{run_id}/cancel", headers={"Idempotency-Key": "stop"})
        assert len(runtime.provider.requests) == 1


def test_cached_answer_receipt_can_rearm_the_same_waiter(isolated_workspace, monkeypatch):
    runtime = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"tool_calls": [dict(ASK_CALL)]}, {"text": "done"},
    ]))
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        original = runtime.resolve_question
        monkeypatch.setattr(runtime, "resolve_question", lambda _: False)
        url = _answer_url(run_id, question["question_id"])
        response = client.post(url, json=_answer_body(), headers={"Idempotency-Key": "receipt"})
        assert response.status_code == 200
        assert len(runtime.provider.requests) == 1
        monkeypatch.setattr(runtime, "resolve_question", original)
        replay = client.post(url, json=_answer_body(), headers={"Idempotency-Key": "receipt"})
        assert replay.json() == response.json()
        wait_for_status(client, run_id, {"completed"})
        assert len(runtime.provider.requests) == 2


def test_plan_can_ask_and_answers_are_redacted_before_persistence(isolated_workspace):
    secret = "question-provider-secret-456789"
    runtime = RuntimeHost(settings_for(isolated_workspace, api_key=secret), ScriptedProvider([
        {"tool_calls": [dict(ASK_CALL)]}, {"text": "done"},
    ]))
    with _client(runtime) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": "session"},
        ).json()["session_id"]
        response = client.post(
            f"/api/sessions/{session}/runs", json={"prompt": "plan", "mode": "plan"},
            headers={"Idempotency-Key": "run"},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        question = wait_for_question(client, run_id)
        assert "ask_user" in {tool["name"] for tool in runtime.provider.requests[0].tools}
        response = client.post(_answer_url(run_id, question["question_id"]),
                               json={"answers": [{"text": secret}], "question_revision": 1},
                               headers={"Idempotency-Key": "answer"})
        assert response.status_code == 200, response.text
        wait_for_status(client, run_id, {"completed"})
        import json
        assert secret not in json.dumps(runtime.store.session_events(session))
        assert secret not in json.dumps(runtime.provider.requests[-1].messages)


def test_free_text_and_unrecognized_answer_fields_are_rejected():
    opened = {"questions": interactions.normalize_questions([
        {"text": "Q", "choices": ["a"], "allow_free_text": False},
    ])}
    for answer in ({"text": "no choice"}, {"choice": "a", "hidden": "x"}, {"choice": 1}):
        with pytest.raises(ValueError):
            interactions.normalize_answers(opened, [answer])


async def test_custom_handler_call_id_remains_a_regular_argument(tmp_path):

    from eventide.executor import CURRENT_TOOL_CALL, ToolContext, ToolExecutor
    from eventide.models import ToolCall
    from eventide.policy import PolicyEngine

    seen = []
    async def custom(call_id):
        seen.append((call_id, CURRENT_TOOL_CALL.get().id))
        return "ok"
    executor = ToolExecutor({"custom": custom}, {"custom"}, tools=[{
        "name": "custom", "input_schema": {"type": "object", "properties": {
            "call_id": {"type": "string"}}, "required": ["call_id"], "additionalProperties": False},
    }])
    result = await executor.execute(ToolCall("physical", "custom", {"call_id": "user-argument"}),
                                    ToolContext(tmp_path, "run", PolicyEngine()))
    assert not result.is_error
    assert seen == [("user-argument", "physical")]
    with pytest.raises(LookupError):
        CURRENT_TOOL_CALL.get()


async def test_service_close_keeps_question_and_does_not_auto_answer(isolated_workspace):
    from eventide.models import RunRequest

    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([{"tool_calls": [dict(ASK_CALL)]}]))
    session = host.create_session()
    ready = asyncio.Event()
    class Sink:
        async def emit(self, event):
            if event["type"] == interactions.QUESTION_OPENED_EVENT:
                ready.set()
    task = asyncio.create_task(host.run(RunRequest("ask", session), sink=Sink()))
    await asyncio.wait_for(ready.wait(), 10)
    run_id = host.store.latest_run(session)["id"]
    question_id = host.store.interaction_questions(run_id)[0]["question_id"]
    await host.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not host._question_waiters
    provider = ScriptedProvider([{"text": "must not resume an unanswered question"}])
    reopened = RuntimeHost(settings, provider)
    try:
        await reopened.initialize()
        stored = reopened.store.interaction_questions(run_id)
        assert stored[0]["question_id"] == question_id
        assert stored[0]["status"] == "closed"
        assert provider.requests == []
        assert not any(event["type"] == interactions.QUESTION_ANSWERED_EVENT
                       for event in reopened.store.session_events(session))
    finally:
        await reopened.close()


def test_answer_does_not_approve_a_later_dangerous_tool(isolated_workspace):
    target = isolated_workspace / "keep.txt"
    target.write_text("keep me")
    runtime = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([
        {"tool_calls": [dict(ASK_CALL)]},
        {"tool_calls": [{"id": "danger", "name": "bash", "arguments": {
            "command": "rm keep.txt"}}]},
        {"text": "permission remained independent"},
    ]))
    with _client(runtime) as client:
        _, run_id = _start_run(client)
        question = wait_for_question(client, run_id)
        response = client.post(_answer_url(run_id, question["question_id"]),
                               json=_answer_body(), headers={"Idempotency-Key": "answer"})
        assert response.status_code == 200
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/runs/{run_id}").json()
            if state["pending_approvals"]:
                break
            time.sleep(0.01)
        assert state["pending_approvals"]
        assert target.read_text() == "keep me"
        approval = next(iter(state["pending_approvals"]))
        response = client.post(f"/api/runs/{run_id}/approvals/{approval}",
                               json={"approved": False}, headers={"Idempotency-Key": "deny"})
        assert response.status_code == 200, response.text
        wait_for_status(client, run_id, {"completed"})
        assert target.read_text() == "keep me"
