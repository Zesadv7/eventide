"""Workspace-scoped Git worktrees, with metadata outside the source checkout.

Callers serialize operations with the Workspace lock and record intent/results
in the Event Log. Importing or constructing this service never creates files.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from eventide.utils import decode_output

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    name: str
    workspace_id: str
    path: str
    branch: str
    base_commit: str
    repository: str
    git_common_dir: str
    kept: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorktreeService:
    """Manage only this Workspace's explicitly recorded Git checkouts."""

    def __init__(
        self, root: Path, state_dir: Path, workspace_id: str, *, timeout: float = 30.0
    ) -> None:
        if not _WORKSPACE_ID.fullmatch(workspace_id):
            raise ValueError("Invalid Workspace identity for worktree storage")
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")
        self.state_dir = state_dir.expanduser().resolve()
        if self.state_dir.is_relative_to(self.root):
            raise ValueError("Worktree state must stay outside the source Workspace")
        self.workspace_id = workspace_id
        self.timeout = timeout
        self.directory = self.state_dir / "worktrees" / workspace_id
        self.records = self.directory / "records"
        self.checkouts = self.directory / "checkouts"

    @staticmethod
    def validate_name(name: str) -> None:
        if (
            not _NAME.fullmatch(name)
            or name.endswith(".")
            or name.split(".", 1)[0].lower() in _WINDOWS_RESERVED
        ):
            raise ValueError("Worktree name must use 1-64 safe letters, digits, dots or dashes")

    def _git(self, cwd: Path, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), *arguments],
                capture_output=True,
                timeout=self.timeout,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"Git worktree operation failed: {exc}") from exc
        if result.returncode:
            reason = decode_output(result.stderr).strip() or "Git command failed"
            raise ValueError(reason[:2000])
        return os.fsdecode(result.stdout).strip()

    def _repository(self) -> Path:
        top = Path(self._git(self.root, "rev-parse", "--show-toplevel")).resolve()
        if top != self.root:
            raise ValueError("Worktrees require the exact Git Workspace root")
        self._git(self.root, "rev-parse", "--verify", "HEAD")
        if self._git(self.root, "submodule", "status", "--recursive"):
            raise ValueError("Worktrees with submodules are not supported")
        common = Path(self._git(self.root, "rev-parse", "--git-common-dir"))
        return (self.root / common).resolve()

    def _safe_path(self, path: Path) -> None:
        if not path.is_relative_to(self.state_dir):
            raise ValueError("Worktree path escapes its state root")
        current = self.state_dir
        for part in path.relative_to(self.state_dir).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Worktree storage cannot contain symbolic links")
        if path.resolve() != path:
            raise ValueError("Worktree path does not match its managed location")

    def _record_path(self, name: str) -> Path:
        self.validate_name(name)
        path = self.records / f"{name}.json"
        self._safe_path(path)
        return path

    def _write(self, record: WorktreeRecord) -> None:
        path = self._record_path(record.name)
        temporary = self.records / f".{record.name}.{uuid.uuid4().hex}.tmp"
        self._safe_path(temporary)
        try:
            temporary.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _registered(self) -> dict[str, str]:
        registered: dict[str, str] = {}
        current = ""
        for field in self._git(self.root, "worktree", "list", "--porcelain", "-z").split("\0"):
            if field.startswith("worktree "):
                current = field.removeprefix("worktree ")
                registered[current] = ""
            elif field.startswith("branch ") and current:
                registered[current] = field.removeprefix("branch ")
        return registered

    def _load(self, name: str, common: Path) -> WorktreeRecord:
        record_path = self._record_path(name)
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
            record = WorktreeRecord(**data)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(f"Cannot read managed worktree record: {name}") from exc
        expected = self.checkouts / name
        self._safe_path(expected)
        if (
            record.name != name
            or record.workspace_id != self.workspace_id
            or record.repository != str(self.root)
            or record.git_common_dir != str(common)
            or record.path != str(expected)
            or record.branch != f"codex/{self.workspace_id}/{name}"
            or not isinstance(record.base_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record.base_commit)
            or not isinstance(record.kept, bool)
        ):
            raise ValueError("Worktree record does not belong to this Workspace")
        registered = self._registered()
        if registered.get(str(expected)) != f"refs/heads/{record.branch}":
            raise ValueError("Worktree checkout is missing or its registered branch changed")
        checkout_common = Path(self._git(expected, "rev-parse", "--git-common-dir"))
        if (expected / checkout_common).resolve() != common:
            raise ValueError("Worktree checkout belongs to another Git repository")
        if Path(self._git(expected, "rev-parse", "--show-toplevel")).resolve() != expected:
            raise ValueError("Worktree checkout does not match its recorded path")
        return record

    def resolve_ref(self, ref: str) -> str:
        if not ref or ref.startswith("-") or any(char in ref for char in "\0\r\n"):
            raise ValueError("Invalid worktree starting ref")
        return self._git(
            self.root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        )

    def create(self, name: str, ref: str = "HEAD") -> WorktreeRecord:
        record_path = self._record_path(name)
        common = self._repository()
        base = self.resolve_ref(ref)
        tree = self._git(self.root, "ls-tree", "-r", base)
        if any(line.startswith("160000 ") for line in tree.splitlines()):
            raise ValueError("Worktree starting ref contains unsupported submodules")
        path = self.checkouts / name
        self._safe_path(path)
        if record_path.exists() or path.exists():
            raise ValueError("Managed worktree name or checkout already exists")
        self._safe_path(self.records)
        self._safe_path(self.checkouts)
        self.records.mkdir(parents=True, exist_ok=True)
        self.checkouts.mkdir(parents=True, exist_ok=True)
        record = WorktreeRecord(
            name=name,
            workspace_id=self.workspace_id,
            path=str(path),
            branch=f"codex/{self.workspace_id}/{name}",
            base_commit=base,
            repository=str(self.root),
            git_common_dir=str(common),
        )
        # Identity precedes the Git mutation, so a crash leaves evidence to inspect.
        self._write(record)
        try:
            self._git(self.root, "worktree", "add", "-b", record.branch, str(path), base)
        except ValueError:
            if not path.exists():
                record_path.unlink()
            raise
        return self._load(name, common)

    def list(self) -> list[WorktreeRecord]:
        common = self._repository()
        self._safe_path(self.records)
        if not self.records.exists():
            return []
        return [self._load(path.stem, common) for path in sorted(self.records.glob("*.json"))]

    def keep(self, name: str) -> WorktreeRecord:
        record = replace(self._load(name, self._repository()), kept=True)
        self._write(record)
        return record

    def remove(self, name: str) -> WorktreeRecord:
        record = self._load(name, self._repository())
        path = Path(record.path)
        index_entries = self._git(path, "ls-files", "-v", "-z").split("\0")
        if any(entry and (entry[0].islower() or entry[0] == "S") for entry in index_entries):
            # Both status and unforced Git removal can overlook these tracked files.
            raise ValueError("Worktree index hides tracked files; keep it for review")
        if self._git(path, "status", "--porcelain", "--untracked-files=all", "--ignored"):
            raise ValueError("Worktree has changed, untracked or ignored files; keep it for review")
        if int(self._git(path, "rev-list", "--count", f"{record.base_commit}..HEAD")):
            raise ValueError("Worktree has commits beyond its base; keep it for review")
        self._git(self.root, "worktree", "remove", record.path)
        self._record_path(name).unlink()
        # Branches are retained, including for an empty checkout.
        return record
