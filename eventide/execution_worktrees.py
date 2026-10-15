"""Validated Git execution copies; Run facts own the durable binding."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from eventide.delivery import validate_delivery_source
from eventide.execution_snapshots import ExecutionSnapshots
from eventide.workspace import workspace_checkpoint
from eventide.worktrees import WorktreeService


class ExecutionWorktrees:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def create(self, workspace_id: str, source: Path, cwd: str) -> dict[str, Any]:
        ExecutionSnapshots._relative_directory(cwd)
        checkpoint = validate_delivery_source(source)
        service = WorktreeService(source, self.state_dir, workspace_id)
        record = service.create(f"session-{uuid.uuid4().hex[:16]}", checkpoint["head"])
        root = Path(record.path)
        # Empty working directories are not represented in Git trees.
        (root / cwd).mkdir(parents=True, exist_ok=True)
        if workspace_checkpoint(source) != checkpoint:
            raise ValueError("Source Workspace changed during worktree admission; copy retained")
        binding = {
            "kind": "worktree",
            "workspace_id": workspace_id,
            "working_directory": cwd,
            "source_checkpoint": checkpoint,
            "worktree": record.to_dict(),
        }
        self.restore(binding)
        return binding

    def restore(self, binding: dict[str, Any]) -> tuple[Path, Path]:
        recorded = binding["worktree"]
        service = WorktreeService(
            Path(recorded["repository"]),
            self.state_dir,
            binding["workspace_id"],
        )
        record = service._load(recorded["name"], service._repository())
        # A user may explicitly mark a copy as kept; that does not change its identity.
        if any(record.to_dict()[key] != value for key, value in recorded.items() if key != "kept"):
            raise ValueError("Execution worktree identity changed")
        relative = ExecutionSnapshots._relative_directory(binding["working_directory"])
        root = Path(record.path)
        cwd = root / str(relative)
        if not cwd.is_dir() or cwd.resolve() != cwd:
            raise ValueError("Execution worktree working directory changed or unavailable")
        return root, cwd

    def checkpoint(self, binding: dict[str, Any]) -> dict[str, Any]:
        root, _ = self.restore(binding)
        checkpoint = workspace_checkpoint(root)
        if checkpoint is None:
            raise ValueError("Execution worktree has no trustworthy checkpoint")
        return checkpoint
