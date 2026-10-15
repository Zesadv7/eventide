"""Skills management uses real package bytes and durable at-most-once receipts."""

import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.http_control import begin_mutation
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for
from tests.test_skill_runtime import call, package


def key(value="import-1"):
    return {"Idempotency-Key": value}


def test_import_configure_resource_and_restart_replay(isolated_workspace):
    source = package(isolated_workspace / "upload")
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    workspace = host.default_workspace.workspace_id
    route = f"/api/workspaces/{workspace}/skills"
    body = {"directory": str(source)}
    with TestClient(create_app(host)) as client:
        assert client.post(route + "/import", json=body).status_code == 422
        preview = client.post(route + "/preview", json=body)
        assert preview.json()["valid"]
        assert not host.skills.installed()
        imported = client.post(route + "/import", json=body, headers=key())
        assert imported.status_code == 200
        version = imported.json()["version"]
        assert client.post(route + "/import", json=body, headers=key()).json() == imported.json()
        assert len(host.store.facts_by_type(("skill.installed",))) == 1
        assert (
            client.post(
                route + "/import", json={"bundled": "code-review"}, headers=key()
            ).status_code
            == 409
        )
        assert client.get(route).json()["enabled"]["skills"] == []
        config = {"enabled_user": {"diagnose": version}, "selected": ["user:diagnose"]}
        assert (
            client.put(route + "/configuration", json=config, headers=key("enable")).status_code
            == 200
        )
        assert client.get(route).json()["enabled"]["selected"] == ["user:diagnose"]
        response = client.get(
            f"/api/skills/diagnose/versions/{version}/resource",
            params={"path": "references/check.md"},
        )
        assert response.json()["content"] == "version one"
        assert (
            client.get(
                f"/api/skills/diagnose/versions/{version}/resource", params={"path": "../outside"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/skills/diagnose/versions/diff", params={"before": version, "after": version}
            ).json()["changed"]
            == []
        )
    # A deleted import source cannot break an already completed request replay.
    (source / "SKILL.md").unlink()
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        assert client.post(route + "/import", json=body, headers=key()).json() == imported.json()
        assert client.get(route).json()["enabled"]["selected"] == ["user:diagnose"]


def test_pending_mutation_is_not_replayed_after_restart(isolated_workspace):
    from starlette.requests import Request

    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([]))
    workspace = host.default_workspace.workspace_id
    body = {
        "directory": None, "archive_base64": None, "bundled": "code-review",
        "scope": "user", "expected_version": None,
    }
    request = Request({"type": "http", "headers": [(b"idempotency-key", b"pending")]})
    with TestClient(create_app(host)):
        begin_mutation(host, request, host._skill_owner(workspace), "skills.import", body)
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.post(
            f"/api/workspaces/{workspace}/skills/import", json=body, headers=key("pending")
        )
        assert response.status_code == 409
        assert "unknown" in response.json()["detail"]
        assert not host.skills.installed()


def test_upload_diagnostics_bundled_and_origin_boundary(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    workspace = host.default_workspace.workspace_id
    route = f"/api/workspaces/{workspace}/skills"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape", "bad")
    body = {"archive_base64": base64.b64encode(buffer.getvalue()).decode()}
    with TestClient(create_app(host)) as client:
        preview = client.post(route + "/preview", json=body)
        assert preview.status_code == 200 and not preview.json()["valid"]
        assert preview.json()["diagnostics"]
        response = client.post(route + "/import", json=body, headers=key())
        assert response.status_code == 422
        assert client.post(route + "/import", json=body, headers=key()).json() == response.json()
        assert len(client.get("/api/skills/bundled").json()) == 3
        assert (
            client.post(
                route + "/import",
                json={"bundled": "code-review"},
                headers={**key("x"), "Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert not host.skills.installed()
        assert (
            client.post(
                route + "/import", json={"bundled": "code-review"}, headers=key("bundled")
            ).status_code
            == 200
        )
        assert client.post(route + "/preview", json={"archive_base64": "***"}).status_code == 422
        assert client.post(route + "/preview", json={"bundled": "missing"}).status_code == 422
        assert (
            client.post(route + "/preview", json={"directory": "x", "bundled": "x"}).status_code
            == 422
        )


def test_explicit_selected_skills_are_frozen_by_http_admission(isolated_workspace):
    package(isolated_workspace / "skills/diagnose")
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        session = client.post("/api/sessions", json={}).json()["session_id"]
        response = client.post(
            f"/api/sessions/{session}/runs",
            json={
                "prompt": "check",
                "selected_skills": ["project:diagnose"],
            },
            headers=key(),
        )
        assert response.status_code == 202
        admitted = host._admitted_intent(response.json()["run_id"])
        assert admitted["skill_snapshot"]["selected"] == ["project:diagnose"]
        frozen = client.get(f"/api/runs/{response.json()['run_id']}/skills").json()
        assert frozen["snapshot"]["selected"] == ["project:diagnose"]


def test_configuration_conflicts_and_broken_pin_can_be_repaired(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    route = f"/api/workspaces/{host.default_workspace.workspace_id}/skills"
    with TestClient(create_app(host)) as client:
        old = client.get(route).json()["configuration_hash"]
        imported = client.post(
            route + "/import", json={"bundled": "code-review"}, headers=key()
        ).json()
        body = {
            "expected_configuration_hash": old,
            "enabled_user": {"code-review": imported["version"]},
        }
        assert client.put(route + "/configuration", json=body, headers=key("a")).status_code == 200
        stale = client.put(
            route + "/configuration", json={"expected_configuration_hash": old}, headers=key("b")
        )
        assert stale.status_code == 409
        assert "refresh" in stale.json()["detail"]
        path = host.skills.packages.root / "code-review" / imported["version"] / "SKILL.md"
        path.chmod(0o600)
        path.write_text("damaged")
        broken = client.get(route).json()
        assert broken["enabled"]["diagnostics"][0]["code"] == "invalid_configuration"
        response = client.put(
            route + "/configuration",
            json={"expected_configuration_hash": broken["configuration_hash"]}, headers=key("c"),
        )
        assert response.status_code == 200
        assert not client.get(route).json()["enabled"]["diagnostics"]


def test_loaded_records_are_durable_and_separate_from_selection(isolated_workspace):
    package(isolated_workspace / "skills/diagnose")
    settings = settings_for(isolated_workspace)
    host = RuntimeHost(settings, ScriptedProvider([
        call("load_skill", {"name": "diagnose"}, "load"), {"text": "done"},
    ]))
    with TestClient(create_app(host)) as client:
        session = client.post("/api/sessions", json={}).json()["session_id"]
        run = client.post(
            f"/api/sessions/{session}/runs", json={"prompt": "check"}, headers=key(),
        ).json()["run_id"]
        # TestClient portal waits on the actual owned task, not an arbitrary sleep.
        async def wait():
            tasks = list(host._active_tasks)
            if tasks:
                import asyncio
                await asyncio.gather(*tasks)
        client.portal.call(wait)
        record = client.get(f"/api/runs/{run}/skills").json()
        assert record["snapshot"]["selected"] == []
        assert record["loaded"][0]["skill_id"] == "project:diagnose"
        assert record["loaded"][0]["version"] == record["snapshot"]["skills"][0]["version"]
    host = RuntimeHost(settings, ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        assert client.get(f"/api/runs/{run}/skills").json() == record
        assert client.get("/api/runs/missing/skills").status_code == 404


@pytest.mark.parametrize("scope", ["user", "project"])
def test_import_rejects_changes_since_review(isolated_workspace, scope):
    source = package(isolated_workspace / "upload")
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    route = f"/api/workspaces/{host.default_workspace.workspace_id}/skills"
    body = {"directory": str(source), "scope": scope}
    with TestClient(create_app(host)) as client:
        reviewed = client.post(route + "/preview", json=body).json()
        (source / "references/check.md").write_text("changed after preview")
        response = client.post(
            route + "/import", json={**body, "expected_version": reviewed["version"]},
            headers=key(),
        )
        assert response.status_code == 422
        assert "changed since preview" in response.json()["detail"]
        assert not host.skills.installed()
        assert not (isolated_workspace / "skills/diagnose").exists()
        reviewed = client.post(route + "/preview", json=body).json()
        response = client.post(
            route + "/import", json={**body, "expected_version": reviewed["version"]},
            headers=key("reviewed-again"),
        )
        assert response.status_code == 200
        assert response.json()["version"] == reviewed["version"]


@pytest.mark.parametrize("bad", ["", "x" * 129, "key with spaces"])
def test_invalid_mutation_keys_are_rejected(isolated_workspace, bad):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.post(
            f"/api/workspaces/{host.default_workspace.workspace_id}/skills/import",
            json={"bundled": "code-review"},
            headers=key(bad),
        )
        assert response.status_code == 422
        assert not host.skills.installed()
