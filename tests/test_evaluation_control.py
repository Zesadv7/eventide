"""Web evaluations share root ownership, while fixture runtimes remain test artifacts."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.bundled_evaluations import builtin_suite_catalog, load_builtin_suite
from eventide.config import Settings
from eventide.evaluation_control import EvaluationController
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.scheduler import QueueFull, SchedulingLimits
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def host_for(path, *, inside=False):
    source = make_repo(path / "source")
    return RuntimeHost(
        settings_for(source, state_dir=(source / ".state" if inside else path / "state")),
        ScriptedProvider([]),
        enable_mcp=False,
    )


def payload(split="development"):
    return {
        "suite_id": "engineering-v1",
        "suite_sha256": builtin_suite_catalog()[0]["suite_sha256"],
        "split": split,
        "mode": "offline",
    }


async def admit(controller, identity="eval_1", split="development"):
    body = payload(split)
    body.pop("mode")
    return await controller.admit(controller.host.default_workspace.project_id, identity, **body)


async def spin_until(check):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not check():
        if loop.time() >= deadline:
            raise asyncio.TimeoutError("Condition not reached within 10 seconds")
        await asyncio.sleep(0.005)


async def test_offline_twenty_actual_cases_no_credentials_network_or_main_usage(
    tmp_path, monkeypatch
):
    host = host_for(tmp_path, inside=True)
    source = Path(host.default_workspace.path)
    before = host._workspace_checkpoint(source)
    controller = EvaluationController(host)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline evaluation accessed external configuration/provider")

    monkeypatch.setattr(Settings, "from_env", forbidden)
    monkeypatch.setattr("eventide.evaluation.build_provider", forbidden)
    monkeypatch.setattr("eventide.evaluation.resolve_provider_configuration", forbidden)
    try:
        run_id = await admit(controller, split="all")
        assert not host.workspace_busy(host.default_workspace.workspace_id)
        result = await controller.start(run_id)
        assert result.status == "completed"
        detail = controller.detail(run_id)
        assert (detail["total"], detail["attempted"], detail["passed"], detail["failed"]) == (
            20,
            20,
            20,
            0,
        )
        assert detail["billing_applicable"] is False
        assert detail["measurement_kind"] == "scripted_mechanism_validation"
        report = controller.report(run_id)
        assert report["passed"] == 20 and report["billing_applicable"] is False
        catalog = builtin_suite_catalog()[0]
        assert report["provenance"]["suite_source_sha256"] == catalog["manifest_sha256"]
        assert report["provenance"]["builtin_suite"]["suite_sha256"] == catalog["suite_sha256"]
        assert catalog["manifest_sha256"] != catalog["suite_sha256"]
        assert len(report["results"]) == 20
        assert host.store.usage_events(run_id=run_id) == []
        assert not any(e["type"].startswith("budget.") for e in host.store.run_events(run_id))
        assert host._workspace_checkpoint(source) == before
        assert host.provider.requests == []
        events = host.store.run_events(run_id)
        assert len([e for e in events if e["type"] == "evaluation.case_completed"]) == 20
        assert len([e for e in events if e["type"] == "evaluation.report_saved"]) == 22
    finally:
        await host.close()


async def test_eval_waits_for_root_slot_and_cancel_before_dispatch_creates_no_artifacts(tmp_path):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    try:
        roots = [
            await host.admit_run(
                RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
            )
            for _ in range(4)
        ]
        run_id = await admit(controller)
        task = controller.start(run_id)
        detail = controller.detail(run_id)
        assert detail["dispatch_state"] == "queued" and detail["queue_position"] == 1
        assert host.dispatch.snapshot()["counts"]["active_roots"] == 4
        assert host.run_execution(run_id)["kind"] == "evaluation"
        with pytest.raises(ValueError, match="busy"):
            await admit(controller, "eval_other")
        stopped = await controller.cancel(run_id, "cancel-1")
        assert task.cancelled() and stopped["status"] == "interrupted"
        assert not stopped["report_available"]
        assert not (host.state_dir / "evaluations" / run_id).exists()
        assert (await controller.cancel(run_id, "cancel-1"))["status"] == "interrupted"
        assert (
            len(
                [
                    e
                    for e in host.store.run_events(run_id)
                    if e["type"] == "evaluation.cancel_requested"
                ]
            )
            == 1
        )
        assert all(host.store.get_run(r)["status"] == "queued" for r in roots)
    finally:
        await host.close()


async def test_global_queue_full_rejects_without_control_run(tmp_path):
    host = host_for(tmp_path)
    host.dispatch.scheduler.limits = SchedulingLimits(active_roots=1, waiting_roots=1)
    controller = EvaluationController(host)
    try:
        for _ in range(2):
            await host.admit_run(
                RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
            )
        with pytest.raises(QueueFull):
            await admit(controller)
        assert host.store.get_run_identity("eval_1") is None
        assert not host.session_busy(controller.owner(host.default_workspace.project_id))
    finally:
        await host.close()


async def test_frozen_cases_dispatch_and_cleanup_keep_root_owned(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    host.dispatch.scheduler.limits = SchedulingLimits(active_roots=1)
    entered = asyncio.Event()
    cleanup = asyncio.Event()
    cleaning = asyncio.Event()
    observed = []

    async def fake_run(path, **kwargs):
        observed.append(kwargs["cases_snapshot"])
        kwargs["progress"]("case_started", {"id": observed[0][0]["id"], "index": 1})
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await cleanup.wait()

    monkeypatch.setattr("eventide.evaluation.run_evaluations", fake_run)
    try:
        prior = await host.admit_run(
            RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
        )
        run_id = await admit(controller)
        controller.start(run_id)
        frozen = load_builtin_suite("engineering-v1", split="development")["cases"]
        monkeypatch.setattr(
            "eventide.bundled_evaluations.load_builtin_suite",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("reloaded")),
        )
        await host.cancel_run(prior)
        await entered.wait()
        assert observed[0] == frozen
        stop = asyncio.create_task(controller.cancel(run_id, "stop"))
        await cleaning.wait()
        assert host.dispatch.scheduler.counts()["active_roots"] == 1
        assert not stop.done()
        cleanup.set()
        assert (await stop)["status"] == "interrupted"
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        cleanup.set()
        await host.close()


@pytest.mark.parametrize("started", [False, True])
async def test_restart_parks_controls_without_user_checkpoint_or_replay(
    tmp_path, monkeypatch, started
):
    host = host_for(tmp_path)
    settings = host.settings
    controller = EvaluationController(host)
    run_id = await admit(controller)
    owner = controller.owner(host.default_workspace.project_id)
    if started:
        host.store.append_event(run_id, "run.started", host._admitted_intent(run_id))
    # Simulate process loss: no Host close/terminal cleanup before opening fresh owner.
    host._closed = True
    host.store.close()
    host._lease.close()
    monkeypatch.setattr(
        RuntimeHost,
        "_crash_gap_evidence",
        lambda *a: (_ for _ in ()).throw(AssertionError("source inspected")),
    )
    reopened = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        current = EvaluationController(reopened)
        assert current.detail(run_id)["status"] == "interrupted"
        assert reopened.dispatch.snapshot()["active"] == []
        assert reopened.dispatch.snapshot()["queued"] == []
        assert not (settings.state_dir / "evaluations").exists()
        for operation in (reopened.continue_session(owner), reopened.abandon_interruption(owner)):
            with pytest.raises(ValueError, match="Worker"):
                await operation
        with pytest.raises(ValueError):
            reopened.start_admitted_run(run_id)
        assert await admit(current, "eval_new") == "eval_new"
    finally:
        await reopened.close()


async def test_report_hash_missing_and_symlink_never_change_canonical_outcome(
    tmp_path, monkeypatch
):
    host = host_for(tmp_path)
    controller = EvaluationController(host)

    async def fake_run(path, **kwargs):
        report = {"passed": 0, "total": 12, "billing_applicable": False, "status": "completed"}
        kwargs["progress"]("report_saved", report)
        return report

    monkeypatch.setattr("eventide.evaluation.run_evaluations", fake_run)
    try:
        run_id = await admit(controller)
        await controller.start(run_id)
        evidence = next(
            e["payload"]
            for e in host.store.run_events(run_id)
            if e["type"] == "evaluation.report_saved"
        )
        path = host.state_dir / "evaluations" / run_id / evidence["name"]
        body = path.read_bytes()
        path.write_text("{}")
        with pytest.raises(ValueError, match="fingerprint"):
            controller.report(run_id)
        assert controller.detail(run_id)["status"] == "completed"
        path.unlink()
        with pytest.raises(ValueError, match="missing"):
            controller.report(run_id)
        foreign = tmp_path / "foreign.json"
        foreign.write_bytes(body)
        path.symlink_to(foreign)
        with pytest.raises(ValueError, match="changed"):
            controller.report(run_id)
    finally:
        await host.close()


def test_http_contract_rejections_idempotency_report_and_sse(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    project = host.default_workspace.project_id

    async def fake_run(path, **kwargs):
        report = {"passed": 0, "total": 12, "billing_applicable": False, "status": "completed"}
        kwargs["progress"]("report_saved", report)
        return report

    monkeypatch.setattr("eventide.evaluation.run_evaluations", fake_run)
    with TestClient(create_app(host)) as client:
        path = f"/api/projects/{project}/evaluations"
        body = payload()
        headers = {"Idempotency-Key": "same-request"}
        assert client.get("/api/evaluations/suites").json()[0]["supported_modes"] == [
            "offline", "live",
        ]
        assert client.post(path, json=body).status_code == 422
        assert client.post(path, json={**body, "mode": "live"}, headers=headers).status_code == 409
        assert (
            client.post(path, json={**body, "path": "/tmp/evil"}, headers=headers).status_code
            == 422
        )
        assert (
            client.post(
                path, json=body, headers={**headers, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        stale = client.post(path, json={**body, "suite_sha256": "a" * 64}, headers=headers)
        assert stale.status_code == 409
        assert stale.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 202
        receipt = response.json()
        replay = client.post(path, json=body, headers=headers)
        assert replay.json() == receipt
        conflict = client.post(path, json={**body, "split": "all"}, headers=headers)
        assert conflict.status_code == 409
        assert "X-Eventide-Mutation-Outcome" not in conflict.headers
        detail = client.get(receipt["status_url"]).json()
        assert detail["status"] == "completed"
        assert client.get(path).json()[0]["evaluation_id"] == receipt["evaluation_id"]
        assert client.get(detail["report_url"]).json()["total"] == 12
        assert client.get(receipt["events_url"]).text.count("event: evaluation.accepted") == 1
        stopped = client.post(receipt["status_url"] + "/cancel", headers=headers)
        assert stopped.json()["status"] == "completed"
        assert (
            client.get("/api/runs/" + receipt["run_id"]).json()["execution"]["kind"] == "evaluation"
        )
        assert client.get(path + "?limit=0").status_code == 422


async def test_artifact_namespace_symlink_rejected_before_fixture_write(tmp_path):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (host.state_dir / "evaluations").symlink_to(foreign, target_is_directory=True)
    try:
        run_id = await admit(controller)
        with pytest.raises(ValueError, match="path changed"):
            await controller.start(run_id)
        assert list(foreign.iterdir()) == []
        assert controller.detail(run_id)["status"] == "failed"
    finally:
        await host.close()


async def test_repeated_running_cancel_drains_real_runner_and_preserves_partial_report(
    tmp_path, monkeypatch,
):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    entered = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    original_close = RuntimeHost.close

    async def waiting_case(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def slow_close(runtime):
        if runtime is not host:
            closing.set()
            await release.wait()
        await original_close(runtime)

    monkeypatch.setattr("eventide.evaluation._run_case", waiting_case)
    monkeypatch.setattr(RuntimeHost, "close", slow_close)
    try:
        run_id = await admit(controller)
        controller.start(run_id)
        await entered.wait()
        first = asyncio.create_task(controller.cancel(run_id, "first"))
        await closing.wait()
        second = asyncio.create_task(controller.cancel(run_id, "second"))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert host.dispatch.scheduler.counts()["active_roots"] == 1
        release.set()
        results = await asyncio.gather(first, second)
        assert all(item["status"] == "interrupted" for item in results)
        detail = controller.detail(run_id)
        assert (detail["attempted"], detail["failed"], detail["remaining"]) == (1, 1, 11)
        assert detail["cases"][0]["status"] == "interrupted"
        report = controller.report(run_id)
        assert report["status"] == "cancelled"
        assert report["provenance"]["split"] == "development"
        assert Path(report["results"][0]["case_artifact_dir"], "case-result.json").is_file()
        assert host.store.usage_events(run_id=run_id) == []
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        release.set()
        await host.close()


def test_proxy_peer_report_get_needs_no_origin_but_mutations_do(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    host.settings = replace(host.settings, local_proxy_peers=("172.18.0.1",))

    async def fake_run(path, **kwargs):
        report = {"passed": 0, "total": 12}
        kwargs["progress"]("report_saved", report)
        return report

    monkeypatch.setattr("eventide.evaluation.run_evaluations", fake_run)
    with TestClient(create_app(host), base_url="http://localhost:8000",
                    client=("172.18.0.1", 1234)) as client:
        route = f"/api/projects/{host.default_workspace.project_id}/evaluations"
        headers = {"Idempotency-Key": "proxy"}
        assert client.post(route, json=payload(), headers=headers).status_code == 403
        created = client.post(route, json=payload(), headers={
            **headers, "Origin": "http://localhost:8000",
        })
        assert created.status_code == 202
        detail = client.get(created.json()["status_url"]).json()
        assert client.get(detail["report_url"]).status_code == 200


@pytest.mark.parametrize("kind", ["tracked", "symlink", "other_state"])
def test_evaluation_exclusion_does_not_hide_source_files(tmp_path, kind):
    from eventide.workspace import git

    repo = make_repo(tmp_path / "source")
    state = repo / ".state"
    artifact = state / "evaluations" / "code.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("original")
    if kind == "tracked":
        git(repo, "add", ".state/evaluations/code.py")
        git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-m", "tracked source")
    host = RuntimeHost(settings_for(repo, state_dir=state), ScriptedProvider([]))
    try:
        before = host._workspace_checkpoint(repo)
        if kind == "symlink":
            artifact.unlink()
            artifact.symlink_to(tmp_path / "foreign.py")
        elif kind == "other_state":
            (state / "customer.py").write_text("must stay visible")
        else:
            artifact.write_text("changed")
        assert host._workspace_checkpoint(repo) != before
    finally:
        asyncio.run(host.close())
