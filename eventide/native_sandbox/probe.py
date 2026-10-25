"""Read-only availability probes for native sandbox tooling.

A probe only answers "is the platform expected to support this backend and is
the executable present". It never executes the tool and never proves isolation:
whether a backend actually enforces a policy is verified by the separate
real-machine acceptance step (MK-21).
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass

from eventide.native_sandbox.policy import NativeBackendKind

SANDBOX_EXEC_NAME = "sandbox-exec"
BUBBLEWRAP_NAME = "bwrap"

_PLATFORM_FOR_BACKEND: dict[NativeBackendKind, str] = {
    NativeBackendKind.SEATBELT: "Darwin",
    NativeBackendKind.BUBBLEWRAP: "Linux",
}

_BINARY_NAME: dict[NativeBackendKind, str] = {
    NativeBackendKind.SEATBELT: SANDBOX_EXEC_NAME,
    NativeBackendKind.BUBBLEWRAP: BUBBLEWRAP_NAME,
}


@dataclass(frozen=True)
class BackendDetection:
    """Outcome of a read-only availability probe; never an isolation guarantee."""

    kind: NativeBackendKind
    platform: str
    platform_supported: bool
    binary_path: str | None
    available: bool
    detail: str

    @property
    def kind_name(self) -> str:
        return self.kind.value


def detect_backend(kind: NativeBackendKind) -> BackendDetection:
    system = platform.system()
    expected = _PLATFORM_FOR_BACKEND[kind]
    binary = shutil.which(_BINARY_NAME[kind])
    if system != expected:
        return BackendDetection(
            kind=kind,
            platform=system,
            platform_supported=False,
            binary_path=binary,
            available=False,
            detail=f"{kind.value} applies to {expected}; host platform is {system}",
        )
    if binary is None:
        return BackendDetection(
            kind=kind,
            platform=system,
            platform_supported=True,
            binary_path=None,
            available=False,
            detail=f"{_BINARY_NAME[kind]} not found on PATH",
        )
    if not os.access(binary, os.X_OK):
        return BackendDetection(
            kind=kind,
            platform=system,
            platform_supported=True,
            binary_path=binary,
            available=False,
            detail=f"{binary} is not executable for this user",
        )
    return BackendDetection(
        kind=kind,
        platform=system,
        platform_supported=True,
        binary_path=binary,
        available=True,
        detail="Executable found; presence is not an isolation guarantee",
    )


def detect_native_backends() -> dict[NativeBackendKind, BackendDetection]:
    return {kind: detect_backend(kind) for kind in NativeBackendKind}


def preferred_backend() -> tuple[NativeBackendKind, BackendDetection] | None:
    """The expected backend for this platform, or None when none applies."""
    for kind in NativeBackendKind:
        detection = detect_backend(kind)
        if detection.platform_supported:
            return (kind, detection)
    return None
