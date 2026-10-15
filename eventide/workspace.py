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
        env={
            **{key: value for key, value in os.environ.items() if key not in {
                "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
            }},
            "GIT_OPTIONAL_LOCKS": "0",
        },
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


def project_source(path: Path, git_root: str | None) -> tuple[Path, str | None]:
    """Identify local Git siblings, never merge independent clones by remote URL.

    Missing historical paths remain independent identities. A linked checkout of
    a bare repository uses its first registered checkout as the source.
    """
    if git_root is None:
        return path, None
    try:
        common = Path(os.fsdecode(git(path, "rev-parse", "--git-common-dir")).strip())
        common = (path / common).resolve()
        entries = git(path, "worktree", "list", "--porcelain", "-z").split(b"\0")
        primary = next(field[9:] for field in entries if field.startswith(b"worktree "))
        candidate = Path(os.fsdecode(primary)).resolve()
        source, source_git = canonical_workspace(candidate)
        if source_git is None:
            return path, str(common)
        source_common = Path(os.fsdecode(git(source, "rev-parse", "--git-common-dir")).strip())
        if (source / source_common).resolve() != common:
            raise ValueError("Git project source identity changed")
        return source, str(common)
    except (ValueError, OSError, subprocess.TimeoutExpired, StopIteration):
        return path, None


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


def runtime_checkpoint_files(database: Path, state_dir: Path) -> tuple[Path, ...]:
    """Exact runtime-owned files, never the state directory or a filename glob."""
    lease = database.parent.resolve() / "host.lock"
    database = database.resolve()
    return (
        database, *(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        lease, state_dir.resolve() / "secret.key",
    )


def runtime_checkpoint_directories(state_dir: Path) -> tuple[Path, ...]:
    """Only Runtime artifact namespaces; never project `skills` or arbitrary data."""
    root = state_dir.resolve()
    return tuple(root / name for name in (
        "attachments", "skill-packages", "execution-snapshots", "evaluations",
    ))


def _checkpoint_exclusions(path: Path, files: tuple[Path, ...]) -> set[str]:
    root = path.resolve()
    return {
        file.relative_to(root).as_posix()
        for file in files if file.is_relative_to(root) and file != root
    }


def _excluded_untracked(
    path: Path, name: str, files: set[str], directories: set[str],
) -> bool:
    if name not in files and not any(name.startswith(f"{root}/") for root in directories):
        return False
    candidate = path / name
    # A link replacing an owned artifact is drift, not an ignorable runtime file.
    while candidate != path:
        if candidate.is_symlink():
            return False
        candidate = candidate.parent
    return True


def workspace_checkpoint(
    path: Path, *, exclude_untracked: tuple[Path, ...] = (),
    exclude_untracked_directories: tuple[Path, ...] = (),
) -> dict[str, Any] | None:
    """Hash Git-visible state without staging, refreshing the index, or following links."""
    try:
        head = git(path, "rev-parse", "--verify", "HEAD").decode().strip()
        changes = {}
        for key, args in (
            ("staged", ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")),
            ("unstaged", ("diff", "--binary", "--no-ext-diff", "--no-textconv")),
        ):
            changes[key] = hashlib.sha256(git(path, *args)).hexdigest()
        exclusions = _checkpoint_exclusions(path, exclude_untracked)
        directories = _checkpoint_exclusions(path, exclude_untracked_directories)
        untracked = []
        for raw in git(path, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if not raw:
                continue
            name = os.fsdecode(raw)
            candidate = path / name
            if _excluded_untracked(path, name, exclusions, directories):
                continue
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
        result: dict[str, Any] = {"head": head, **changes, "untracked": sorted(untracked)}
        if exclusions or directories:
            # Never silently compare an old, unfiltered baseline with a new policy.
            result["checkpoint_policy"] = {
                "version": 2, "excluded_untracked": sorted(exclusions),
                "excluded_untracked_directories": sorted(directories),
            }
        return result
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


CRASH_GAP_FILE_LIMIT = 20
CRASH_GAP_SCAN_LIMIT = 50_000


def changed_since(
    path: Path, since: float, *, exclude_untracked: tuple[Path, ...] = (),
    exclude_untracked_directories: tuple[Path, ...] = (),
) -> tuple[list[str], bool] | None:
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
    exclusions = _checkpoint_exclusions(path, exclude_untracked)
    directories = _checkpoint_exclusions(path, exclude_untracked_directories)
    tracked: set[str] = set()
    if exclusions or directories:
        try:
            tracked = {os.fsdecode(raw) for raw in git(path, "ls-files", "-z").split(b"\0")}
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return None
    changed: list[str] = []
    for name in names:
        candidate = path / name
        if name not in tracked and _excluded_untracked(path, name, exclusions, directories):
            continue
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
