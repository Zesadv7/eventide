"""Skills keep canonical activation, fixed run bytes, and production policy."""

import os
from dataclasses import replace

import pytest

from eventide.container_executor import ContainerCleanupError, ContainerCommandExecutor
from eventide.executor import LocalCommandExecutor
from eventide.host import RuntimeHost
from eventide.models import CommandResult, RunRequest
from eventide.providers import ScriptedProvider
from eventide.skill_packages import SkillPackageError
from eventide.skill_runtime import execute_skill_script
from eventide.skills import SkillCatalog, bundled_skill_directories
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def package(root, body="version one", *, scripts=False):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\nname: diagnose\ndescription: Diagnose a problem\n---\n" + body
    )
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "check.md").write_text(body)
    if scripts:
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "check.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "Path('script-ran').write_text('yes')\nprint(sys.argv[1:])\n"
        )
    return root


def call(name, arguments, identity):
    return {"tool_calls": [{"id": identity, "name": name, "arguments": arguments}]}


async def test_install_does_not_enable_and_scopes_are_project_specific(isolated_workspace):
    source = package(isolated_workspace / "upload")
    one = make_repo(isolated_workspace / "one")
    two = make_repo(isolated_workspace / "two")
    host = RuntimeHost(settings_for(one), ScriptedProvider([]))
    try:
        first = host.default_workspace.workspace_id
        second = host.resolve_or_register_workspace(two).workspace_id
        version = await host.import_skill(first, source, zipped=False)
        assert not host.skill_status(first)["enabled"]["skills"]
        host.configure_skills(
            first,
            enabled_user={"diagnose": version["version"]},
            disabled_project=[],
            selected=["user:diagnose"],
        )
        assert host.skill_status(first)["enabled"]["selected"] == ["user:diagnose"]
        assert not host.skill_status(second)["enabled"]["skills"]
        owner = host._skill_owner(first)
        with pytest.raises(ValueError, match="Worker sessions"):
            await host.run(RunRequest("run", owner))
        with pytest.raises(SkillPackageError, match="installation"):
            host.configure_skills(
                first, enabled_user={"diagnose": "f" * 64}, disabled_project=[], selected=[]
            )
    finally:
        await host.close()


async def test_admitted_snapshot_survives_project_update_and_restart(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    source = package(repo / "skills" / "diagnose")
    settings = replace(settings_for(repo), max_steps=1, state_dir=isolated_workspace / "state")
    provider = ScriptedProvider([call("load_skill", {"name": "diagnose"}, "first")])
    host = RuntimeHost(settings, provider)
    session = host.create_session()
    try:
        run_id = await host.admit_run(
            RunRequest("diagnose", session, selected_skills=["project:diagnose"])
        )
        package(source, body="version two")
        result = await host.start_admitted_run(run_id)
        loaded = next(e for e in host.store.run_events(run_id) if e["type"] == "tool.completed")
        assert "version one" in loaded["payload"]["content"]
        assert result.status == "interrupted"
    finally:
        await host.close()
    # The current project has version two. Continue must still reuse the prior
    # run's fixed version one, with its unchanged parked source checkpoint.
    settings = replace(settings, max_steps=2)
    provider = ScriptedProvider(
        [call("load_skill", {"name": "diagnose"}, "second"), {"text": "done"}]
    )
    host = RuntimeHost(settings, provider)
    try:
        resumed = await host.continue_session(session)
        assert resumed.status == "completed"
        loaded = next(
            e for e in host.store.run_events(resumed.run_id) if e["type"] == "tool.completed"
        )
        assert "version one" in loaded["payload"]["content"]
    finally:
        await host.close()


@pytest.mark.parametrize(
    "mode,approve,executed",
    [
        ("auto", None, False),
        ("auto", False, False),
        ("auto", True, True),
        ("plan", True, False),
    ],
)
async def test_scripts_use_executor_policy_and_readonly_catalog(
    isolated_workspace,
    mode,
    approve,
    executed,
):
    package(isolated_workspace / "skills" / "diagnose", scripts=True)
    args = "literal; touch injected"
    provider = ScriptedProvider(
        [
            call("load_skill", {"name": "diagnose"}, "load"),
            call("read_skill_resource", {"name": "diagnose", "path": "references/check.md"}, "ref"),
            call(
                "run_skill_script",
                {"name": "project:diagnose", "path": "scripts/check.py", "args": [args]},
                "script",
            ),
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    approvals = []

    async def approval(_id, tool, _reason):
        approvals.append(tool.name)
        return approve

    try:
        result = await host.run(
            RunRequest("diagnose", mode=mode),
            approval_handler=approval if approve is not None else None,
        )
        assert result.status == "completed"
        assert (isolated_workspace / "script-ran").exists() is executed
        assert not (isolated_workspace / "injected").exists()
        assert not list(isolated_workspace.glob(".eventide-skill-*"))
        assert ("run_skill_script" in {t["name"] for t in provider.requests[0].tools}) == (
            mode != "plan"
        )
        loaded = [e for e in host.store.run_events(result.run_id) if e["type"] == "skill.loaded"]
        assert loaded[0]["payload"]["skill_id"] == "project:diagnose"
        assert len(approvals) == (1 if approve is not None and mode != "plan" else 0)
    finally:
        await host.close()


async def test_loading_required_and_snapshot_tamper_rejected(isolated_workspace):
    package(isolated_workspace / "skills" / "diagnose", scripts=True)
    provider = ScriptedProvider(
        [
            call("read_skill_resource", {"name": "diagnose", "path": "references/check.md"}, "ref"),
            {"text": "done"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("check"))
        completed = next(
            e for e in host.store.run_events(result.run_id) if e["type"] == "tool.completed"
        )
        assert completed["payload"]["is_error"]
        snapshot = next(
            e["payload"]
            for e in host.store.run_events(result.run_id)
            if e["type"] == "skills.snapshot"
        )
        version = snapshot["skills"][0]["version"]
        path = host.skills.packages.root / "diagnose" / version / "references/check.md"
        path.chmod(0o600)
        path.write_text("tampered")
        with pytest.raises(SkillPackageError, match="version"):
            host.skills.restore(isolated_workspace, snapshot)
    finally:
        await host.close()


async def test_versions_diff_rollback_and_project_import(isolated_workspace):
    source = package(isolated_workspace / "upload")
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(settings_for(repo), ScriptedProvider([]))
    try:
        workspace = host.default_workspace.workspace_id
        first = await host.import_skill(workspace, source, zipped=False)
        package(source, "version two")
        second = await host.import_skill(workspace, source, zipped=False)
        diff = host.skills.diff("diagnose", first["version"], second["version"])
        assert "SKILL.md" in diff["changed"]
        assert "+version two" in diff["instructions_diff"]
        for version in (second["version"], first["version"]):
            host.configure_skills(
                workspace, enabled_user={"diagnose": version}, disabled_project=[], selected=[]
            )
            assert host.skill_status(workspace)["enabled"]["skills"][0]["version"] == version
        await host.import_skill(workspace, source, zipped=False, scope="project")
        with pytest.raises(SkillPackageError, match="already exists"):
            await host.import_skill(workspace, source, zipped=False, scope="project")
        assert (repo / "skills/diagnose/SKILL.md").exists()
    finally:
        await host.close()


async def test_script_cancellation_cleans_after_process_and_unknown_retains_evidence(
    isolated_workspace,
):
    package(isolated_workspace / "skills/diagnose", scripts=True)
    catalog = SkillCatalog.discover(isolated_workspace)
    # Normal local script execution removes its staged inputs.
    result = await execute_skill_script(
        catalog,
        "project:diagnose",
        "scripts/check.py",
        [],
        isolated_workspace,
        LocalCommandExecutor(),
    )
    assert not result.is_error
    assert not list(isolated_workspace.glob(".eventide-skill-*"))

    class Unknown:
        async def execute(self, command, cwd):
            raise ContainerCleanupError("not confirmed")

    with pytest.raises(ContainerCleanupError):
        await execute_skill_script(
            catalog, "project:diagnose", "scripts/check.py", [], isolated_workspace, Unknown()
        )
    assert len(list(isolated_workspace.glob(".eventide-skill-*"))) == 1


def test_three_bundled_packages_are_distributable():
    assert {p.name for p in bundled_skill_directories()} == {
        "code-review",
        "fault-localization",
        "test-repair",
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX mount permissions")
async def test_container_staged_resources_are_readable_without_changing_project_permissions(
    isolated_workspace,
):
    import stat

    package(isolated_workspace / "skills/diagnose", scripts=True)
    catalog = SkillCatalog.discover(isolated_workspace)
    project_mode = stat.S_IMODE(isolated_workspace.stat().st_mode)

    class InspectMount(ContainerCommandExecutor):
        async def execute(self, command, cwd):
            staging = next(cwd.glob(".eventide-skill-*"))
            for path in (staging, *staging.rglob("*")):
                assert stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o644)
            return CommandResult(exit_code=0, stdout="readable", stderr="")

    old_umask = os.umask(0o077)
    try:
        await execute_skill_script(
            catalog,
            "project:diagnose",
            "scripts/check.py",
            [],
            isolated_workspace,
            InspectMount("python:3.12-slim"),
        )
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(isolated_workspace.stat().st_mode) == project_mode
    assert not list(isolated_workspace.glob(".eventide-skill-*"))
