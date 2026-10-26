"""Attention center projections: readable approvals and pending facts."""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.attention import summarize_tool_call
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_api import wait_for_run
from tests.test_runtime import settings_for


def wait_for_approval(runtime: RuntimeHost, run_id: str) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        facts = runtime.store.pending_approval_facts()
        if facts:
            return facts[0]["payload"]["approval_id"]
        time.sleep(0.01)
    raise AssertionError("approval.required fact never appeared")


def test_attention_projects_readable_approval(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "danger", "name": "bash",
                             "arguments": {"command": "rm -rf x"}}]},
            {"text": "denial handled"},
        ]
    )
    runtime = RuntimeHost(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        run_id = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "delete"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        approval_id = wait_for_approval(runtime, run_id)

        snapshot = client.get("/api/attention").json()
        assert snapshot["counts"]["approvals"] == 1
        item = snapshot["approvals"][0]
        assert item["dedup_key"] == f"approval:{approval_id}"
        assert item["approval_id"] == approval_id
        assert item["run_id"] == run_id
        assert item["session_id"] == session
        assert item["actionable"] is True
        assert item["arguments"] == {"command": "rm -rf x"}
        assert item["action"]["url"] == f"/api/runs/{run_id}/approvals/{approval_id}"
        summary = item["summary"]
        assert summary["command"] == "rm -rf x"
        assert summary["execution_kind"] == "source"
        assert summary["impact"] == "写入源工作区（未隔离）"
        assert summary["network_access"] is None
        assert summary["privilege_elevation"] is None
        assert snapshot["counts"]["questions"] == 0
        assert snapshot["questions"] == []
        assert "awaiting_answers" not in snapshot["unsupported"]

        resolved = client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"approved": False},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert resolved.status_code == 200
        assert wait_for_run(client, run_id)["output"] == "denial handled"
        refreshed = client.get("/api/attention").json()
        assert refreshed["counts"]["approvals"] == 0
        assert refreshed["approvals"] == []


def test_attention_keeps_expired_approval_rejected(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "danger", "name": "bash",
                             "arguments": {"command": "rm -rf x"}}]},
        ]
    )
    runtime = RuntimeHost(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        run_id = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "delete"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        approval_id = wait_for_approval(runtime, run_id)
        # Cancel never emits a resolution: the unpaired fact stays pending in a
        # terminal run, so the center must flag it expired and acting must stay
        # rejected by the original approval endpoint.
        cancelled = client.post(
            f"/api/runs/{run_id}/cancel",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert cancelled.status_code in {200, 202}
        assert wait_for_run(client, run_id)["status"] == "interrupted"
        snapshot = client.get("/api/attention").json()
        assert snapshot["counts"]["approvals"] == 1
        item = snapshot["approvals"][0]
        assert item["dedup_key"] == f"approval:{approval_id}"
        assert item["approval_id"] == approval_id
        assert item["actionable"] is False
        assert item["run_status"] == "interrupted"
        assert item["arguments"] == {"command": "rm -rf x"}
        rejected = client.post(
            f"/api/runs/{run_id}/approvals/{approval_id}",
            json={"approved": True},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        # The original endpoint stays the only authority: an expired fact is
        # never actionable again, whatever the center displays.
        assert rejected.status_code in {404, 409}
        assert wait_for_run(client, run_id)["status"] == "interrupted"


def test_attention_lists_failed_runs_and_unknown_project(isolated_workspace):
    runtime = RuntimeHost(
        settings_for(isolated_workspace),
        ScriptedProvider([{"error": "boom"}]),
    )
    with TestClient(create_app(runtime)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        run_id = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "break"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        assert wait_for_run(client, run_id)["status"] == "failed"
        snapshot = client.get("/api/attention").json()
        assert snapshot["counts"]["runs"] == 1
        item = snapshot["runs"][0]
        assert item["dedup_key"] == f"run:{run_id}:failed"
        assert item["run_id"] == run_id
        assert item["session_id"] == session
        assert item["links"]["run"] == f"/runs/{run_id}/diagnostics"
        assert client.get("/api/projects/missing-project/attention").status_code == 404


def test_attention_dedupes_across_sessions(isolated_workspace):
    provider = ScriptedProvider(
        [
            {"tool_calls": [{"id": "call_1", "name": "bash",
                             "arguments": {"command": "rm a"}}]},
            {"text": "first done"},
            {"tool_calls": [{"id": "call_2", "name": "bash",
                             "arguments": {"command": "rm b"}}]},
            {"text": "second done"},
        ]
    )
    runtime = RuntimeHost(settings_for(isolated_workspace), provider)
    with TestClient(create_app(runtime)) as client:
        def start_run():
            session = client.post(
                "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).json()["session_id"]
            return session, client.post(
                f"/api/sessions/{session}/runs",
                json={"prompt": "work"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ).json()["run_id"]

        def approval_of(run_id: str) -> str:
            for _ in range(100):
                for fact in runtime.store.pending_approval_facts():
                    if fact["run_id"] == run_id:
                        return fact["payload"]["approval_id"]
                time.sleep(0.01)
            raise AssertionError(f"approval for {run_id} never appeared")

        first_session, first_run = start_run()
        first_approval = approval_of(first_run)
        # A source Workspace runs one write task at a time; the second Session
        # only starts after the first decision, so the center is checked once
        # per pending approval and keys must stay distinct across sessions.
        assert client.get("/api/attention").json()["approvals"][0][
            "dedup_key"
        ] == f"approval:{first_approval}"
        client.post(
            f"/api/runs/{first_run}/approvals/{first_approval}",
            json={"approved": False},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert wait_for_run(client, first_run)["output"] == "first done"

        second_session, second_run = start_run()
        second_approval = approval_of(second_run)
        snapshot = client.get("/api/attention").json()
        keys = [item["dedup_key"] for item in snapshot["approvals"]]
        assert keys == [f"approval:{second_approval}"]
        assert first_approval != second_approval
        assert snapshot["counts"]["approvals"] == 1
        assert snapshot["approvals"][0]["session_id"] == second_session
        client.post(
            f"/api/runs/{second_run}/approvals/{second_approval}",
            json={"approved": False},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert wait_for_run(client, second_run)["output"] == "second done"
        assert client.get("/api/attention").json()["counts"]["approvals"] == 0


def test_summarize_tool_call_never_guesses_undeclared_scope():
    summary = summarize_tool_call("bash", "not-a-dict", {"kind": "worktree"})
    assert summary["command"] is None
    assert summary["impact"] == "写入隔离执行副本"
    assert summary["execution_label"] == "隔离工作副本"
    unknown = summarize_tool_call("mcp__files__read", {"path": "a.txt"}, {})
    assert unknown["mcp_server"] == "files"
    assert unknown["mcp_tool"] == "read"
    assert unknown["impact"] is None
    assert unknown["network_access"] is None


def test_project_filter_precedes_limit_and_global_includes_all_projects(isolated_workspace):
    from eventide.attention import collect_attention

    runtime = RuntimeHost(settings_for(isolated_workspace))
    first_path, second_path = isolated_workspace / "one", isolated_workspace / "two"
    first_path.mkdir()
    second_path.mkdir()
    first = runtime.resolve_or_register_workspace(first_path)
    second = runtime.resolve_or_register_workspace(second_path)
    for index, workspace in enumerate((first, second)):
        session = runtime.create_session(workspace_id=workspace.workspace_id)
        run = f"attention_run_{index}"
        runtime.store.create_run(run, session)
        runtime.store.append_event(run, "approval.required", {
            "approval_id": f"approval_{index}", "tool": "bash", "arguments": {"command": "rm x"},
        })
        runtime.store.append_event(run, "run.failed", {"error": "test failure"})
    scoped = collect_attention(runtime, project_id=first.project_id, approval_limit=1, run_limit=1)
    assert [item["run_id"] for item in scoped["runs"]] == ["attention_run_0"]
    assert [item["run_id"] for item in scoped["approvals"]] == ["attention_run_0"]
    assert scoped["limits"]["runs"] == 1
    runtime.store.close()
    runtime._lease.close()
