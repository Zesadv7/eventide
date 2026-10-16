"""Web delivery controls share one root lease with their Writer descendants."""

import asyncio

from eventide.delivery_control import WebDeliveryController
from tests.test_delivery_repair import done, edit
from tests.test_delivery_runtime import CHECK, delivery_host


async def test_prepare_repair_apply_use_durable_operations_and_shared_root(tmp_path):
    host = delivery_host(tmp_path, [edit("price + fraction", "price * (1 - fraction)"), done()])
    controller = WebDeliveryController(host)
    project = host.default_workspace.project_id
    try:
        delivery = await controller.admit(project, "prepare", action="prepare", name="web-delivery")
        await controller.start("prepare")
        assert controller.operation("prepare")["status"] == "completed"
        assert controller.list(project)[0]["delivery_id"] == delivery
        await controller.admit(
            project,
            "repair",
            action="repair",
            delivery_id=delivery,
            goal="Repair discount",
            checks=[CHECK],
        )
        await asyncio.wait_for(controller.start("repair"), 20)
        value = controller.delivery.load(delivery)
        assert value["repair_status"] == "passed"
        assert value["repair_result"]["usage"]["attempt_count"] == 2
        writer = value["rounds"][0]["worker_run_id"]
        assert host._admitted_intent(writer)["parent_run_id"] == "repair"
        assert not any(entry.run_id == writer for entry in host.dispatch.scheduler.entries())
        assert "price + fraction" in (host.settings.workdir / "calc.py").read_text()
        assert "price * (1 - fraction)" in controller.patch(delivery)
        await controller.admit(
            project,
            "apply",
            action="apply",
            delivery_id=delivery,
            patch_sha256=value["verification"]["patch_sha256"],
        )
        await controller.start("apply")
        assert controller.delivery.load(delivery)["applied"]
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
        assert host.store.workspace_execution_config(host.default_workspace.workspace_id) == {}
    finally:
        await host.close()


async def test_four_control_roots_do_not_deadlock_waiting_for_writers(tmp_path):
    from eventide.models import ModelResponse, ToolCall

    host = delivery_host(tmp_path)
    controller = WebDeliveryController(host)
    project = host.default_workspace.project_id
    entered = 0
    overlap = asyncio.Event()

    async def provider(request):
        nonlocal entered
        if any(
            block.get("type") == "tool_result"
            for message in request.messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
        ):
            return ModelResponse("done", usage={"input_tokens": 2, "output_tokens": 1})
        entered += 1
        if entered >= 2:
            overlap.set()
        await asyncio.wait_for(overlap.wait(), 5)
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "fix",
                    "edit_file",
                    {
                        "path": "calc.py",
                        "old_text": "price + fraction",
                        "new_text": "price * (1 - fraction)",
                    },
                ),
            ),
            usage={"input_tokens": 2, "output_tokens": 1},
        )

    host.provider.complete = provider
    try:
        deliveries = []
        for n in range(4):
            identity = await controller.admit(
                project, f"prepare{n}", action="prepare", name=f"web{n}"
            )
            await controller.start(f"prepare{n}")
            deliveries.append(identity)
        tasks = []
        for n, identity in enumerate(deliveries):
            await controller.admit(
                project,
                f"repair{n}",
                action="repair",
                delivery_id=identity,
                goal="repair",
                checks=[CHECK],
            )
            tasks.append(controller.start(f"repair{n}"))
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
        assert entered == 4
        for n, identity in enumerate(deliveries):
            value = controller.delivery.load(identity)
            assert value["repair_status"] == "passed"
            writer = value["rounds"][0]["worker_run_id"]
            from eventide.model_budget import ModelBudgetController

            _, context = ModelBudgetController(host.store).context(writer)
            assert context.root_run_id == f"repair{n}"
            assert context.delivery_id == identity
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        await host.close()


async def test_queued_delivery_cancel_and_restart_never_replay_writes(tmp_path):
    import pytest

    from eventide.host import RuntimeHost
    from eventide.models import RunRequest
    from eventide.providers import ScriptedProvider

    host = delivery_host(tmp_path)
    controller = WebDeliveryController(host)
    settings = host.settings
    project = host.default_workspace.project_id
    try:
        for _ in range(4):
            await host.admit_run(
                RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
            )
        await controller.admit(project, "queued", action="prepare", name="never-created")
        task = controller.start("queued")
        await host.cancel_run("queued")
        assert task.cancelled()
        assert controller.operation("queued")["status"] == "interrupted"
        assert controller.list(project) == []
        assert host.store.usage_events(run_id="queued") == []
    finally:
        await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        await restored.initialize()
        controller = WebDeliveryController(restored)
        assert controller.operation("queued")["status"] == "interrupted"
        assert controller.list(project) == []
        with pytest.raises(ValueError, match="control|Control|ordinary"):
            await restored.continue_session(controller.operation("queued")["session_id"])
    finally:
        await restored.close()


def test_http_delivery_round_trip_idempotency_and_reviewed_hash(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from eventide.api import create_app

    host = delivery_host(tmp_path, [edit("price + fraction", "price * (1 - fraction)"), done()])
    project = host.default_workspace.project_id
    with TestClient(create_app(host)) as client:

        def wait(receipt):
            for _ in range(300):
                value = client.get(receipt["status_url"]).json()
                if value["status"] not in {"running", "queued"}:
                    assert value["status"] == "completed", value
                    return value
                time.sleep(0.05)
            raise AssertionError("Delivery operation did not finish")

        url = f"/api/projects/{project}/deliveries"
        body = {"name": "http-delivery"}
        assert client.post(url, json=body).status_code == 422
        assert (
            client.post(
                url,
                json=body,
                headers={"Origin": "https://evil.invalid", "Idempotency-Key": "prepare"},
            ).status_code
            == 403
        )
        response = client.post(url, json=body, headers={"Idempotency-Key": "prepare"})
        assert response.status_code == 202, response.text
        prepared = response.json()
        wait(prepared)
        assert (
            client.post(url, json=body, headers={"Idempotency-Key": "prepare"}).json() == prepared
        )
        assert (
            client.post(
                url, json={"name": "different"}, headers={"Idempotency-Key": "prepare"}
            ).status_code
            == 409
        )
        identity = prepared["delivery_id"]
        base = f"/api/deliveries/{identity}"
        assert client.get(base + "/patch").status_code == 409
        repair = client.post(
            base + "/repair",
            json={"goal": "repair", "checks": [CHECK]},
            headers={"Idempotency-Key": "repair"},
        )
        assert repair.status_code == 202, repair.text
        wait(repair.json())
        patch = client.get(base + "/patch").json()
        assert "price * (1 - fraction)" in patch["patch"]
        rejected = client.post(
            base + "/apply", json={"patch_sha256": "0" * 64}, headers={"Idempotency-Key": "wrong"}
        )
        assert rejected.status_code == 409
        assert rejected.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        applied = client.post(
            base + "/apply",
            json={"patch_sha256": patch["patch_sha256"]},
            headers={"Idempotency-Key": "apply"},
        )
        assert applied.status_code == 202, applied.text
        wait(applied.json())
        assert client.get(base).json()["applied"] is True
        history = client.get(f"/api/projects/{project}/delivery-operations?limit=2").json()
        assert len(history) == 2
        assert history[0]["run_id"] == applied.json()["run_id"]
        older = client.get(
            f"/api/projects/{project}/delivery-operations", params={"before": history[-1]["run_id"]}
        ).json()
        assert older[0]["run_id"] == prepared["run_id"]
        assert (
            client.post(
                base + "/apply",
                json={"patch_sha256": patch["patch_sha256"]},
                headers={"Idempotency-Key": "apply"},
            ).json()
            == applied.json()
        )


async def test_state_root_alias_is_canonical_but_nested_artifact_links_are_rejected(tmp_path):
    from dataclasses import replace

    import pytest

    from eventide.delivery_runtime import DeliveryController
    from eventide.host import RuntimeHost
    from eventide.providers import ScriptedProvider

    original = delivery_host(tmp_path)
    settings = original.settings
    await original.close()
    alias = tmp_path / "state-alias"
    alias.symlink_to(settings.state_dir, target_is_directory=True)
    host = RuntimeHost(replace(settings, state_dir=alias), ScriptedProvider([]))
    try:
        controller = DeliveryController(host)
        artifact = controller._artifact("delivery_aaaaaaaaaaaaaaaa", "a" * 64)
        assert artifact.resolve().is_relative_to(settings.state_dir.resolve())
        outside = tmp_path / "outside"
        outside.mkdir()
        (alias / "deliveries").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="symbolic"):
            controller._artifact("delivery_aaaaaaaaaaaaaaaa", "a" * 64)
    finally:
        await host.close()


async def test_web_cancel_retains_root_until_owned_writer_cleanup(tmp_path):
    import pytest

    host = delivery_host(tmp_path)
    controller = WebDeliveryController(host)
    project = host.default_workspace.project_id
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(request):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await release.wait()

    host.provider.complete = pending
    try:
        identity = await controller.admit(project, "prepare", action="prepare", name="cancel-web")
        await controller.start("prepare")
        await controller.admit(
            project, "repair", action="repair", delivery_id=identity, goal="repair", checks=[CHECK]
        )
        task = controller.start("repair")
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert host.dispatch.scheduler.counts()["active_roots"] == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        value = controller.delivery.load(identity)
        assert value["repair_result"]["reason"] == "cancelled"
        assert value["repair_result"]["usage"]["unknown_attempts"] == 1
        assert host.dispatch.scheduler.counts()["active_roots"] == 0
    finally:
        release.set()
        await host.close()


def test_http_queued_cancel_missing_identity_and_admission_conflict(tmp_path):
    from fastapi.testclient import TestClient

    from eventide.api import create_app
    from eventide.models import RunRequest

    host = delivery_host(tmp_path)
    project = host.default_workspace.project_id
    with TestClient(create_app(host)) as client:

        async def occupy():
            for _ in range(4):
                await host.admit_run(
                    RunRequest(
                        "read", host.create_session(), mode="plan", execution_target="snapshot"
                    )
                )

        client.portal.call(occupy)
        endpoint = f"/api/projects/{project}/deliveries"
        receipt = client.post(
            endpoint, json={"name": "queued"}, headers={"Idempotency-Key": "queued"}
        ).json()
        conflict = client.post(
            endpoint, json={"name": "other"}, headers={"Idempotency-Key": "other"}
        )
        assert conflict.status_code == 409
        assert conflict.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        cancel = receipt["status_url"] + "/cancel"
        first = client.post(cancel, headers={"Idempotency-Key": "stop"})
        assert first.status_code == 200 and first.json()["status"] == "interrupted"
        assert client.post(cancel, headers={"Idempotency-Key": "stop"}).json() == first.json()
        assert host.dispatch.scheduler.counts()["active_roots"] == 4
        assert (
            len(
                [
                    e
                    for e in host.store.run_events(receipt["run_id"])
                    if e["type"] == "delivery.cancel_requested"
                ]
            )
            == 1
        )
        for path in (
            "/api/deliveries/missing",
            "/api/delivery-operations/missing",
            "/api/projects/missing/deliveries",
            "/api/projects/missing/delivery-operations",
        ):
            assert client.get(path).status_code == 404
        assert (
            client.post(
                "/api/delivery-operations/missing/cancel", headers={"Idempotency-Key": "none"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/projects/missing/deliveries",
                json={"name": "valid"},
                headers={"Idempotency-Key": "none"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/deliveries/missing/repair",
                json={"goal": "fix", "checks": [CHECK]},
                headers={"Idempotency-Key": "none"},
            ).status_code
            == 404
        )


async def test_writer_cannot_borrow_an_ordinary_or_closed_root(tmp_path):
    import pytest

    from eventide.delivery_control import validate_writer_parent
    from eventide.models import RunRequest

    host = delivery_host(tmp_path, [done()])
    controller = WebDeliveryController(host)
    project = host.default_workspace.project_id
    try:
        run = await host.admit_run(
            RunRequest("read", host.create_session(), mode="plan", execution_target="snapshot")
        )
        with pytest.raises(ValueError, match="trusted"):
            validate_writer_parent(host, run, host.default_workspace.workspace_id, {})
        identity = await controller.admit(project, "prepare", action="prepare", name="closed")
        await controller.start("prepare")
        with pytest.raises(ValueError, match="live root"):
            validate_writer_parent(
                host,
                "prepare",
                host.default_workspace.workspace_id,
                {"writer_delivery_id": identity},
            )
        with pytest.raises(ValueError, match="awaiting"):
            controller.start("prepare")
    finally:
        await host.close()


async def test_completed_web_repair_never_contaminates_new_source_runs(tmp_path):
    from eventide.host import RuntimeHost
    from eventide.models import RunRequest
    from eventide.providers import ScriptedProvider

    host = delivery_host(tmp_path, [edit("price + fraction", "price * (1 - fraction)"), done()])
    controller = WebDeliveryController(host)
    project = host.default_workspace.project_id
    settings = host.settings
    try:
        identity = await controller.admit(project, "prepare", action="prepare", name="inheritance")
        await controller.start("prepare")
        await controller.admit(project, "repair", action="repair", delivery_id=identity,
                               goal="repair", checks=[CHECK])
        await controller.start("repair")
        assert host.store.workspace_execution_config(host.default_workspace.workspace_id) == {}
    finally:
        await host.close()
    restored = RuntimeHost(settings, ScriptedProvider([done()]), enable_mcp=False)
    try:
        run = await restored.admit_run(RunRequest("read", restored.create_session(), mode="plan",
                                                  execution_target="snapshot"))
        assert "execution_config" not in restored._admitted_intent(run)
        result = await restored.start_admitted_run(run)
        assert result.status == "completed"
    finally:
        await restored.close()
