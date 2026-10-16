"""Evaluation Skills are explicit frozen inputs, never fabricated load evidence."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.evaluation import run_evaluations
from eventide.evaluation_control import EvaluationController
from eventide.evaluation_profiles import ENGINEERING_SKILLS, EvaluationProfile, evaluation_profile
from eventide.evaluation_quote import evaluation_quote
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_evaluation_control import host_for, payload
from tests.test_evaluation_evidence import _case, _suite
from tests.test_runtime import settings_for


def test_preset_quote_is_read_only_and_binds_content(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    project = host.default_workspace.project_id
    try:
        before = host.store.facts_by_type(("control.session", "skill.installed", "run.admitted"))
        plain = evaluation_quote(host, project)
        engineering = evaluation_quote(host, project, preset="engineering")
        assert plain["quote_hash"] != engineering["quote_hash"]
        assert [s["name"] for s in engineering["profile"]["skills"]] == list(ENGINEERING_SKILLS)
        assert not (host.state_dir / "skill-packages").exists()
        assert (
            host.store.facts_by_type(("control.session", "skill.installed", "run.admitted"))
            == before
        )
        assert host.provider.requests == []
        directories = []
        from eventide.skills import bundled_skill_directories

        for source in bundled_skill_directories():
            if source.name in ENGINEERING_SKILLS:
                destination = tmp_path / "updated" / source.name
                shutil.copytree(source, destination)
                directories.append(destination)
        (directories[0] / "SKILL.md").write_text(
            (directories[0] / "SKILL.md").read_text() + "\nUpdate.\n"
        )
        monkeypatch.setattr(
            "eventide.evaluation_profiles.bundled_skill_directories", lambda: directories
        )
        assert (
            evaluation_quote(host, project, preset="engineering")["quote_hash"]
            != engineering["quote_hash"]
        )
        with pytest.raises(ValueError, match="preset"):
            evaluation_profile("invalid")
        with pytest.raises(ValueError, match="match"):
            evaluation_quote(host, project, preset="engineering", profile=EvaluationProfile())
    finally:
        host.store.close()
        host._lease.close()


async def test_queued_profile_uses_received_bytes_after_bundled_update(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    roots = []
    captured = []
    try:
        for _ in range(4):
            roots.append(
                await host.admit_run(
                    RunRequest(
                        "read",
                        host.create_session(),
                        mode="plan",
                        execution_target="snapshot",
                    )
                )
            )
        await controller.admit(
            host.default_workspace.project_id, "pinned", **payload(), preset="engineering"
        )
        accepted = controller._accepted("pinned")["profile"]
        assert host.skills.installed() == []  # Frozen files alone are not activation.
        assert EvaluationProfile.restore(host.skills.packages, accepted).snapshot() == accepted

        def forbidden():
            raise AssertionError("Reloaded bundled Skill source after acceptance")

        monkeypatch.setattr("eventide.evaluation_profiles.bundled_skill_directories", forbidden)

        async def capture(*args, **kwargs):
            captured.append(kwargs["profile"].snapshot())
            raise ValueError("stop after observing frozen inputs")

        monkeypatch.setattr("eventide.evaluation.run_evaluations", capture)
        task = controller.start("pinned")
        assert controller.detail("pinned")["dispatch_state"] == "queued"
        await host.cancel_run(roots.pop())
        with pytest.raises(ValueError, match="stop after observing"):
            await task
        assert controller.detail("pinned")["status"] == "failed"
        assert captured == [accepted]
        assert controller.detail("pinned")["profile"] == accepted
        assert host.provider.requests == []
    finally:
        await host.close()


async def test_twenty_frozen_cases_with_engineering_preset_do_not_fake_loads(tmp_path):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    original = host._workspace_checkpoint(host.settings.workdir)
    try:
        await controller.admit(
            host.default_workspace.project_id, "engineering", **payload("all"), preset="engineering"
        )
        result = await controller.start("engineering")
        assert result.status == "completed"
        report = controller.report("engineering")
        assert report["passed"] == report["total"] == 20
        assert report["provenance"]["profile"] == controller.detail("engineering")["profile"]
        for case in report["results"]:
            main = next(r for r in case["evidence"]["runs"] if r["run_id"] == case["run_id"])
            snapshot = main["skill_snapshot"]
            assert snapshot["selected"] == [f"user:{n}" for n in ENGINEERING_SKILLS]
            assert {s["scope"] for s in snapshot["skills"]} == {"user"}
            assert [s["name"] for s in snapshot["skills"]] == list(ENGINEERING_SKILLS)
            assert not main.get("loaded_skills")
        assert host._workspace_checkpoint(host.settings.workdir) == original
        assert host.provider.requests == []
        assert host.store.usage_events(run_id="engineering") == []
    finally:
        await host.close()


async def test_model_loads_only_requested_skill_from_selected_profile(isolated_workspace):
    profile = evaluation_profile("engineering")
    case = _case(
        script=[
            {
                "tool_calls": [
                    {"id": "load", "name": "load_skill", "arguments": {"name": "test-repair"}}
                ]
            },
            {"text": "done"},
        ]
    )
    report = await run_evaluations(
        _suite(isolated_workspace, [case]),
        settings=settings_for(isolated_workspace),
        profile=profile,
    )
    assert report["passed"] == 1
    case = report["results"][0]
    main = next(r for r in case["evidence"]["runs"] if r["run_id"] == case["run_id"])
    assert len(main["skill_snapshot"]["selected"]) == 3
    assert len(main["loaded_skills"]) == 1
    loaded = main["loaded_skills"][0]
    assert loaded["skill_id"] == "user:test-repair"
    expected = next(p for p in profile.packages if p.name == "test-repair")
    assert loaded["version"] == expected.version
    assert loaded["characters"] > 0
    assert not (isolated_workspace / "skills").exists()


def test_http_preset_quote_mismatch_and_default_none_request_identity(tmp_path):
    host = host_for(tmp_path)
    project = host.default_workspace.project_id
    with TestClient(create_app(host)) as client:
        path = f"/api/projects/{project}/evaluations"
        quote = client.get(path + "/quote").json()
        wrong = {
            **payload(),
            "mode": "live",
            "preset": "engineering",
            "quote_hash": quote["quote_hash"],
            "confirmed": True,
        }
        rejected = client.post(path, json=wrong, headers={"Idempotency-Key": "wrong-profile"})
        assert rejected.status_code == 409
        assert rejected.headers["X-Eventide-Mutation-Outcome"] == "rejected"
        assert client.get(path).json() == []
        ordinary = client.post(path, json=payload(), headers={"Idempotency-Key": "old-default"})
        explicit = client.post(
            path, json={**payload(), "preset": "none"}, headers={"Idempotency-Key": "old-default"}
        )
        assert ordinary.status_code == explicit.status_code == 202
        assert ordinary.json() == explicit.json()
        changed = client.post(
            path,
            json={**payload(), "preset": "engineering"},
            headers={"Idempotency-Key": "old-default"},
        )
        assert changed.status_code == 409
        assert host.provider.requests == []


def test_restore_checks_exact_manifest_and_supports_old_absent_profile(tmp_path):
    host = host_for(tmp_path)
    try:
        profile = evaluation_profile("engineering")
        profile.persist(host.skills.packages)
        restored = EvaluationProfile.restore(host.skills.packages, profile.snapshot())
        assert restored.snapshot() == profile.snapshot()
        assert EvaluationProfile.restore(host.skills.packages, None) == EvaluationProfile()
        tampered = json.loads(json.dumps(profile.snapshot()))
        tampered["skills"][0]["manifest"][0]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="fingerprint"):
            EvaluationProfile.restore(host.skills.packages, tampered)
    finally:
        host.store.close()
        host._lease.close()


async def test_restart_preserves_profile_without_replaying_or_adopting_new_sources(tmp_path):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    settings = host.settings
    await controller.admit(
        host.default_workspace.project_id, "restart-profile", **payload(), preset="engineering"
    )
    snapshot = controller.detail("restart-profile")["profile"]
    await host.close()
    reopened = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        controller = EvaluationController(reopened)
        detail = controller.detail("restart-profile")
        assert detail["status"] == "interrupted" and detail["profile"] == snapshot
        assert EvaluationProfile.restore(reopened.skills.packages, snapshot).snapshot() == snapshot
        with pytest.raises(ValueError, match="awaiting"):
            controller.start("restart-profile")
        assert reopened.provider.requests == []
    finally:
        await reopened.close()


async def test_frozen_resource_tampering_fails_before_model_dispatch(tmp_path):
    host = host_for(tmp_path)
    controller = EvaluationController(host)
    try:
        await controller.admit(
            host.default_workspace.project_id, "tampered", **payload(), preset="engineering"
        )
        first = controller.detail("tampered")["profile"]["skills"][0]
        package = host.skills.packages.inspect_version(first["name"], first["version"])
        source = package.root / "SKILL.md"
        source.chmod(0o600)
        source.write_bytes(source.read_bytes() + b"\nchanged\n")
        with pytest.raises(ValueError):
            await controller.start("tampered")
        assert controller.detail("tampered")["status"] == "failed"
        assert host.provider.requests == []
        assert host.store.usage_events(run_id="tampered") == []
    finally:
        await host.close()
