"""Source evidence excludes only explicit Runtime artifacts, never arbitrary source."""

import base64
import os
import time
from pathlib import Path

import pytest

from eventide import attachments
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from eventide.workspace import (
    changed_since,
    git,
    runtime_checkpoint_directories,
    runtime_checkpoint_files,
    workspace_checkpoint,
)
from tests.test_host import make_repo
from tests.test_readonly_workers import WorkerProvider
from tests.test_runtime import settings_for


def policy(repo: Path, state: Path):
    return {
        "exclude_untracked": runtime_checkpoint_files(state / "runtime.sqlite", state),
        "exclude_untracked_directories": runtime_checkpoint_directories(state),
    }


@pytest.mark.parametrize("relative", [".eventide", "var/private-state"])
@pytest.mark.parametrize("target", ["source", "snapshot"])
async def test_in_source_runtime_artifacts_do_not_create_admission_drift(
    isolated_workspace, relative, target,
):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "app.py").write_text("print('stable source')\n")
    state = repo / relative
    provider = ScriptedProvider([{"text": "done"}])
    host = RuntimeHost(settings_for(repo, state_dir=state), provider)
    try:
        session = host.create_session()
        attachment_id = attachments.new_attachment_id()
        attachments.save_attachment(
            state, session, attachment_id, "note.txt", base64.b64encode(b"inspect app").decode(),
        )
        run_id = await host.admit_run(RunRequest(
            "inspect", session, mode="plan", execution_target=target,
            attachment_ids=[attachment_id],
        ))
        host.create_session()  # More writes after the frozen source checkpoint.
        assert (await host.start_admitted_run(run_id)).status == "completed"
        assert len(provider.requests) == 1
        checkpoint = host.store.get_run(run_id)["checkpoint"]
        if target == "source":
            assert checkpoint["checkpoint_policy"]["version"] == 2
            assert [entry[0] for entry in checkpoint["untracked"]] == ["app.py"]
        else:
            binding = host._execution_binding(run_id)
            assert binding["source_checkpoint"]["checkpoint_policy"]["version"] == 2
            assert set(binding["manifest"]) == {"app.py"}
    finally:
        await host.close()


@pytest.mark.parametrize("name", ["new.py", "customer.db", "var/state/customer.db",
                                  "skills/check/SKILL.md", "other/attachments/source.txt"])
async def test_source_changes_are_not_hidden_by_runtime_exclusions(isolated_workspace, name):
    repo = make_repo(isolated_workspace / "repo")
    provider = ScriptedProvider([{"text": "must not execute"}])
    host = RuntimeHost(settings_for(repo, state_dir=repo / "var/state"), provider)
    try:
        run_id = await host.admit_run(RunRequest("inspect"))
        source = repo / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("new source\n")
        with pytest.raises(ValueError, match="Source Workspace changed"):
            await host.start_admitted_run(run_id)
        assert not provider.requests and not host.store.usage_events(run_id=run_id)
    finally:
        await host.close()


@pytest.mark.parametrize("name", ["runtime.sqlite-wal", "host.lock", "attachments/source.txt"])
def test_tracked_runtime_named_files_still_affect_checkpoint_and_recovery(
    isolated_workspace, name,
):
    repo = make_repo(isolated_workspace / "repo")
    state = repo / "var/state"
    artifact = state / name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("first\n")
    git(repo, "add", "var")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "file")
    stamp = time.time() - 600
    os.utime(artifact, (stamp, stamp))
    before = workspace_checkpoint(repo, **policy(repo, state))
    artifact.write_text("second\n")
    assert workspace_checkpoint(repo, **policy(repo, state)) != before
    assert changed_since(repo, stamp + 1, **policy(repo, state)) == (
        [artifact.relative_to(repo).as_posix()], False,
    )


def test_untracked_artifact_changes_and_links_have_precise_recovery_semantics(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    state = repo / "var/state"
    state.mkdir(parents=True)
    artifact = state / "runtime.sqlite-wal"
    artifact.write_text("initial")
    before = workspace_checkpoint(repo, **policy(repo, state))
    artifact.write_text("additional runtime data")
    assert workspace_checkpoint(repo, **policy(repo, state)) == before
    assert changed_since(repo, 0, **policy(repo, state)) == ([], False)
    artifact.unlink()
    artifact.symlink_to("../../source.txt")
    after = workspace_checkpoint(repo, **policy(repo, state))
    assert after != before
    assert changed_since(repo, 0, **policy(repo, state)) == (
        ["var/state/runtime.sqlite-wal"], False,
    )


async def test_in_source_runtime_worker_evidence_uses_same_policy(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "code.py").write_text("value = 1\nprint(value)\n")
    host = RuntimeHost(
        settings_for(repo, state_dir=repo / "var/state", workers_enabled=True), WorkerProvider(),
    )
    try:
        result = await host.run(RunRequest("private parent history"))
        assert result.status == "completed"
        starts = [e["payload"] for e in host.store.run_events(result.run_id)
                  if e["type"] == "worker.started"]
        assert len(starts) == 2
        assert starts[0]["source_checkpoint"] == starts[1]["source_checkpoint"]
        assert starts[0]["source_checkpoint"] == host.store.get_run(result.run_id)["checkpoint"]
    finally:
        await host.close()


@pytest.mark.parametrize("target", ["source", "snapshot"])
async def test_new_policy_checkpoint_survives_restart_and_continue(isolated_workspace, target):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "app.py").write_text("source\n")
    settings = settings_for(repo, state_dir=repo / "var/state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    session = host.create_session()
    run_id = await host.admit_run(RunRequest(
        "inspect", session, mode="plan", execution_target=target,
    ))
    checkpoint = host.store.get_run(run_id)["checkpoint"]
    await host.close()
    provider = ScriptedProvider([{"text": "continued"}])
    host = RuntimeHost(settings, provider)
    try:
        assert host.store.get_run(run_id)["checkpoint"] == checkpoint
        assert not provider.requests
        result = await host.continue_session(session)
        assert result.status == "completed"
        assert host.store.get_run(result.run_id)["checkpoint"] == checkpoint
    finally:
        await host.close()


async def test_injected_database_uses_its_actual_path(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    store = RuntimeStore(repo / "var/custom.db")
    host = RuntimeHost(
        settings_for(repo, state_dir=isolated_workspace / "state"),
        ScriptedProvider([{"text": "done"}]), store=store,
    )
    try:
        result = await host.run(RunRequest("inspect"))
        assert result.status == "completed"
        checkpoint = host.store.get_run(result.run_id)["checkpoint"]
        assert "var/custom.db" in checkpoint["checkpoint_policy"]["excluded_untracked"]
        assert "runtime.sqlite" not in str(checkpoint["checkpoint_policy"])
    finally:
        await host.close()
        store.close()


async def test_legacy_checkpoint_is_not_upgraded_by_restart(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    settings = settings_for(repo, state_dir=repo / "var/state")
    host = RuntimeHost(settings, ScriptedProvider([]))
    session = host.create_session()
    host.store.create_run("legacy", session)
    host.store.append_event("legacy", "run.started", {})
    original = workspace_checkpoint(repo)
    host.store.append_event("legacy", "workspace.checkpoint", {"checkpoint": original})
    await host.close()
    host = RuntimeHost(settings, ScriptedProvider([]))
    try:
        recovered = host.store.get_run("legacy")
        assert recovered["status"] == "interrupted"
        assert recovered["checkpoint"] == original
        assert len([e for e in host.store.run_events("legacy")
                    if e["type"] == "workspace.checkpoint"]) == 1
        with pytest.raises(ValueError, match="Workspace changed or checkpoint unavailable"):
            await host.continue_session(session)
    finally:
        await host.close()


async def test_state_at_workspace_root_is_explicitly_rejected(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(repo, state_dir=repo), ScriptedProvider([]))
    try:
        with pytest.raises(ValueError, match="State directory must not be the Workspace root"):
            await host.run(RunRequest("inspect"))
        assert not host.store.unfinished_runs()
    finally:
        await host.close()
