"""Persisted DAGs dispatch through real read-only Host execution."""

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from eventide.agent_graph import FACTS, GraphDefinition
from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.models import ModelResponse, RunRequest, ToolCall
from tests.test_api import wait_for_run
from tests.test_readonly_workers import worker_host


def definition(*specs):
    return GraphDefinition.model_validate(
        {
            "title": "Investigate",
            "nodes": [{"id": name, "goal": name, "depends_on": deps} for name, deps in specs],
        }
    )


@pytest.mark.parametrize(
    "specs",
    [
        (("a", []), ("a", [])),
        (("a", ["missing"]),),
        (("a", ["a"]),),
        (("a", ["b"]), ("b", ["a"])),
        (("a", ["b", "b"]), ("b", [])),
    ],
)
def test_reject_invalid_dag(specs):
    with pytest.raises(ValidationError):
        definition(*specs)


class GraphProvider:
    def __init__(self, hold=False, fail=False):
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hold, self.fail = hold, fail

    async def complete(self, request):
        self.requests.append(request)
        if "read-only side-question" not in request.messages[0]["content"]:
            return ModelResponse(text="root", usage={"input_tokens": 2, "output_tokens": 1})
        self.entered.set()
        if self.hold:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("Provider failure")
        if len(request.messages) == 1:
            return ModelResponse(
                tool_calls=(ToolCall("read", "read_file", {"path": "code.py"}),),
                usage={"input_tokens": 3, "output_tokens": 1},
            )
        return ModelResponse(text="found value = 1", usage={"input_tokens": 4, "output_tokens": 2})


async def wait_graph(host, graph_id, state):
    for _ in range(500):
        value = host.agent_graphs.get(graph_id)
        if value["state"] == state:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"Graph did not reach {state}: {value}")


async def root_graph(host, spec=None, key="create"):
    root = await host.run(RunRequest("root"))
    graph = host.agent_graphs.create(root.run_id, spec or definition(("a", [])), key)
    return root, graph


async def test_dag_dependency_results_unique_claims_budget_and_source(isolated_workspace):
    provider = GraphProvider()
    host = worker_host(isolated_workspace, provider)
    try:
        root, graph = await root_graph(host, definition(("a", []), ("b", ["a"])))
        graph_id = graph["graph_id"]
        assert not graph["started"]
        assert host.agent_graphs.tasks == {}
        same = host.agent_graphs.create(root.run_id, definition(("a", []), ("b", ["a"])), "create")
        assert same["graph_id"] == graph_id
        with pytest.raises(ValueError, match="different content"):
            host.agent_graphs.create(root.run_id, definition(("other", [])), "create")
        await asyncio.gather(
            host.agent_graphs.advance(graph_id), host.agent_graphs.advance(graph_id)
        )
        completed = await wait_graph(host, graph_id, "completed")
        claims = [f for f in host.store.facts_by_type(FACTS) if f["type"] == "graph.node_claimed"]
        assert len(claims) == 2
        for node in completed["nodes"]:
            run_id = node["claim"]["run_id"]
            intent = host._admitted_intent(run_id)
            assert intent["parent_run_id"] == root.run_id
            assert intent["worker_role"] == "graph_node"
            assert {t["name"] for req in provider.requests[1:] for t in req.tools} == {
                "read_file",
                "glob",
                "search_text",
                "compact",
                "read_tool_result",
            }
            _, budget_context = host.model_budget.context(run_id)
            assert budget_context.root_run_id == root.run_id
            assert host.store.usage_events(run_id=run_id)
        later = [r for r in provider.requests if "Earlier node results" in str(r.messages)]
        assert later and "found value = 1" in str(later[0].messages)
        assert (host.settings.workdir / "code.py").read_text() == "value = 1\nprint(value)\n"
        assert list((host.state_dir / "workers").glob("eventide-readonly-*")) == []
        assert not any(
            f["type"] == "side_question.created" for f in host.store.run_events(root.run_id)
        )
    finally:
        await host.close()


async def test_global_capacity_two_graphs_drafts_and_cancellation(isolated_workspace):
    provider = GraphProvider(hold=True)
    host = worker_host(isolated_workspace, provider)
    try:
        root, first = await root_graph(host, definition(*[(f"a{i}", []) for i in range(5)]))
        second = host.agent_graphs.create(root.run_id, definition(("b", [])), "second")
        await host.agent_graphs.advance(first["graph_id"])
        assert len(host.agent_graphs.tasks) == 4
        assert not host.agent_graphs.get(second["graph_id"])["started"]
        assert host.agent_graphs.get(second["graph_id"])["nodes"][0]["claim"] is None
        await host.agent_graphs.advance(second["graph_id"])
        assert len(host.agent_graphs.tasks) == 4
        await provider.entered.wait()
        cancelled = await host.agent_graphs.cancel(first["graph_id"])
        assert cancelled["state"] == "cancelled"
        assert all(n["state"] in {"interrupted", "cancelled"} for n in cancelled["nodes"])
        provider.hold = False
        provider.release.set()
        assert (await wait_graph(host, second["graph_id"], "completed"))["nodes"][0]["id"] == "b"
        assert (
            len(
                [
                    f
                    for f in host.store.facts_by_type(FACTS)
                    if f["type"] == "graph.node_claimed"
                    and f["payload"]["graph_id"] == first["graph_id"]
                ]
            )
            == 4
        )
    finally:
        await host.close()


async def test_cancel_before_start_and_failure_blocks_dependency(isolated_workspace):
    host = worker_host(isolated_workspace, GraphProvider(fail=True))
    try:
        root, graph = await root_graph(host, definition(("a", []), ("b", ["a"])))
        await host.agent_graphs.advance(graph["graph_id"])
        await host.agent_graphs.cancel(graph["graph_id"])
        assert host.agent_graphs.get(graph["graph_id"])["state"] == "cancelled"
        assert not host.agent_graphs.tasks
        other = host.agent_graphs.create(root.run_id, definition(("x", []), ("y", ["x"])), "other")
        await host.agent_graphs.advance(other["graph_id"])
        failed = await wait_graph(host, other["graph_id"], "failed")
        assert [n["state"] for n in failed["nodes"]] == ["failed", "blocked"]
    finally:
        await host.close()


async def test_preparing_is_not_reclaimed_and_restart_does_not_repeat(isolated_workspace):
    provider = GraphProvider(hold=True)
    host = worker_host(isolated_workspace, provider)
    settings = host.settings
    root, graph = await root_graph(host)
    graph_id = graph["graph_id"]
    await host.agent_graphs.advance(graph_id)
    await host.agent_graphs.advance(graph_id)
    assert (
        len([f for f in host.store.facts_by_type(FACTS) if f["type"] == "graph.node_claimed"]) == 1
    )
    await provider.entered.wait()
    await host.close()
    replacement = RuntimeHost(settings, GraphProvider())
    try:
        await replacement.initialize()
        before = replacement.agent_graphs.get(graph_id)
        assert before["nodes"][0]["state"] == "interrupted"
        child_id = before["nodes"][0]["claim"]["run_id"]
        child_session = replacement.store.get_run_identity(child_id)["session_id"]
        assert not any(
            f["type"] == "run.cancel_requested"
            for f in replacement.store.session_events(child_session)
        )
        await replacement.agent_graphs.advance(graph_id)
        assert replacement.agent_graphs.tasks == {}
        assert (
            len(
                [
                    f
                    for f in replacement.store.facts_by_type(FACTS)
                    if f["type"] == "graph.node_claimed"
                ]
            )
            == 1
        )
    finally:
        await replacement.close()


def test_graph_http_binding_origin_and_idempotency(isolated_workspace):
    host = worker_host(isolated_workspace, GraphProvider())
    with TestClient(create_app(host)) as client:
        session = client.post(
            "/api/sessions", headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["session_id"]
        run_id = client.post(
            f"/api/sessions/{session}/runs",
            json={"prompt": "root"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["run_id"]
        wait_for_run(client, run_id)
        endpoint = f"/api/runs/{run_id}/graphs"
        body = definition(("a", [])).model_dump()
        headers = {"Idempotency-Key": "same-request"}
        response = client.post(endpoint, json=body, headers=headers)
        assert response.status_code == 201, response.text
        graph = response.json()
        assert not graph["started"]
        assert client.post(endpoint, json=body, headers=headers).json() == graph
        assert (
            client.post(endpoint, json={**body, "title": "changed"}, headers=headers).status_code
            == 409
        )
        assert (
            client.post(
                endpoint,
                json=body,
                headers={"Idempotency-Key": uuid.uuid4().hex, "Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert client.get("/api/runs/missing/graphs").status_code == 404
        assert client.get("/api/graphs/missing").status_code == 404
        assert (
            client.post(
                endpoint,
                json={"title": "bad", "nodes": [{"id": "w", "goal": "g", "kind": "write"}]},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ).status_code
            == 422
        )
        graph_id = graph["graph_id"]
        assert (
            client.post(
                f"/api/graphs/{graph_id}/advance", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).status_code
            == 200
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = client.get(f"/api/graphs/{graph_id}").json()
            if value["state"] == "completed":
                break
            time.sleep(0.01)
        assert value["state"] == "completed"
        assert client.get(endpoint).json()["graphs"][0]["graph_id"] == graph_id
        assert (
            client.post(
                f"/api/graphs/{graph_id}/cancel", headers={"Idempotency-Key": uuid.uuid4().hex}
            ).status_code
            == 200
        )


async def test_root_budget_blocks_children_and_cannot_reset(isolated_workspace):
    from eventide.budgets import BudgetScope

    provider = GraphProvider()
    host = worker_host(isolated_workspace, provider)
    try:
        root, graph = await root_graph(host)
        host.configure_budget(BudgetScope("run", root.run_id), token_limit=0, request_id="no-more")
        await host.agent_graphs.advance(graph["graph_id"])
        assert (await wait_graph(host, graph["graph_id"], "failed"))["nodes"][0][
            "state"
        ] == "failed"
        assert len(provider.requests) == 1
    finally:
        await host.close()


async def test_companion_capacity_and_configuration_guard(isolated_workspace):
    from dataclasses import replace

    provider = GraphProvider(hold=True)
    host = worker_host(isolated_workspace, provider)
    try:
        root, graph = await root_graph(host, definition(*[(f"a{i}", []) for i in range(4)]))
        host._configuring = True
        with pytest.raises(ValueError, match="configuration"):
            await host.agent_graphs.advance(graph["graph_id"])
        host._configuring = False
        await host.agent_graphs.advance(graph["graph_id"])
        with pytest.raises(ValueError, match="four"):
            await host.create_side_question(root.run_id, "extra")
        await host.agent_graphs.cancel(graph["graph_id"])
        stale = host.agent_graphs.create(root.run_id, definition(("stale", [])), "stale")
        host.settings = replace(host.settings, base_url="http://changed.invalid")
        await host.agent_graphs.advance(stale["graph_id"])
        failed = await wait_graph(host, stale["graph_id"], "failed")
        assert "provider configuration changed" in failed["nodes"][0]["result"]["error"]
        assert len(provider.requests) == 1
    finally:
        host._configuring = False
        await host.close()


async def test_preparation_cancel_cleans_snapshot_and_has_no_model_call(
    isolated_workspace, monkeypatch
):

    provider = GraphProvider()
    host = worker_host(isolated_workspace, provider)
    preparing = asyncio.Event()
    release = asyncio.Event()
    original_check = host._admission_check

    async def controlled_check(operation, *args, **kwargs):
        value = await original_check(operation, *args, **kwargs)
        if operation.__name__ == "make_snapshot":
            preparing.set()
            await release.wait()
        return value

    monkeypatch.setattr(host, "_admission_check", controlled_check)
    try:
        root, graph = await root_graph(host)
        await host.agent_graphs.advance(graph["graph_id"])
        await asyncio.wait_for(preparing.wait(), 3)
        await host.agent_graphs.advance(graph["graph_id"])
        assert len(host.agent_graphs.tasks) == 1
        assert await host.agent_graphs.cancel(graph["graph_id"])
        assert len(provider.requests) == 1
        assert not list((host.state_dir / "workers").glob("eventide-readonly-*"))
        assert host.agent_graphs.get(graph["graph_id"])["state"] == "cancelled"
    finally:
        await host.close()


@pytest.mark.parametrize("target", ["worktree", "snapshot"])
async def test_nodes_read_actual_root_copy(isolated_workspace, target):
    provider = GraphProvider()
    host = worker_host(isolated_workspace, provider)
    try:
        if target == "worktree":
            from eventide.workspace import git

            git(host.settings.workdir, "add", "code.py")
            git(host.settings.workdir, "commit", "-m", "fixture source")
        root = await host.run(
            RunRequest(
                "root", execution_target=target, mode="plan" if target == "snapshot" else "auto"
            )
        )
        binding = host._execution_binding(root.run_id)
        _, copied = host._execution_paths(binding)
        if target == "worktree":
            (copied / "code.py").write_text("value = 7\n")
            expected = "value = 7"
        else:
            (host.settings.workdir / "code.py").write_text("value = 99\n")
            expected = "value = 1"
        graph = host.agent_graphs.create(root.run_id, definition(("a", [])), "actual-copy")
        await host.agent_graphs.advance(graph["graph_id"])
        completed = await wait_graph(host, graph["graph_id"], "completed")
        child_id = completed["nodes"][0]["result"]["run_id"]
        reads = [f for f in host.store.run_events(child_id) if f["type"] == "tool.completed"]
        assert expected in reads[0]["payload"]["content"]
        assert (host.settings.workdir / "code.py").read_text() == (
            "value = 1\nprint(value)\n" if target == "worktree" else "value = 99\n"
        )
    finally:
        await host.close()


async def test_repeated_graph_stop_waits_for_provider_cleanup(isolated_workspace):
    entered, finishing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ClosingProvider(GraphProvider):
        async def complete(self, request):
            if "read-only side-question" not in request.messages[0]["content"]:
                return await super().complete(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                finishing.set()
                await release.wait()
                raise

    host = worker_host(isolated_workspace, ClosingProvider())
    try:
        _, graph = await root_graph(host)
        graph_id = graph["graph_id"]
        await host.agent_graphs.advance(graph_id)
        await asyncio.wait_for(entered.wait(), 3)
        first = asyncio.create_task(host.agent_graphs.cancel(graph_id))
        await asyncio.wait_for(finishing.wait(), 3)
        second = asyncio.create_task(host.agent_graphs.cancel(graph_id))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert host.agent_graphs.get(graph_id)["state"] == "cancelling"
        release.set()
        done = await asyncio.gather(first, second)
        assert all(value["state"] == "cancelled" for value in done)
        assert host.agent_graphs.tasks == {}
        assert not list((host.state_dir / "workers").glob("eventide-readonly-*"))
    finally:
        release.set()
        await host.close()


async def test_waiting_graph_refills_when_side_questions_finish(isolated_workspace):
    provider = GraphProvider(hold=True)
    host = worker_host(isolated_workspace, provider)
    try:
        root = await host.run(RunRequest("root"))
        for i in range(4):
            await host.create_side_question(root.run_id, f"investigate {i}")
        graph = host.agent_graphs.create(root.run_id, definition(("a", [])), "waiting")
        await host.agent_graphs.advance(graph["graph_id"])
        assert not host.agent_graphs.tasks
        assert host.agent_graphs.get(graph["graph_id"])["state"] == "ready"
        provider.hold = False
        provider.release.set()
        assert (await wait_graph(host, graph["graph_id"], "completed"))["counts"]["completed"] == 1
    finally:
        provider.release.set()
        await host.close()
