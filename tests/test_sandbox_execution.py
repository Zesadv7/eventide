"""Optional sandbox integration; no Docker or provider network required."""

from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.sandbox_profile import LIMITS
from tests.test_host import make_repo
from tests.test_runtime import settings_for

IMAGE = "sha256:" + "a" * 64
PROFILE = {**LIMITS, "image": IMAGE, "requested_image": "tools:local"}


@pytest.fixture
def sandbox_admission(monkeypatch):
    calls = []

    async def resolve(image, root):
        calls.append(image)
        return {**PROFILE, "requested_image": image}

    monkeypatch.setattr("eventide.host.resolve_sandbox_profile", resolve)
    return calls


def make_host(tmp_path, provider, **changes):
    root = make_repo(tmp_path / "source")
    (root / "code.py").write_text("original\n")
    (root / ".env").write_text("PRIVATE=never-mount\n")
    return RuntimeHost(
        replace(settings_for(root), state_dir=tmp_path / "state", **changes), provider
    )


def request(session=None, **changes):
    return RunRequest(
        "inspect",
        session_id=session,
        execution_profile="sandbox",
        sandbox_image="tools:local",
        **changes,
    )


async def test_profile_persisted_catalog_safe_and_source_untouched(tmp_path, sandbox_admission):
    provider = ScriptedProvider([{"text": "done"}])
    host = make_host(tmp_path, provider)
    try:
        result = await host.run(request())
        assert result.status == "completed"
        intent = host._admitted_intent(result.run_id)
        assert intent["execution_profile_config"] == PROFILE
        root, _ = host._execution_paths(intent["execution_binding"])
        assert root != host.settings.workdir
        assert not (root / ".git").exists()
        assert not (root / ".env").exists()
        names = {tool["name"] for tool in provider.requests[0].tools}
        assert names == {
            "bash",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "compact",
            "read_tool_result",
            "todo_write",
        }
        assert "local host" not in str(provider.requests[0].tools)
        assert (host.settings.workdir / "code.py").read_text() == "original\n"
        state = host.session_status(result.session_id)
        assert state["execution_profile"] == "sandbox"
        assert state["sandbox_profile"] == PROFILE
        assert sandbox_admission == ["tools:local", IMAGE]
    finally:
        await host.close()


async def test_plan_filters_side_effects_and_hallucinated_worker(tmp_path, sandbox_admission):
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {"id": "bad", "name": "delegate_readonly", "arguments": {"tasks": []}}
                ]
            },
            {"text": "done"},
        ]
    )
    host = make_host(tmp_path, provider)
    try:
        result = await host.run(request(mode="plan"))
        assert result.status == "completed"
        names = {tool["name"] for tool in provider.requests[0].tools}
        assert names == {"read_file", "glob", "compact", "read_tool_result", "todo_write"}
        events = host.store.run_events(result.run_id)
        assert not any(e["type"].startswith("worker.") for e in events)
        assert any(e["type"] == "tool.completed" and e["payload"]["is_error"] for e in events)
    finally:
        await host.close()


async def test_session_boundary_frozen_and_followup_reuses_copy(tmp_path, sandbox_admission):
    provider = ScriptedProvider([{"text": "first"}, {"text": "second"}])
    host = make_host(tmp_path, provider)
    try:
        first = await host.run(request())
        binding = host._execution_binding(first.run_id)
        root, _ = host._execution_paths(binding)
        (root / "code.py").write_text("candidate\n")
        (host.settings.workdir / "code.py").write_text("source drift\n")
        second = await host.run(
            RunRequest("continue task", first.session_id, execution_profile="sandbox")
        )
        assert second.status == "completed"
        assert host._execution_binding(second.run_id) == binding
        assert (root / "code.py").read_text() == "candidate\n"
        with pytest.raises(ValueError, match="fixed for this Session"):
            await host.run(RunRequest("local", first.session_id))
        with pytest.raises(ValueError, match="image is fixed"):
            await host.run(
                RunRequest(
                    "change",
                    first.session_id,
                    execution_profile="sandbox",
                    sandbox_image="other:local",
                )
            )
    finally:
        await host.close()


async def test_local_session_cannot_silently_switch(tmp_path, sandbox_admission):
    host = make_host(tmp_path, ScriptedProvider([{"text": "local"}]))
    try:
        first = await host.run(RunRequest("local"))
        with pytest.raises(ValueError, match="fixed for this Session"):
            await host.run(request(first.session_id))
    finally:
        await host.close()


async def test_sandbox_failure_never_calls_provider(tmp_path, monkeypatch):
    async def fail(*args):
        raise ValueError("Docker unavailable")

    monkeypatch.setattr("eventide.host.resolve_sandbox_profile", fail)
    provider = ScriptedProvider([{"text": "should not happen"}])
    host = make_host(tmp_path, provider)
    try:
        with pytest.raises(ValueError, match="Docker unavailable"):
            await host.run(request())
        assert not provider.requests
    finally:
        await host.close()


async def test_continue_preserves_pinned_profile_and_candidate(tmp_path, sandbox_admission):
    host = make_host(
        tmp_path,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "plan",
                            "name": "todo_write",
                            "arguments": {
                                "todos": [{"content": "Inspect code", "status": "in_progress"}],
                            },
                        }
                    ],
                }
            ]
        ),
        max_steps=1,
    )
    try:
        first = await host.run(request())
        assert first.status == "interrupted"
        host.provider = ScriptedProvider([{"text": "done"}])
        second = await host.continue_session(first.session_id)
        assert second.status == "completed"
        assert host._admitted_intent(second.run_id)["execution_profile_config"] == PROFILE
        assert host._execution_binding(second.run_id) == host._execution_binding(first.run_id)
    finally:
        await host.close()


@pytest.mark.parametrize("name,readonly", [("bash", False), ("read_file", True)])
async def test_crash_quarantines_container_tools_without_blocking_source(
    tmp_path,
    sandbox_admission,
    name,
    readonly,
):
    host = make_host(tmp_path, ScriptedProvider([]))
    try:
        run_id = await host.admit_run(request())
        record = host.store.get_run(run_id)
        binding = host._execution_binding(run_id)
        host.store.append_event(run_id, "run.started", {"mode": "auto"})
        host.store.append_event(
            run_id,
            "tool.prepared",
            {
                "call_id": "unfinished",
                "name": name,
                "arguments": {},
                "readonly": readonly,
            },
        )
        host._recover_interrupted_runs()
        host._release_admission(run_id)
        settings = host.settings
        await host.close()
        host = RuntimeHost(settings, ScriptedProvider([]))
        unknown = [
            e["payload"]
            for e in host.store.run_events(run_id)
            if e["type"] == "tool.outcome_unknown"
        ]
        assert len(unknown) == 1
        assert unknown[0]["workspace_id"] == f"sandbox:{binding['sandbox_id']}"
        assert not host.store.workspace_has_unknown_outcome(host.default_workspace.workspace_id)
        with pytest.raises(ValueError, match="unresolved tool outcome"):
            await host.admit_continue(record["session_id"], "refused")
        await host.abandon_interruption(record["session_id"], expected_run_id=run_id)
        with pytest.raises(ValueError, match="unresolved tool outcome"):
            await host.run(request(record["session_id"]))
    finally:
        await host.close()


async def test_cleanup_unknown_quarantines_only_candidate(tmp_path, sandbox_admission, monkeypatch):
    from eventide.container_executor import ContainerCleanupError

    async def uncertain(*args):
        raise ContainerCleanupError("cleanup not confirmed")

    monkeypatch.setattr("eventide.container_executor.ContainerCommandExecutor.execute", uncertain)
    host = make_host(
        tmp_path,
        ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}
                    ],
                }
            ]
        ),
    )
    try:
        result = await host.run(request(mode="plan"))
        assert result.status == "interrupted"
        unknown = [
            e for e in host.store.run_events(result.run_id) if e["type"] == "tool.outcome_unknown"
        ]
        assert len(unknown) == 1
        assert not host.store.workspace_has_unknown_outcome(host.default_workspace.workspace_id)
        with pytest.raises(ValueError, match="unresolved tool outcome"):
            await host.admit_continue(result.session_id, "refused")
    finally:
        await host.close()


def test_http_profile_and_complete_current_candidate_export(tmp_path, sandbox_admission):
    import uuid

    from fastapi.testclient import TestClient

    from eventide.api import create_app
    from tests.test_api import wait_for_run

    host = make_host(tmp_path, ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        session = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["session_id"]
        assert client.get(f"/api/sessions/{session}").json()["execution_profile"] is None
        key = {"Idempotency-Key": uuid.uuid4().hex}
        body = {"prompt": "inspect", "execution_profile": "sandbox", "sandbox_image": "tools:local"}
        accepted = client.post(f"/api/sessions/{session}/runs", json=body, headers=key)
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        result = wait_for_run(client, run_id)
        assert result["status"] == "completed"
        assert result["execution"]["profile"] == PROFILE
        assert (
            client.post(f"/api/sessions/{session}/runs", json=body, headers=key).json()["run_id"]
            == run_id
        )
        altered = {**body, "sandbox_image": "other:local"}
        assert (
            client.post(f"/api/sessions/{session}/runs", json=altered, headers=key).status_code
            == 409
        )
        binding = host._execution_binding(run_id)
        root, _ = host._execution_paths(binding)
        (root / "code.py").write_text("candidate\n")
        exported = client.get(f"/api/runs/{run_id}/sandbox-diff")
        assert exported.status_code == 200
        assert "-original" in exported.text and "+candidate" in exported.text
        assert ".env" not in exported.text and str(root) not in exported.text
        assert exported.headers["cache-control"] == "no-store"
        assert (
            client.get(
                f"/api/runs/{run_id}/sandbox-diff",
                headers={
                    "Origin": "https://untrusted.example",
                },
            ).status_code
            == 403
        )
        (root / "unsafe").symlink_to(tmp_path)
        assert client.get(f"/api/runs/{run_id}/sandbox-diff").status_code == 422


def test_local_admission_keeps_legacy_idempotency_fingerprint(tmp_path):
    import uuid

    from fastapi.testclient import TestClient

    from eventide.api import create_app
    from eventide.http_api.schemas import RunBody
    from eventide.workspace import digest
    from tests.test_api import wait_for_run

    host = make_host(tmp_path, ScriptedProvider([{"text": "local"}]))
    with TestClient(create_app(host)) as client:
        session = client.post(
            "/api/sessions",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["session_id"]
        body = {"prompt": "local"}
        key = uuid.uuid4().hex
        accepted = client.post(
            f"/api/sessions/{session}/runs",
            json=body,
            headers={"Idempotency-Key": key},
        )
        run_id = accepted.json()["run_id"]
        assert wait_for_run(client, run_id)["status"] == "completed"
        legacy = RunBody(**body).model_dump(
            exclude={"execution_profile", "sandbox_image", "knowledge_index",
                     "code_structure", "source_scope", "native_profile", "native_backend"},
        )
        legacy["generation"] = {}
        assert host._admitted_intent(run_id)["request_fingerprint"] == digest(
            [session, "run", legacy]
        )
        replay = client.post(
            f"/api/sessions/{session}/runs",
            json={**body, "knowledge_index": "existing"},
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 202 and replay.json()["run_id"] == run_id
        changed = client.post(
            f"/api/sessions/{session}/runs",
            json={**body, "knowledge_index": "prepare"},
            headers={"Idempotency-Key": key},
        )
        assert changed.status_code == 409
