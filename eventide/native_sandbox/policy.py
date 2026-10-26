"""Strictly typed permission policy for native macOS/Linux sandboxing.

This module is host-side configuration only. Backends translate a policy into
platform-specific profile text and argv; anything a backend cannot express must
fail explicitly instead of falling back to an unsandboxed run. A policy never
executes tools and never talks to a model.

Current boundary (MK-21 slice 1, 2026-10-26):

- Profiles: ``read-only`` and ``workspace-write`` only.
- Network access is always denied; ``allow_network=True`` has no verified
  loopback guard for the local management API and is refused by every backend.
- Subprocess spawning is refused: Both launchers retain executable interpreters
  and only offer partial process-exec restrictions, so workers
  never get an allow.
- Enforcement correctness on a real kernel is verified separately; tool
  presence is only an availability probe, not proof of isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from eventide.native_sandbox.worker import WorkerGrant

MAX_PATH_LENGTH = 4096
_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


class NativeSandboxProfile(str, Enum):
    """File permission profile granted to the isolated worker."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class NativeBackendKind(str, Enum):
    """Supported native isolation backends."""

    SEATBELT = "seatbelt"
    BUBBLEWRAP = "bubblewrap"


class UnsupportedPolicyError(ValueError):
    """A policy input cannot be expressed safely by the requested backend."""


def _canonical_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        if isinstance(value, str):
            value = Path(value)
        else:
            raise ValueError(f"{name} must be a filesystem path")
    raw = str(value)
    if not raw or "\x00" in raw or any(char in _CONTROL_CHARS for char in raw):
        raise ValueError(f"{name} contains control characters")
    if len(raw) > MAX_PATH_LENGTH:
        raise ValueError(f"{name} exceeds the path length limit")
    if not value.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return value.resolve()


def _is_within(child: Path, parent: Path) -> bool:
    if child == parent:
        return True
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _overlaps(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


@dataclass(frozen=True)
class NativeSandboxPolicy:
    """Declarative sandbox inputs; every path is canonicalized on construction."""

    profile: NativeSandboxProfile
    workspace_root: Path
    tmp_root: Path
    read_only_roots: tuple[Path, ...] = ()
    protected_roots: tuple[Path, ...] = ()
    allow_network: bool = False
    allow_subprocess: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.profile, NativeSandboxProfile):
            raise ValueError("profile must be a NativeSandboxProfile")
        object.__setattr__(
            self, "workspace_root", _canonical_path(self.workspace_root, "workspace_root")
        )
        object.__setattr__(self, "tmp_root", _canonical_path(self.tmp_root, "tmp_root"))
        read_only = tuple(
            _canonical_path(path, f"read_only_roots[{index}]")
            for index, path in enumerate(self.read_only_roots)
        )
        protected = tuple(
            _canonical_path(path, f"protected_roots[{index}]")
            for index, path in enumerate(self.protected_roots)
        )
        if _overlaps(self.workspace_root, self.tmp_root):
            raise ValueError("tmp_root must be outside the workspace")
        for root in read_only:
            if _overlaps(root, self.tmp_root):
                raise ValueError("read_only_roots must not overlap tmp_root")
            if root == self.workspace_root:
                raise ValueError("read_only_roots must not equal the workspace root")
        for root in protected:
            if root in (self.workspace_root, self.tmp_root):
                raise ValueError("protected_roots must not equal workspace or tmp roots")
            if _is_within(self.tmp_root, root):
                raise ValueError("tmp_root must not live inside a protected root")
        object.__setattr__(self, "read_only_roots", read_only)
        object.__setattr__(self, "protected_roots", protected)

    def grant(self) -> WorkerGrant:
        """Build the worker-side view of this policy (no secrets, paths only)."""
        return WorkerGrant(
            profile=self.profile.value,
            workspace_root=str(self.workspace_root),
            tmp_root=str(self.tmp_root),
            read_only_roots=tuple(str(path) for path in self.read_only_roots),
            protected_roots=tuple(str(path) for path in self.protected_roots),
        )


def default_protected_paths(
    workspace_root: Path,
    *,
    state_root: Path | None = None,
    git_common_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Protected roots for an execution copy: runtime state and shared Git.

    The workspace may contain a legacy ``.eventide`` directory; denying it
    explicitly works on both backends (Seatbelt carve-out, bubblewrap shadow
    mount), so it is always included.
    """
    roots: list[Path] = [workspace_root / ".eventide", workspace_root / ".git"]
    # Execution copies may live below state_root. The worker and OS only grant
    # the exact copy, so denying its ancestor would also deny the granted copy.
    if state_root is not None and not _is_within(workspace_root.resolve(), state_root.resolve()):
        roots.append(state_root)
    if git_common_dir is not None:
        roots.append(git_common_dir)
    unique: dict[Path, None] = {}
    for root in roots:
        unique.setdefault(root.resolve(), None)
    return tuple(unique)


# Capability matrix for the current slice. Values are honest support levels:
# "full" (policy expression available; kernel verification remains separate), "partial"
# (approximation, documented limitation) and "unsupported" (explicit refusal).
SUPPORT_MATRIX: dict[tuple[NativeBackendKind, str], str] = {
    (NativeBackendKind.SEATBELT, "write_allowlist"): "full",
    (NativeBackendKind.SEATBELT, "network_deny"): "full",
    (NativeBackendKind.SEATBELT, "network_allow_loopback_guard"): "unsupported",
    (NativeBackendKind.SEATBELT, "subprocess_deny"): "partial",
    (NativeBackendKind.SEATBELT, "protected_inside_workspace"): "full",
    (NativeBackendKind.BUBBLEWRAP, "write_allowlist"): "full",
    (NativeBackendKind.BUBBLEWRAP, "network_deny"): "full",
    (NativeBackendKind.BUBBLEWRAP, "network_allow_loopback_guard"): "unsupported",
    # Interpreter prefixes may include other binaries; the interpreter itself stays
    # executable; bwrap has no per-syscall exec filter without a seccomp BPF.
    (NativeBackendKind.BUBBLEWRAP, "subprocess_deny"): "partial",
    # Shadow tmpfs hides the subtree; content is invisible rather than erroring.
    (NativeBackendKind.BUBBLEWRAP, "protected_inside_workspace"): "full",
}


def check_backend_support(policy: NativeSandboxPolicy, backend: NativeBackendKind) -> None:
    """Refuse policies the backend cannot express; never degrade to unsandboxed."""
    if policy.allow_network:
        raise UnsupportedPolicyError(
            "Network allow is unsupported: a loopback guard protecting the local "
            "management API has no offline-verifiable expression on this backend"
        )
    if policy.allow_subprocess:
        raise UnsupportedPolicyError(
            "Subprocess allow is unsupported in this slice; workers run file "
            "operations only and must not spawn children"
        )
    if backend is NativeBackendKind.BUBBLEWRAP:
        for root in policy.read_only_roots:
            if _is_within(root, policy.workspace_root):
                raise UnsupportedPolicyError(
                    "bubblewrap cannot mount a read-only dependency inside the "
                    "workspace: the shadow mount would hide its content"
                )
