"""Workspace-scoped skill discovery and progressive loading."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from eventide.workspace import digest

DESCRIPTION_LIMIT = 500
SKILLS_DIRECTORY = "skills"


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    content: str
    source: str


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    boundary = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if boundary is None:
        return {}, text
    try:
        value = yaml.safe_load("".join(lines[1:boundary])) or {}
    except yaml.YAMLError:
        value = {}
    metadata = value if isinstance(value, dict) else {}
    return metadata, "".join(lines[boundary + 1 :]).lstrip()


def _safe_name(value: object, fallback: str) -> str:
    name = str(value).strip() if isinstance(value, str) else fallback
    if not name or len(name) > 128 or any(ord(char) < 32 for char in name):
        return fallback
    return name


def _description(value: object, body: str, fallback: str) -> str:
    description = str(value).strip() if isinstance(value, str) else ""
    if not description:
        heading = next(
            (line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")),
            "",
        )
        description = heading or fallback
    description = re.sub(r"\s+", " ", description)
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    return description


class SkillCatalog:
    """An immutable snapshot of valid skills found under one Workspace."""

    def __init__(self, workspace: Path, skills: tuple[Skill, ...]):
        self.workspace = workspace
        self.skills = skills
        self._by_name = {skill.name: skill for skill in skills}
        self.catalog_hash = digest(
            [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "content_hash": digest(skill.content),
                }
                for skill in skills
            ]
        )

    @classmethod
    def discover(cls, workspace: Path) -> SkillCatalog:
        root = workspace.resolve()
        skills_root = root / SKILLS_DIRECTORY
        if not skills_root.exists():
            return cls(root, ())
        resolved_skills_root = skills_root.resolve()
        if not resolved_skills_root.is_dir() or not resolved_skills_root.is_relative_to(root):
            return cls(root, ())

        discovered: list[Skill] = []
        names: set[str] = set()
        for directory in sorted(
            resolved_skills_root.iterdir(), key=lambda path: path.name.casefold()
        ):
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            try:
                resolved_manifest = manifest.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if not resolved_manifest.is_file() or not resolved_manifest.is_relative_to(root):
                continue
            try:
                content = resolved_manifest.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            metadata, body = _frontmatter(content)
            name = _safe_name(metadata.get("name"), directory.name)
            if name in names:
                continue
            names.add(name)
            discovered.append(
                Skill(
                    name=name,
                    description=_description(metadata.get("description"), body, name),
                    content=content,
                    source=resolved_manifest.relative_to(root).as_posix(),
                )
            )
        return cls(root, tuple(discovered))

    def __bool__(self) -> bool:
        return bool(self.skills)

    def prompt(self) -> str:
        if not self.skills:
            return ""
        entries = "\n".join(f"- {skill.name}: {skill.description}" for skill in self.skills)
        return (
            "Workspace skills available:\n"
            f"{entries}\n"
            "When a listed skill applies, call load_skill with its exact name before doing "
            "the work. Only the catalog is shown here; the tool returns the full SKILL.md."
        )

    def tool(self) -> dict[str, Any]:
        return {
            "name": "load_skill",
            "description": "Load the full instructions for one available Workspace skill.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [skill.name for skill in self.skills],
                    }
                },
                "required": ["name"],
            },
        }

    def load(self, name: str) -> str:
        skill = self._by_name.get(name)
        if skill is None:
            available = ", ".join(self._by_name) or "(none)"
            return f"Error: Skill not found: {name}. Available: {available}"
        return skill.content
