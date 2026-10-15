"""Bounded, content-addressed Skill packages; enabled state belongs to Event Log.

Preview never writes. Import only materializes validated bytes, never executes them.
The immutable manifest is a content receipt, not installation/activation authority.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DESCRIPTION_LIMIT = 1024
RESOURCE_DIRECTORIES = frozenset({"references", "scripts", "assets"})
ROOT_FILES = frozenset({"SKILL.md", "LICENSE", "LICENSE.txt", "README.md"})
METADATA_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)


class SkillPackageError(ValueError):
    """A package was invalid, changed, or exceeded configured bounds."""


@dataclass(frozen=True, slots=True)
class SkillLimits:
    max_files: int = 256
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 5_000_000
    max_archive_bytes: int = 8_000_000
    max_manifest_bytes: int = 64_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.max_files,
                self.max_file_bytes,
                self.max_total_bytes,
                self.max_archive_bytes,
                self.max_manifest_bytes,
            )
        ):
            raise ValueError("Skill limits must be positive integers")


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    code: str
    message: str
    source: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class SkillResource:
    path: str
    content: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": len(self.content), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class SkillPackagePreview:
    source: str
    name: str | None
    description: str | None
    version: str | None
    resources: tuple[SkillResource, ...] = field(repr=False)
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    @property
    def manifest(self) -> list[dict[str, Any]]:
        return [resource.as_dict() for resource in self.resources]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "valid": self.valid,
            "manifest": self.manifest,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SkillPackageVersion:
    name: str
    description: str
    version: str
    root: Path
    resources: tuple[SkillResource, ...] = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "manifest": [resource.as_dict() for resource in self.resources],
            "metadata": dict(self.metadata),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


def validate_name(name: object) -> str:
    if not isinstance(name, str) or len(name) > 64 or not NAME_PATTERN.fullmatch(name):
        raise SkillPackageError(
            "Skill name must be 1-64 lowercase letters/digits with single hyphens"
        )
    return name


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    # Reads may update atime; only identity, type and modification evidence matter.
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise SkillPackageError("Metadata keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueLoader.add_constructor("tag:yaml.org,2002:map", _unique_mapping)


def resource_path(value: str) -> str:
    """Reject ambiguous paths before touching a filesystem or zip member."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SkillPackageError("Invalid Skill resource path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or len(value) > 512
        or unicodedata.normalize("NFC", value) != value
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
        or any(ord(char) < 32 for char in value)
        or any(part.endswith((".", " ")) for part in path.parts)
        or any(
            part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for part in path.parts
        )
    ):
        raise SkillPackageError("Skill resource must have a canonical relative path")
    if len(path.parts) == 1:
        if value not in ROOT_FILES:
            raise SkillPackageError(f"Unsupported root resource: {value}")
    elif path.parts[0] not in RESOURCE_DIRECTORIES:
        raise SkillPackageError(f"Unsupported resource directory: {path.parts[0]}")
    return value


def _metadata(content: bytes) -> tuple[dict[str, Any], tuple[SkillDiagnostic, ...]]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise SkillPackageError("SKILL.md must be UTF-8") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillPackageError("SKILL.md requires YAML frontmatter")
    boundary = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if boundary is None:
        raise SkillPackageError("SKILL.md frontmatter is not closed")
    try:
        frontmatter = "".join(lines[1:boundary])
        if any(isinstance(token, yaml.AliasToken) for token in yaml.scan(frontmatter)):
            raise SkillPackageError("YAML aliases are not supported in Skill metadata")
        metadata = yaml.load(frontmatter, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise SkillPackageError("SKILL.md has invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise SkillPackageError("SKILL.md metadata must be a mapping with string keys")
    unknown = set(metadata) - METADATA_FIELDS
    if unknown:
        raise SkillPackageError(f"Unsupported metadata fields: {', '.join(sorted(unknown))}")
    validate_name(metadata.get("name"))
    description = metadata.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > DESCRIPTION_LIMIT
        or any(ord(char) < 32 and char not in "\n\r\t" for char in description)
    ):
        raise SkillPackageError("description must contain 1-1024 readable characters")
    if not "".join(lines[boundary + 1 :]).strip():
        raise SkillPackageError("SKILL.md must include instructions after frontmatter")
    for field_name in ("license", "compatibility", "allowed-tools"):
        if field_name in metadata and not isinstance(metadata[field_name], str):
            raise SkillPackageError(f"{field_name} must be a string")
    extra = metadata.get("metadata", {})
    if not isinstance(extra, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in extra.items()
    ):
        raise SkillPackageError("metadata must map strings to strings")
    if len(metadata.get("compatibility", "")) > 500:
        raise SkillPackageError("compatibility must not exceed 500 characters")
    diagnostics: list[SkillDiagnostic] = []
    # Optional advisory convention; lookup only, never invoke installers or commands.
    requires = extra.get("requires", "")
    for command in filter(None, (item.strip() for item in requires.split(","))):
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", command):
            raise SkillPackageError("metadata.requires must list comma-separated executable names")
        if shutil.which(command) is None:
            diagnostics.append(
                SkillDiagnostic(
                    "dependency_missing",
                    f"Required executable is unavailable: {command}",
                    "SKILL.md",
                    "warning",
                )
            )
    return metadata, tuple(diagnostics)


def _content_version(resources: tuple[SkillResource, ...]) -> str:
    manifest = [resource.as_dict() for resource in resources]
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _preview(
    resources: list[SkillResource], source: str, limits: SkillLimits
) -> SkillPackagePreview:
    ordered = tuple(sorted(resources, key=lambda item: item.path))
    if len({item.path.casefold() for item in ordered}) != len(ordered):
        raise SkillPackageError("Duplicate or case-colliding Skill resource")
    if len(ordered) > limits.max_files:
        raise SkillPackageError("Skill file count exceeds limit")
    if sum(len(item.content) for item in ordered) > limits.max_total_bytes:
        raise SkillPackageError("Skill total size exceeds limit")
    if any(len(item.content) > limits.max_file_bytes for item in ordered):
        raise SkillPackageError("Skill resource exceeds file size limit")
    manifest = next((item.content for item in ordered if item.path == "SKILL.md"), None)
    if manifest is None:
        raise SkillPackageError("Skill package requires SKILL.md at its root")
    if len(manifest) > limits.max_manifest_bytes:
        raise SkillPackageError("SKILL.md exceeds manifest size limit")
    metadata, diagnostics = _metadata(manifest)
    return SkillPackagePreview(
        source,
        metadata["name"],
        metadata["description"].strip(),
        _content_version(ordered),
        ordered,
        diagnostics,
        metadata,
    )


def _invalid(source: str, error: Exception) -> SkillPackagePreview:
    return SkillPackagePreview(
        source,
        None,
        None,
        None,
        (),
        (SkillDiagnostic("invalid_package", str(error), source),),
    )


def preview_directory(path: Path, limits: SkillLimits | None = None) -> SkillPackagePreview:
    """Read a bounded tree, rejecting symlinks and source drift; never write."""
    limits = limits or SkillLimits()
    source = str(path)
    try:
        if path.is_symlink() or not path.is_dir():
            raise SkillPackageError("Skill source must be a directory, not a symlink")
        root = path.resolve(strict=True)
        resources: list[SkillResource] = []
        identities: list[tuple[Path, os.stat_result]] = []
        directory_identities: list[tuple[Path, os.stat_result]] = []
        total = 0
        for directory_count, (current, directories, filenames) in enumerate(
            os.walk(root, followlinks=False),
            1,
        ):
            if directory_count > limits.max_files * 4:
                raise SkillPackageError("Skill directory count exceeds limit")
            directories.sort()
            current_path = Path(current)
            directory_identities.append((current_path, current_path.stat(follow_symlinks=False)))
            for name in directories:
                directory = current_path / name
                if directory.is_symlink():
                    raise SkillPackageError("Skill directories cannot contain symbolic links")
                relative_path = directory.relative_to(root)
                resource_path(f"{relative_path.as_posix()}/__directory__")
                relative = relative_path.parts
                if relative[0] not in RESOURCE_DIRECTORIES:
                    raise SkillPackageError(f"Unsupported resource directory: {relative[0]}")
            for name in sorted(filenames):
                file_path = current_path / name
                relative_name = resource_path(file_path.relative_to(root).as_posix())
                if len(resources) >= limits.max_files:
                    raise SkillPackageError("Skill file count exceeds limit")
                actual = file_path.lstat()
                if not stat.S_ISREG(actual.st_mode):
                    raise SkillPackageError("Skill resources must be regular files, not links")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                with os.fdopen(os.open(file_path, flags), "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or not stat.S_ISREG(actual.st_mode)
                        or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino)
                    ):
                        raise SkillPackageError("Skill resources must be regular files, not links")
                    if before.st_size > limits.max_file_bytes:
                        raise SkillPackageError("Skill resource exceeds file size limit")
                    total += before.st_size
                    if total > limits.max_total_bytes:
                        raise SkillPackageError("Skill total size exceeds limit")
                    content = stream.read(limits.max_file_bytes + 1)
                    after = os.fstat(stream.fileno())
                if len(content) != before.st_size or _file_identity(before) != _file_identity(
                    after
                ):
                    raise SkillPackageError("Skill source changed while reading")
                resources.append(SkillResource(relative_name, content))
                identities.append((file_path, after))
        for item, identity in [*identities, *directory_identities]:
            if _file_identity(item.stat(follow_symlinks=False)) != _file_identity(identity):
                raise SkillPackageError("Skill source changed while reading")
        return _preview(resources, source, limits)
    except (OSError, ValueError) as exc:
        return _invalid(source, exc)


def preview_zip(source: Path | bytes, limits: SkillLimits | None = None) -> SkillPackagePreview:
    """Preflight archive entries and then read, without extractall or filesystem writes."""
    limits = limits or SkillLimits()
    label = str(source) if isinstance(source, Path) else "uploaded.zip"
    try:
        if isinstance(source, Path):
            if source.is_symlink() or not source.is_file():
                raise SkillPackageError("Archive source must be a regular file")
            with source.open("rb") as stream:
                archive_bytes = stream.read(limits.max_archive_bytes + 1)
        else:
            archive_bytes = source
        if len(archive_bytes) > limits.max_archive_bytes:
            raise SkillPackageError("Skill archive exceeds compressed size limit")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_files * 4:
                raise SkillPackageError("Skill archive entry count exceeds limit")
            files: list[zipfile.ZipInfo] = []
            seen: set[str] = set()
            total = 0
            for entry in entries:
                name = entry.filename
                mode = entry.external_attr >> 16
                if (
                    entry.flag_bits & 1
                    or stat.S_ISLNK(mode)
                    or (stat.S_IFMT(mode) and not stat.S_ISREG(mode) and not stat.S_ISDIR(mode))
                ):
                    raise SkillPackageError(
                        "Encrypted archives, links and special files are forbidden"
                    )
                if entry.is_dir():
                    name = name[:-1]
                    parts = PurePosixPath(name).parts
                    if (
                        not parts
                        or parts[0] not in RESOURCE_DIRECTORIES
                        or name != PurePosixPath(name).as_posix()
                        or any(part in {".", ".."} or ":" in part for part in parts)
                        or "\\" in name
                    ):
                        raise SkillPackageError("Invalid Skill archive directory")
                    continue
                name = resource_path(name)
                if name.casefold() in seen:
                    raise SkillPackageError("Duplicate or case-colliding archive resource")
                seen.add(name.casefold())
                if entry.file_size > limits.max_file_bytes:
                    raise SkillPackageError("Skill resource exceeds file size limit")
                total += entry.file_size
                if total > limits.max_total_bytes or len(files) >= limits.max_files:
                    raise SkillPackageError("Skill archive exceeds package limits")
                files.append(entry)
            resources: list[SkillResource] = []
            for entry in files:
                with archive.open(entry) as stream:
                    content = stream.read(limits.max_file_bytes + 1)
                if len(content) != entry.file_size:
                    raise SkillPackageError("Archive resource size differs from manifest")
                resources.append(SkillResource(entry.filename, content))
            return _preview(resources, label, limits)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        return _invalid(label, exc)


class SkillPackageService:
    """Stateless package IO; caller authorizes activation using canonical events."""

    def __init__(self, state_dir: Path, limits: SkillLimits | None = None):
        self.root = state_dir.resolve() / "skill-packages"
        self.limits = limits or SkillLimits()

    def preview_directory(self, path: Path) -> SkillPackagePreview:
        return preview_directory(path, self.limits)

    def preview_zip(self, source: Path | bytes) -> SkillPackagePreview:
        return preview_zip(source, self.limits)

    def import_directory(
        self,
        path: Path,
        *,
        record: Callable[[dict[str, Any]], None] | None = None,
    ) -> SkillPackageVersion:
        return self._import(self.preview_directory(path), record)

    def import_zip(
        self,
        source: Path | bytes,
        *,
        record: Callable[[dict[str, Any]], None] | None = None,
    ) -> SkillPackageVersion:
        return self._import(self.preview_zip(source), record)

    def _path(self, name: str, version: str) -> Path:
        validate_name(name)
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            raise SkillPackageError("Invalid content version")
        path = self.root / name / version
        if any(part.is_symlink() for part in (self.root, self.root / name, path)):
            raise SkillPackageError("Managed Skill directories cannot be symlinks")
        return path

    def _import(
        self,
        preview: SkillPackagePreview,
        record: Callable[[dict[str, Any]], None] | None,
    ) -> SkillPackageVersion:
        if not preview.valid or not preview.name or not preview.version:
            raise SkillPackageError("; ".join(item.message for item in preview.diagnostics))
        target = self._path(preview.name, preview.version)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=target.parent))
            try:
                for resource in preview.resources:
                    destination = temporary / resource.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(resource.content)
                    destination.chmod(0o444)
                # Manifest is derived from bytes at inspection; no activation state is written.
                temporary.rename(target)
            except FileExistsError:
                pass
            finally:
                if temporary.exists():
                    for destination in temporary.rglob("*"):
                        if destination.is_file():
                            destination.chmod(0o600)
                    shutil.rmtree(temporary)
        installed = self.inspect_version(preview.name, preview.version)
        if record is not None:
            record({"type": "skill.package_imported", **installed.as_dict()})
        return installed

    def inspect_version(self, name: str, version: str) -> SkillPackageVersion:
        root = self._path(name, version)
        preview = self.preview_directory(root)
        if not preview.valid or preview.name != name or preview.version != version:
            raise SkillPackageError(
                "Skill content is missing, invalid, or no longer matches its version"
            )
        assert preview.description is not None
        return SkillPackageVersion(
            name,
            preview.description,
            version,
            root,
            preview.resources,
            preview.metadata,
            preview.diagnostics,
        )

    def list_versions(self, name: str) -> tuple[SkillPackageVersion, ...]:
        validate_name(name)
        parent = self.root / name
        if parent.is_symlink() or self.root.is_symlink():
            raise SkillPackageError("Managed Skill directories cannot be symlinks")
        if not parent.exists():
            return ()
        return tuple(
            self.inspect_version(name, path.name)
            for path in sorted(parent.iterdir())
            if VERSION_PATTERN.fullmatch(path.name)
        )

    def read_resource(
        self,
        name: str,
        version: str,
        path: str,
        *,
        offset: int = 0,
        limit: int = 12_000,
    ) -> str:
        package = self.inspect_version(name, version)
        return resource_text(package.resources, path, offset=offset, limit=limit)


def resource_text(
    resources: tuple[SkillResource, ...],
    path: str,
    *,
    offset: int = 0,
    limit: int = 12_000,
) -> str:
    path = resource_path(path)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12_000:
        raise SkillPackageError("Resource offset must be nonnegative and limit must be 1-12000")
    resource = next((item for item in resources if item.path == path), None)
    if resource is None:
        raise SkillPackageError(f"Skill resource not found: {path}")
    try:
        text = resource.content.decode("utf-8")
    except UnicodeError as exc:
        raise SkillPackageError("Binary Skill resources cannot be displayed as text") from exc
    page = text[offset : offset + limit]
    if offset + len(page) < len(text):
        return f"{page}\n\n[Next character offset: {offset + len(page)}; total: {len(text)}]"
    return page
