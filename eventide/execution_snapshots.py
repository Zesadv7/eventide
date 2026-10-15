"""Persistent root-run source copies, referenced only by canonical admission facts."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from eventide.workers import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SOURCE_BYTES,
    SourceSnapshot,
)
from eventide.workspace import digest


class ExecutionSnapshots:
    def __init__(self, state_dir: Path):
        self.directory = state_dir / "execution-snapshots"
        self.state_dir = state_dir

    def create(self, workspace_id: str, root: Path, working_directory: str) -> dict[str, Any]:
        self._relative_directory(working_directory)
        copied = SourceSnapshot.create(root, root, self.state_dir)
        binding = {
            "kind": "snapshot",
            "workspace_id": workspace_id,
            "snapshot_id": copied.identity,
            "working_directory": working_directory,
            "source_checkpoint": copied.checkpoint,
            "manifest": copied.manifest,
            "skipped": copied.skipped,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self._root(binding)
            if not target.exists():
                with tempfile.TemporaryDirectory(prefix="staging-", dir=self.directory) as stage:
                    materialized = Path(stage) / "content"
                    shutil.copytree(copied.root, materialized)
                    (materialized / working_directory).mkdir(parents=True, exist_ok=True)
                    for path in materialized.rglob("*"):
                        if path.is_file():
                            path.chmod(0o444)
                    try:
                        materialized.rename(target)
                    except OSError:
                        if not target.exists():
                            raise
            self.restore(binding)
            return binding
        finally:
            copied.cleanup()

    def _root(self, binding: dict[str, Any]) -> Path:
        identity = binding.get("snapshot_id")
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(value not in "0123456789abcdef" for value in identity)
        ):
            raise ValueError("Invalid execution snapshot identity")
        expected = digest(
            {
                "checkpoint": binding["source_checkpoint"],
                "working_directory": ".",
                "manifest": binding["manifest"],
            }
        )
        if identity != expected:
            raise ValueError("Execution snapshot identity does not match its manifest")
        root = self.directory / identity
        if root.is_symlink() or self.directory.is_symlink():
            raise ValueError("Execution snapshot cannot traverse symbolic links")
        return root

    @staticmethod
    def _relative_directory(value: str) -> PurePosixPath:
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
            raise ValueError("Execution snapshot working directory escapes its root")
        return relative

    def restore(self, binding: dict[str, Any]) -> tuple[Path, Path]:
        root = self._root(binding)
        if not root.is_dir():
            raise ValueError("Execution snapshot is missing")
        actual: dict[str, str] = {}
        total = 0
        for count, path in enumerate(root.rglob("*"), start=1):
            if count > 50_000:
                raise ValueError("Execution snapshot exceeds its resource limit")
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError("Execution snapshot contains an unsafe resource")
            if path.is_file():
                if len(actual) >= MAX_SNAPSHOT_FILES:
                    raise ValueError("Execution snapshot exceeds its file limit")
                with path.open("rb") as stream:
                    content = stream.read(MAX_SOURCE_BYTES + 1)
                total += len(content)
                if len(content) > MAX_SOURCE_BYTES or total > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Execution snapshot exceeds its byte limit")
                actual[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
        expected = {name: metadata["sha256"] for name, metadata in binding["manifest"].items()}
        if actual != expected:
            raise ValueError("Execution snapshot changed; execution is refused")
        relative = self._relative_directory(binding["working_directory"])
        working = root / str(relative)
        if not working.is_dir() or not working.resolve().is_relative_to(root.resolve()):
            raise ValueError("Execution snapshot working directory is unavailable")
        return root, working

    def checkpoint(self, binding: dict[str, Any]) -> dict[str, Any]:
        self.restore(binding)
        return {
            "execution_snapshot_id": binding["snapshot_id"],
            "manifest_digest": digest(binding["manifest"]),
        }
