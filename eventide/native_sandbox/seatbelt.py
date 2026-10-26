"""macOS Seatbelt (sandbox-exec) profile text and argv construction.

The profile denies operations by default and grants read access only to the
execution copy, scratch, explicit dependencies, and Python runtime resources.
Protected paths remain explicit carve-outs. Kernel verification is a separate
acceptance step; generated text alone is not proof of enforcement.
"""

from __future__ import annotations

from pathlib import Path

from eventide.native_sandbox.policy import NativeSandboxPolicy
from eventide.native_sandbox.worker import WORKER_BOOTSTRAP

SANDBOX_EXEC_NAME = "sandbox-exec"


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _subpath(path: Path) -> str:
    return f"(subpath {_quote(str(path))})"


def _literal(path: Path) -> str:
    return f"(literal {_quote(str(path))})"


def build_seatbelt_profile(
    policy: NativeSandboxPolicy, interpreter: Path, *,
    runtime_roots: tuple[Path, ...] = (), worker_path: Path | None = None,
) -> str:
    lines = [
        "(version 1)",
        "; Eventide native sandbox worker profile (deny-default).",
        f"; profile={policy.profile.value} network={'allow' if policy.allow_network else 'deny'}",
        "; Seatbelt gives precedence to the most specific path filter, so explicit",
        "; denies below carve protected roots out of broader allows.",
        "; Generated offline; enforcement on a real kernel is verified separately.",
        "",
        "(deny default)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        "(deny network*)",
        "(deny process-exec*)",
        f"(allow process-exec {_literal(interpreter)})",
        "(deny process-fork)",
        "(deny file-write*)",
        f"(allow file-write* {_subpath(policy.tmp_root)})",
    ]
    for root in (policy.workspace_root, policy.tmp_root, *policy.read_only_roots,
                 *runtime_roots, Path("/System/Library"), Path("/usr/lib")):
        lines.append(f"(allow file-read* {_subpath(root)})")
    for path in (interpreter, Path("/dev/null"), Path("/dev/urandom"), Path("/dev/random"),
                 *((worker_path,) if worker_path else ())):
        lines.append(f"(allow file-read* {_literal(path)})")
    if policy.profile.value == "workspace-write":
        lines.append(f"(allow file-write* {_subpath(policy.workspace_root)})")
    for root in policy.read_only_roots:
        lines.append(f"(deny file-write* {_subpath(root)})")
    for root in policy.protected_roots:
        lines.append(f"(deny file-read* {_subpath(root)})")
        lines.append(f"(deny file-write* {_subpath(root)})")
    return "\n".join(lines) + "\n"


def build_seatbelt_launch(
    policy: NativeSandboxPolicy,
    *,
    sandbox_exec: Path | str,
    interpreter: Path,
    profile_path: Path,
    package_parent: Path,
) -> list[str]:
    """Full argv for launching the worker under Seatbelt.

    ``profile_path`` must already contain ``build_seatbelt_profile`` output and
    live outside the workspace; the host removes it after the worker stops.
    """
    if Path(sandbox_exec).name != SANDBOX_EXEC_NAME:
        raise ValueError("sandbox_exec must point to sandbox-exec")
    return [
        str(sandbox_exec),
        "-f",
        str(profile_path),
        str(interpreter),
        "-I",
        "-c",
        WORKER_BOOTSTRAP,
        str(package_parent),
    ]
