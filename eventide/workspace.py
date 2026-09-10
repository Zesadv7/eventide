"""Workspace identity, source evidence, and a process-owned state-root lease."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def user_state_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Eventide"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Eventide"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "eventide"


class HostLease:
    """An OS-released advisory lock, retained for the full Host lifetime."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self._file = (root / "host.lock").open("a+b")
        self._file.seek(0, 2)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise RuntimeError(f"State root already owned by another Host: {root}") from exc

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


def git(path: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        raise ValueError("Cannot inspect Git workspace")
    return result.stdout


def canonical_workspace(path: Path) -> tuple[Path, str | None]:
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be an existing directory")
    try:
        top = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return root, None
    return top, str(top)


def session_working_directory(workspace: Path, target: str | Path | None) -> tuple[str, Path]:
    """Resolve a stable Session cwd and keep it inside its Workspace identity."""
    root = workspace.resolve(strict=True)
    requested = Path(target).expanduser() if target is not None else Path(".")
    candidate = requested if requested.is_absolute() else root / requested
    candidate = candidate.resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("Working directory must be an existing directory")
    if not candidate.is_relative_to(root):
        raise ValueError("Working directory must stay inside its Workspace")
    relative = candidate.relative_to(root)
    return (relative.as_posix() if relative.parts else "."), candidate


def git_metadata(path: Path) -> dict[str, str | None]:
    """Return lightweight, live Git identity metadata for UI/API projections."""
    try:
        root = Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel")).strip()).resolve()
        head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        branch = git(root, "branch", "--show-current").decode().strip() or None
        return {"git_branch": branch, "git_head": head[:12]}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {"git_branch": None, "git_head": None}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def workspace_checkpoint(path: Path) -> dict[str, Any] | None:
    """Hash Git-visible state without staging, refreshing the index, or following links."""
    try:
        head = git(path, "rev-parse", "--verify", "HEAD").decode().strip()
        changes = {}
        for key, args in (
            ("staged", ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")),
            ("unstaged", ("diff", "--binary", "--no-ext-diff", "--no-textconv")),
        ):
            changes[key] = hashlib.sha256(git(path, *args)).hexdigest()
        untracked = []
        for raw in git(path, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if not raw:
                continue
            name = os.fsdecode(raw)
            candidate = path / name
            if candidate.is_symlink():
                body = os.fsencode(os.readlink(candidate))
            else:
                body = candidate.read_bytes()
            # Lists, not tuples: checkpoints are compared after a JSON round-trip,
            # and a tuple would never equal the list SQLite gives back.
            untracked.append([name, hashlib.sha256(body).hexdigest()])
        # Dirty submodules have state that a top-level diff cannot fully describe.
        if git(path, "submodule", "status", "--recursive").strip():
            return None
        return {"head": head, **changes, "untracked": sorted(untracked)}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


CRASH_GAP_FILE_LIMIT = 20
CRASH_GAP_SCAN_LIMIT = 50_000


def changed_since(path: Path, since: float) -> tuple[list[str], bool] | None:
    """Return Git-visible paths touched strictly after `since`, and whether the list was cut.

    A crash can kill the Host between a completed side-effecting tool and the
    checkpoint that records its result, leaving no record of what the workspace
    looked like. The filesystem can still answer what the lost checkpoint would have
    answered: if nothing carries a newer mtime and no tracked file is missing, a
    checkpoint taken now is the same evidence. Only the comparison against `since`
    matters -- changes from before that moment are equally unknown to both.

    The comparison is strict, with no tolerance subtracted from `since`: the files
    the missing checkpoint was about were written microseconds before the last
    durable fact, so `>=` would flag them on any coarse clock and the check could
    never pass. The cost is that a change landing in the very same clock tick as the
    crash is invisible here.
    """
    try:
        names = [
            os.fsdecode(raw)
            for raw in git(
                path, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            ).split(b"\0")
            if raw
        ]
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None
    if len(names) > CRASH_GAP_SCAN_LIMIT:
        return None
    changed: list[str] = []
    for name in names:
        candidate = path / name
        try:
            stamp = os.stat(candidate, follow_symlinks=False).st_mtime
        except FileNotFoundError:
            # A tracked file is gone. Its directory records when that happened, so a
            # deletion from before the crash is not evidence of a later change.
            try:
                stamp = os.stat(candidate.parent, follow_symlinks=False).st_mtime
            except OSError:
                return None
        except OSError:
            return None
        if stamp > since:
            changed.append(name)
            if len(changed) > CRASH_GAP_FILE_LIMIT:
                return changed[:CRASH_GAP_FILE_LIMIT], True
    return changed, False
