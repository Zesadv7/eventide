"""Side questions: read-only companion Runs bound to a live main Run."""

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.interactions import SUPPLEMENT_EVENT
from eventide.models import ModelResponse, RunRequest, ToolCall
from eventide.side_questions import (
    SIDE_QUESTION_EVENT,
    answer_fingerprint,
    side_question_view,
)
from tests.test_host import make_repo
from tests.test_readonly_workers import worker_host
from tests.test_runtime import settings_for


class SideProvider:
    """Main Run blocks mid-task (no writes in flight) while the side reads."""

    def __init__(self, expect_return: bool = True):
        self.requests = []
        self.expect_return = expect_return
        self.side_read = asyncio.Event()
        self.side_done = asyncio.Event()

    async def complete(self, request):
        self.requests.append(request)
        if "side-question investigator" in request.messages[0]["content"]:
            if len(request.messages) == 1:
                return ModelResponse(
                    tool_calls=(
                        ToolCall("read", "read_file", {"path": "code.py"}),
                        ToolCall("write", "write_file", {"path": "evil.py", "content": "bad"}),
                    ),
                    usage={"input_tokens": 3, "output_tokens": 2},
                )
            content = request.messages[-1]["content"]
            assert "value = 1" in str(content), "side must read the frozen snapshot"
            self.side_done.set()
            return ModelResponse(
                text="side answer: value was 1", usage={"input_tokens": 4, "output_tokens": 1}
            )
        if len(request.messages) == 1:
            return ModelResponse(
                tool_calls=(ToolCall("peek", "read_file", {"path": "code.py"}),),
                usage={"input_tokens": 2, "output_tokens": 1},
            )
        if len(request.messages) == 3:
            assert "side answer" not in str(request.messages), "no auto-injection"
            await self.side_read.wait()
            return ModelResponse(
                tool_calls=(
                    ToolCall("mutate", "write_file", {"path": "code.py", "content": "value = 2\n"}),
                ),
                usage={"input_tokens": 5, "output_tokens": 2},
            )
        if self.expect_return:
            assert "Answer returned from side question" in str(request.messages)
            assert "side answer: value was 1" in str(request.messages)
        return ModelResponse(text="main done", usage={"input_tokens": 7, "output_tokens": 2})


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


async def test_side_question_runs_alongside_main_on_frozen_snapshot(isolated_workspace):
    provider = SideProvider()
    host = worker_host(isolated_workspace, provider)
    try:
        session = host.create_session()
        main_task = asyncio.create_task(
            host.run(RunRequest("private parent history", session))
        )
        await _wait_for(lambda: bool(host.store.session_events(session)))
        run_id = host.store.list_runs(session, limit=1)[0]["id"]
        await _wait_for(
            lambda: any(
                e["type"] == "run.started" for e in host.store.run_events(run_id)
            )
        )
        await _wait_for(
            lambda: any(
                e["type"] == "tool.completed"
                and e["payload"].get("name") == "read_file"
                for e in host.store.run_events(run_id)
            )
        )
        high_water = host.store.run_snapshot(run_id)["high_water"]

        record = await host.create_side_question(run_id, "what does code.py contain?")
        assert record["source_high_water"] >= high_water
        assert record["snapshot_id"]
        side_run_id = record["side_run_id"]
        assert side_run_id.startswith("run_")
        assert record["side_question_id"] == f"sq_{side_run_id[4:]}"

        # The side Run shares the parent budget link and is a distinct Session.
        identity = host.store.get_run(side_run_id)
        assert identity["parent_run_id"] == run_id
        assert identity["session_id"] != session

        await asyncio.wait_for(provider.side_done.wait(), 5)
        side = host.store.get_run(side_run_id)
        assert side["status"] == "completed"
        assert side["output"] == "side answer: value was 1"
        assert side["usage"] == {"input_tokens": 7, "output_tokens": 3}

        # The side Run is a fresh execution: no recovery/continuation identity
        # copied from the main Run; parent_run_id carries the budget link.
        admitted = next(
            e["payload"] for e in host.store.run_events(side_run_id)
            if e["type"] == "run.admitted"
        )
        assert "continuation_of" not in admitted
        assert "recovery_of" not in admitted
        assert admitted["parent_run_id"] == run_id
        assert admitted.get("worker_role") == "side_question"

        # The frozen evidence reached the side prompt: original goal, tool
        # activity, and an explicit exclusion count for later events.
        side_request = next(
            r for r in provider.requests
            if "side-question investigator" in r.messages[0]["content"]
        )
        prompt = side_request.messages[0]["content"]
        assert "private parent history" in prompt
        assert "read_file" in prompt
        assert "later main-Run events are deliberately excluded" in prompt

        # Readonly tools only, via ToolExecutor/Policy: the write was rejected.
        tools = side["tools"]
        assert tools["write"]["is_error"]
        assert not tools["read"]["is_error"]
        assert "value = 1" in tools["read"]["content"]
        side_request = next(
            r for r in provider.requests
            if "side-question investigator" in r.messages[0]["content"]
        )
        assert {tool["name"] for tool in side_request.tools} == {
            "read_file", "glob", "search_text", "compact", "read_tool_result",
        }
        started = next(
            e["payload"] for e in host.store.run_events(side_run_id)
            if e["type"] == "run.started"
        )
        assert started["snapshot_id"] == record["snapshot_id"]
        assert started["worker_role"] == "side_question"
        assert started["parent_run_id"] == run_id

        # The typed fact is durable on both sides and re-readable after "refresh".
        assert side_question_view(host.store, record)["answer"] == "side answer: value was 1"
        source_fact = [
            e["payload"] for e in host.store.session_events(session)
            if e["type"] == SIDE_QUESTION_EVENT
        ]
        assert source_fact and source_fact[-1]["side_question_id"] == record["side_question_id"]

        # Shared ledger: the rollup includes side attempts scoped to the parent.
        usage_events = host.store.usage_events(run_id=run_id, include_children=True)
        owners = {
            e.get("run_id") or e.get("payload", {}).get("run_id") for e in usage_events
        }
        assert {run_id, side_run_id} <= owners

        # Explicit user return while the main Run is active: pending, once.
        view = side_question_view(host.store, record)
        receipt = host.store.record_side_question_return(run_id, {
            "supplement_id": "sup_test0000000001",
            "run_id": run_id,
            "session_id": session,
            "body": f"[Answer returned from side question {record['side_question_id']}]\n"
                    "side answer: value was 1",
            "body_version": 1,
            "side_question_id": record["side_question_id"],
            "answer_fingerprint": view["answer_fingerprint"],
        })
        assert receipt[0] is None and receipt[1] is not None
        again = host.store.record_side_question_return(run_id, {
            "supplement_id": "sup_test0000000002",
            "run_id": run_id,
            "session_id": session,
            "body": "duplicate click",
            "body_version": 1,
            "side_question_id": record["side_question_id"],
            "answer_fingerprint": view["answer_fingerprint"],
        })
        assert again[0] is not None and again[1] is None
        supplements = [
            e["payload"] for e in host.store.session_events(session)
            if e["type"] == SUPPLEMENT_EVENT
            and e["payload"].get("side_question_id") == record["side_question_id"]
        ]
        assert len(supplements) == 1
        assert supplements[0]["status"] == "pending"

        # The main Run consumes the returned answer exactly once at its next
        # boundary; the provider asserts the injected text above.
        provider.side_read.set()
        main_result = await asyncio.wait_for(main_task, 5)
        assert main_result.status == "completed", main_result.output
        assert main_result.output == "main done"
        consumed = [
            e for e in host.store.run_events(run_id)
            if e["type"] == "interaction.supplement.consumed"
        ]
        assert len(consumed) == 1
        # The main Run advanced past the pinned cut and wrote after the snapshot.
        assert host.store.run_snapshot(run_id)["high_water"] > record["source_high_water"]
        assert (
            (isolated_workspace / "repo" / "code.py").read_text() == "value = 2\n"
        )

        # Real file evidence: the side Run's copied snapshot tree is gone once
        # the Run reached its terminal state.
        assert not list((host.state_dir / "workers").glob("eventide-readonly-*"))
    finally:
        await host.close()


async def test_side_question_cancellation_does_not_stop_main(isolated_workspace):
    class StuckSide(SideProvider):
        def __init__(self):
            super().__init__(expect_return=False)

        async def complete(self, request):
            if "side-question investigator" in request.messages[0]["content"]:
                await asyncio.Event().wait()  # side stays busy until cancelled
            return await SideProvider.complete(self, request)

    provider = StuckSide()
    host = worker_host(isolated_workspace, provider)
    try:
        session = host.create_session()
        main_task = asyncio.create_task(host.run(RunRequest("goal", session)))
        await _wait_for(lambda: bool(host.store.list_runs(session, limit=1)))
        run_id = host.store.list_runs(session, limit=1)[0]["id"]
        await _wait_for(
            lambda: any(e["type"] == "run.started" for e in host.store.run_events(run_id))
        )
        await _wait_for(
            lambda: any(
                e["type"] == "tool.completed" and e["payload"].get("name") == "read_file"
                for e in host.store.run_events(run_id)
            )
        )
        record = await host.create_side_question(run_id, "are you there?")
        cancelled = await host.cancel_run(record["side_run_id"])
        assert cancelled["status"] == "interrupted"
        provider.side_read.set()
        result = await asyncio.wait_for(main_task, 5)
        assert result.status == "completed"
        assert host.store.get_run(record["side_run_id"])["status"] == "interrupted"
        await _wait_for(
            lambda: not list((host.state_dir / "workers").glob("eventide-readonly-*"))
        )
    finally:
        await host.close()


def _http_client(workspace, provider):
    # The repo was prepared by the caller via make_repo: the Host workdir must
    # be the Git repo itself so snapshots have a trustworthy checkpoint.
    import dataclasses

    runtime = RuntimeHost(
        dataclasses.replace(
            settings_for(workspace / "repo"),
            state_dir=workspace / "state",
            workers_enabled=True,
        ),
        provider,
    )
    return TestClient(create_app(runtime))


def test_side_question_http_late_return_and_idempotency(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "code.py").write_text("value = 1\n")
    from eventide.providers import ScriptedProvider

    provider = ScriptedProvider([
        {"text": "main finished", "usage": {"input_tokens": 6, "output_tokens": 2}},
        {"text": "side says: code.py holds value = 1",
         "usage": {"input_tokens": 4, "output_tokens": 1}},
    ])
    # The context manager keeps the app (and its portal) alive across requests;
    # a bare TestClient cancels background Run tasks between requests.
    with _http_client(isolated_workspace, provider) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "check code.py"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get(f"/api/runs/{run_id}").json().get("status") == "completed":
                break
            time.sleep(0.01)
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"

        created = client.post(
            f"/api/sessions/{session}/side-questions",
            json={"question": "summarize code.py"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert created.status_code == 202, created.text
        record = created.json()
        deadline = time.monotonic() + 10
        view = {}
        while time.monotonic() < deadline:
            view = client.get(f"/api/side-questions/{record['side_question_id']}").json()
            if view["side_run_status"] == "completed":
                break
            time.sleep(0.01)
        assert view["side_run_status"] == "completed"
        assert view["answer"]
        assert view["returned"] is False
        assert view["source_high_water"] >= 0
        assert view["snapshot_id"] == record["snapshot_id"]

        fingerprint = answer_fingerprint(view["answer"])
        conflict = client.post(
            f"/api/side-questions/{record['side_question_id']}/return",
            json={"answer_fingerprint": "0" * 64},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert conflict.status_code == 409

        returned = client.post(
            f"/api/side-questions/{record['side_question_id']}/return",
            json={"answer_fingerprint": fingerprint},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert returned.status_code == 200, returned.text
        body = returned.json()
        assert body["returned"] is True
        assert body["status"] == "late"  # main Run terminal: never resumed
        assert body["already_returned"] is False

        repeat = client.post(
            f"/api/side-questions/{record['side_question_id']}/return",
            json={"answer_fingerprint": fingerprint},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert repeat.status_code == 200
        assert repeat.json()["already_returned"] is True
        assert repeat.json()["supplement_id"] == body["supplement_id"]

        listed = client.get(f"/api/runs/{run_id}/side-questions").json()
        assert [v["side_question_id"] for v in listed["side_questions"]] == [
            record["side_question_id"]
        ]
        assert listed["side_questions"][0]["returned"] is True
        supplements = client.get(f"/api/runs/{run_id}/supplements").json()
        assert supplements["pending"] == 0
        receipts = [
            s for s in supplements["supplements"]
            if s.get("side_question_id") == record["side_question_id"]
        ]
        assert len(receipts) == 1 and receipts[0]["status"] == "late"

        # The terminal main Run was not resumed by the late answer.
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "completed"

        missing = client.post(
            f"/api/sessions/{session}/side-questions",
            json={"question": "hi", "source_run_id": "run_does_not_exist"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert missing.status_code == 404


@pytest.mark.parametrize("bad", ["", "   ", "x" * 8001])
def test_side_question_validation(isolated_workspace, bad):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "code.py").write_text("value = 1\n")
    from eventide.providers import ScriptedProvider

    with _http_client(isolated_workspace, ScriptedProvider([
        {"text": "done", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "p"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        run_id = accepted.json()["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get(f"/api/runs/{run_id}").json().get("status") == "completed":
                break
            time.sleep(0.01)
        response = client.post(
            f"/api/sessions/{session}/side-questions",
            json={"question": bad, "source_run_id": run_id},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert response.status_code == 422
        # Malformed identities must read as 4xx, never a 500.
        assert client.get("/api/side-questions/sq_nope").status_code == 404
        assert client.get("/api/side-questions/run_not_a_side").status_code == 404


def test_side_question_http_truncated_return_is_explicit(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "code.py").write_text("value = 1\n")
    from eventide.providers import ScriptedProvider

    long_answer = "x" * 9000
    with _http_client(isolated_workspace, ScriptedProvider([
        {"text": "main finished", "usage": {"input_tokens": 6, "output_tokens": 2}},
        {"text": long_answer, "usage": {"input_tokens": 4, "output_tokens": 1}},
    ])) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        run_id = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "p"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get(f"/api/runs/{run_id}").json().get("status") == "completed":
                break
            time.sleep(0.01)
        created = client.post(
            f"/api/sessions/{session}/side-questions",
            json={"question": "long answer please"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert created.status_code == 202, created.text
        record = created.json()
        deadline = time.monotonic() + 10
        view = {}
        while time.monotonic() < deadline:
            view = client.get(
                f"/api/side-questions/{record['side_question_id']}"
            ).json()
            if view["side_run_status"] == "completed":
                break
            time.sleep(0.01)
        assert view["answer"] == long_answer
        returned = client.post(
            f"/api/side-questions/{record['side_question_id']}/return",
            json={"answer_fingerprint": answer_fingerprint(long_answer)},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert returned.status_code == 200, returned.text
        body = returned.json()
        assert body["body_truncated"] is True
        assert body["answer_length"] == 9000
        receipts = [
            s for s in client.get(f"/api/runs/{run_id}/supplements").json()["supplements"]
            if s.get("side_question_id") == record["side_question_id"]
        ]
        assert len(receipts) == 1
        stored = receipts[0]
        assert len(stored["body"]) <= 8000
        assert "truncated" in stored["body"]
        assert stored["body_truncated"] is True
