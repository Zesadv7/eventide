"""Canonical Skill activation and durable content references for RuntimeHost."""

from __future__ import annotations

import difflib
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from eventide.executor import CommandExecutor, LocalCommandExecutor
from eventide.models import CommandResult
from eventide.skill_packages import SkillPackageError, SkillPackageService, resource_path
from eventide.skills import Skill, SkillCatalog
from eventide.store import RuntimeStore

SKILL_FACTS = ("skill.installed", "skills.configured")


class SkillRuntime:
    def __init__(self, store: RuntimeStore, state_dir: Path):
        self.store = store
        self.packages = SkillPackageService(state_dir)

    def installed(self) -> list[dict[str, Any]]:
        versions: dict[tuple[str, str], dict[str, Any]] = {}
        for event in self.store.facts_by_type(("skill.installed",)):
            payload = event["payload"]
            versions[(payload["name"], payload["version"])] = payload
        return list(versions.values())

    def configuration(self, workspace_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"enabled_user": {}, "disabled_project": [], "selected": []}
        for event in self.store.facts_by_type(("skills.configured",)):
            if event["payload"]["workspace_id"] == workspace_id:
                result = event["payload"]
        return result

    def catalog(
        self,
        workspace_id: str,
        workspace: Path,
        *,
        selected: list[str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> SkillCatalog:
        config = self.configuration(workspace_id) if config is None else config
        pins = config["enabled_user"]
        installed = {(item["name"], item["version"]) for item in self.installed()}
        if any((name, version) not in installed for name, version in pins.items()):
            raise SkillPackageError("Enabled Skill version has no canonical installation record")
        packages = tuple(
            self.packages.inspect_version(name, version) for name, version in pins.items()
        )
        return SkillCatalog.discover(
            workspace,
            user_packages=packages,
            enabled_user=tuple(f"user:{name}@{version}" for name, version in pins.items()),
            disabled_project=tuple(config["disabled_project"]),
            selected=tuple(config["selected"] if selected is None else selected),
        )

    def configure(
        self,
        owner: str,
        workspace_id: str,
        workspace: Path,
        *,
        enabled_user: dict[str, str],
        disabled_project: list[str],
        selected: list[str],
    ) -> dict[str, Any]:
        if len(enabled_user) > 64 or len(disabled_project) > 64 or len(selected) > 64:
            raise SkillPackageError("Skill selection exceeds 64 entries")
        if len(set(selected)) != len(selected) or len(set(disabled_project)) != len(
            disabled_project
        ):
            raise SkillPackageError("Duplicate Skill identity")
        if any(not item.startswith("project:") for item in disabled_project):
            raise SkillPackageError("Disabled project Skills require project:name identities")
        config = {
            "workspace_id": workspace_id,
            "enabled_user": enabled_user,
            "disabled_project": disabled_project,
            "selected": selected,
        }
        catalog = self.catalog(workspace_id, workspace, config=config)
        config["selected"] = list(catalog.selected)
        self.store.append_fact(owner, "skills.configured", config, author="user")
        return config

    def import_user(self, owner: str, source: Path | bytes, *, zipped: bool) -> dict[str, Any]:
        if zipped:
            package = self.packages.import_zip(source)
        else:
            if not isinstance(source, Path):
                raise SkillPackageError("Directory imports require a local path")
            package = self.packages.import_directory(source)
        payload = package.as_dict()
        if not any(
            item["name"] == package.name and item["version"] == package.version
            for item in self.installed()
        ):
            self.store.append_fact(owner, "skill.installed", payload, author="user")
        return payload

    def freeze(self, catalog: SkillCatalog) -> dict[str, Any]:
        for skill in catalog.skills:
            package = self.packages.freeze_resources(skill.resources)
            if package.name != skill.name or package.version != skill.version:
                raise SkillPackageError("Skill snapshot version changed")
        return catalog.snapshot()

    def restore(self, workspace: Path, snapshot: dict[str, Any]) -> SkillCatalog:
        skills = []
        for item in snapshot["skills"]:
            package = self.packages.inspect_version(item["name"], item["version"])
            if package.as_dict()["manifest"] != item["manifest"]:
                raise SkillPackageError("Skill resource manifest changed")
            content = next(r.content for r in package.resources if r.path == "SKILL.md")
            skills.append(
                Skill(
                    package.name,
                    package.description,
                    content.decode("utf-8"),
                    item["source"],
                    item["scope"],
                    package.version,
                    package.resources,
                    package.metadata,
                )
            )
        catalog = SkillCatalog(
            workspace, tuple(skills), selected=tuple(snapshot.get("selected", []))
        )
        if catalog.catalog_hash != snapshot["catalog_hash"]:
            raise SkillPackageError("Skill snapshot integrity check failed")
        return catalog

    def status(self, workspace_id: str, workspace: Path) -> dict[str, Any]:
        config = self.configuration(workspace_id)
        catalog = self.catalog(workspace_id, workspace)
        return {
            "installed": self.installed(),
            "configuration": config,
            "enabled": catalog.snapshot(),
            "project_packages": SkillCatalog.discover(workspace).snapshot(),
        }

    def diff(self, name: str, before: str, after: str) -> dict[str, Any]:
        left = self.packages.inspect_version(name, before)
        right = self.packages.inspect_version(name, after)
        a = {r.path: r for r in left.resources}
        b = {r.path: r for r in right.resources}
        changed = sorted(path for path in a.keys() & b.keys() if a[path].sha256 != b[path].sha256)
        text = "".join(
            difflib.unified_diff(
                next(r.content for r in left.resources if r.path == "SKILL.md")
                .decode()
                .splitlines(True),
                next(r.content for r in right.resources if r.path == "SKILL.md")
                .decode()
                .splitlines(True),
                fromfile=before,
                tofile=after,
            )
        )
        return {
            "name": name,
            "before": before,
            "after": after,
            "added": sorted(b.keys() - a.keys()),
            "removed": sorted(a.keys() - b.keys()),
            "changed": changed,
            "instructions_diff": text[:20_000],
            "diff_truncated": len(text) > 20_000,
        }


def script_tool(catalog: SkillCatalog) -> dict[str, Any]:
    return {
        "name": "run_skill_script",
        "description": "Run a loaded Skill's fixed Python or shell script after policy approval.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "enum": [s.skill_id for s in catalog.skills]},
                "path": {"type": "string"},
                "args": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string", "maxLength": 4096},
                },
            },
            "required": ["name", "path"],
        },
    }


async def execute_skill_script(
    catalog: SkillCatalog,
    name: str,
    path: str,
    args: list[str],
    cwd: Path,
    command_executor: CommandExecutor,
) -> CommandResult | str:
    path = resource_path(path)
    if not path.startswith("scripts/") or Path(path).suffix not in {".py", ".sh"}:
        raise SkillPackageError("Only packaged scripts/*.py or scripts/*.sh can execute")
    skill = next((s for s in catalog.skills if s.skill_id == name), None)
    if skill is None:
        raise SkillPackageError("Script requires an exact enabled scope:name identity")
    catalog.resource_bytes(name, path)
    # Stage only frozen bytes, inside the execution root so the selected container
    # backend can see them. The Executor already applied policy and approval.
    root = Path(tempfile.mkdtemp(prefix=".eventide-skill-", dir=cwd))
    preserve = False
    try:
        for resource in skill.resources:
            target = root / resource.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resource.content)
        relative = root.relative_to(cwd)
        interpreter = "python3" if path.endswith(".py") else "sh"
        if isinstance(command_executor, LocalCommandExecutor):
            if path.endswith(".sh") and os.name == "nt":
                raise SkillPackageError("Shell Skill scripts require a POSIX or container backend")
            return await command_executor.execute_argv(
                [sys.executable if path.endswith(".py") else "sh", str(relative / path), *args],
                cwd, extra_env={"EVENTIDE_SKILL_ROOT": str(relative)},
            )
        command = f"EVENTIDE_SKILL_ROOT={shlex.quote(str(relative))} " + shlex.join(
            [interpreter, str(relative / path), *args]
        )
        return await command_executor.execute(command, cwd)
    except BaseException as exc:
        # An unconfirmed container may still hold the files. Keep evidence and
        # let Host block further work through its existing unknown-outcome rule.
        preserve = bool(getattr(exc, "outcome_unknown", False))
        raise
    finally:
        if not preserve:
            shutil.rmtree(root)
