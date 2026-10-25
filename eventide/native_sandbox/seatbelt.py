"""macOS Seatbelt (sandbox-exec) profile text and argv construction.

The profile uses a targeted-deny style: writes, network and process execution
are allowlisted, reads are default-allowed except for protected roots. Seatbelt
gives precedence to the most specific path filter, so protected carve-outs win
over broader allows. The generated text is deterministic and covered by offline
tests; whether a real kernel enforces it is verified in the separate MK-21
real-machine acceptance step, not here.
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


def build_seatbelt_profile(policy: NativeSandboxPolicy, interpreter: Path) -> str:
    lines = [
        "(version 1)",
        "; Eventide native sandbox worker profile (targeted-deny).",
        f"; profile={policy.profile.value} network={'allow' if policy.allow_network else 'deny'}",
        "; Seatbelt gives precedence to the most specific path filter, so explicit",
        "; denies below carve protected roots out of broader allows.",
        "; Generated offline; enforcement on a real kernel is verified separately.",
        "",
        "(allow default)",
        "(deny network*)",
        "(deny process-exec*)",
        f"(allow process-exec {_literal(interpreter)})",
        "(allow process-fork)",
        "(deny file-write*)",
        f"(allow file-write* {_subpath(policy.tmp_root)})",
    ]
    if policy.profile.value == "workspace-write":
        lines.append(f"(allow file-write* {_subpath(policy.workspace_root)})")
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
