"""Native macOS/Linux sandbox boundary for isolated file workers (MK-21).

This slice provides strictly typed policy inputs, read-only backend probes,
Seatbelt/bubblewrap profile and argv construction, the isolated file worker
protocol and the host-side runner. It is a library only: wiring into
``ToolExecutor``/``PolicyEngine`` and the HTTP/UI surface is a separate
integration step. There is no unsandboxed fallback anywhere; unsupported
policies raise explicitly.
"""

from eventide.native_sandbox.policy import (
    SUPPORT_MATRIX,
    NativeBackendKind,
    NativeSandboxPolicy,
    NativeSandboxProfile,
    UnsupportedPolicyError,
    check_backend_support,
    default_protected_paths,
)
from eventide.native_sandbox.probe import (
    BackendDetection,
    detect_backend,
    detect_native_backends,
    preferred_backend,
)
from eventide.native_sandbox.runner import (
    InterpreterInfo,
    LaunchSpec,
    NativeSandboxError,
    NativeSandboxRunner,
    NativeSandboxUnavailableError,
    WorkerCrashedError,
    WorkerLaunchError,
    WorkerOperationError,
    WorkerProtocolError,
)
from eventide.native_sandbox.worker import PROTOCOL_VERSION, WorkerGrant

__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORT_MATRIX",
    "BackendDetection",
    "InterpreterInfo",
    "LaunchSpec",
    "NativeBackendKind",
    "NativeSandboxError",
    "NativeSandboxPolicy",
    "NativeSandboxProfile",
    "NativeSandboxRunner",
    "NativeSandboxUnavailableError",
    "UnsupportedPolicyError",
    "WorkerCrashedError",
    "WorkerGrant",
    "WorkerLaunchError",
    "WorkerOperationError",
    "WorkerProtocolError",
    "check_backend_support",
    "default_protected_paths",
    "detect_backend",
    "detect_native_backends",
    "preferred_backend",
]
