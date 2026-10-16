"""Explicit, content-pinned Skill inputs for isolated evaluation cases."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from eventide.skill_packages import SkillPackagePreview, SkillPackageService, preview_directory
from eventide.skills import bundled_skill_directories
from eventide.workspace import digest

if TYPE_CHECKING:
    from eventide.host import RuntimeHost

EvaluationPreset = Literal["none", "engineering"]
ENGINEERING_SKILLS = ("code-review", "fault-localization", "test-repair")


@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    preset: EvaluationPreset = "none"
    packages: tuple[SkillPackagePreview, ...] = ()

    def __post_init__(self) -> None:
        expected = ENGINEERING_SKILLS if self.preset == "engineering" else ()
        if self.preset not in {"none", "engineering"} or tuple(
            package.name for package in self.packages
        ) != expected or any(not package.valid for package in self.packages):
            raise ValueError("Invalid evaluation Skill preset")

    def snapshot(self) -> dict[str, Any]:
        value = {
            "preset": self.preset,
            "skills": [{"name": package.name, "version": package.version,
                        "manifest": package.manifest} for package in self.packages],
        }
        return {**value, "sha256": digest(value)}

    def persist(self, service: SkillPackageService) -> None:
        """Save the exact preview bytes; this does not install or enable a Skill."""
        for preview in self.packages:
            package = service.freeze_resources(preview.resources)
            if package.name != preview.name or package.version != preview.version:
                raise ValueError("Evaluation Skill content changed")

    @classmethod
    def restore(cls, service: SkillPackageService, snapshot: dict[str, Any] | None
                ) -> EvaluationProfile:
        if snapshot is None:  # Previously accepted batches have the original behavior.
            return cls()
        packages = []
        for item in snapshot["skills"]:
            package = service.inspect_version(item["name"], item["version"])
            packages.append(SkillPackagePreview(
                "evaluation preset", package.name, package.description, package.version,
                package.resources, metadata=package.metadata,
            ))
        profile = cls(snapshot["preset"], tuple(packages))
        if profile.snapshot() != snapshot:
            raise ValueError("Evaluation Skill profile fingerprint changed")
        return profile

    async def install(self, runtime: RuntimeHost) -> None:
        """Use normal user-package activation, outside the untouched task fixture."""
        if not self.packages:
            return
        workspace_id = runtime.default_workspace.workspace_id
        pins = {}
        for package in self.packages:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zipped:
                for resource in package.resources:
                    zipped.writestr(resource.path, resource.content)
            installed = await runtime.import_skill(
                workspace_id, archive.getvalue(), zipped=True,
                expected_version=package.version,
            )
            pins[installed["name"]] = installed["version"]
        runtime.configure_skills(
            workspace_id, enabled_user=pins, disabled_project=[],
            selected=[f"user:{name}" for name in pins],
        )


def evaluation_profile(preset: EvaluationPreset = "none") -> EvaluationProfile:
    """Read only the three first-party packages; never resolve arbitrary Skill sources."""
    if preset == "none":
        return EvaluationProfile()
    if preset != "engineering":
        raise ValueError("Invalid evaluation Skill preset")
    directories = {path.name: path for path in bundled_skill_directories()}
    if any(name not in directories for name in ENGINEERING_SKILLS):
        raise ValueError("Bundled engineering Skills are missing")
    return EvaluationProfile(preset, tuple(
        preview_directory(directories[name]) for name in ENGINEERING_SKILLS
    ))
