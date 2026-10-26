"""Linux bubblewrap argv construction for the isolated file worker.

The mount list is the allowlist: only explicitly bound paths exist inside the
namespace, so everything else (including protected runtime state and the shared
Git dir) is unreachable. ``--unshare-all`` removes the network and PID
namespaces. Subprocess denial is partial: interpreter prefixes may contain executable
programs and the interpreter itself stays executable because the worker needs
it. Generated offline; real-kernel verification is a separate MK-21 step.
"""

from __future__ import annotations

from pathlib import Path

from eventide.native_sandbox.policy import NativeSandboxPolicy, UnsupportedPolicyError
from eventide.native_sandbox.worker import GRANT_ENV_VAR, WORKER_BOOTSTRAP

BUBBLEWRAP_NAME = "bwrap"

# Read-only system library locations the interpreter and its shared objects
# usually need. ``--ro-bind-try`` keeps this portable across distributions.
LIB_BIND_PATHS = ("/usr/lib", "/lib", "/lib64", "/usr/lib64")


def _is_within(child: Path, parent: Path) -> bool:
    if child == parent:
        return True
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def build_bubblewrap_launch(
    policy: NativeSandboxPolicy,
    *,
    bwrap: Path | str,
    interpreter: Path,
    base_prefix: Path,
    prefix: Path,
    package_parent: Path,
) -> list[str]:
    if Path(bwrap).name != BUBBLEWRAP_NAME:
        raise ValueError("bwrap must point to the bubblewrap executable")
    for root in policy.protected_roots:
        if any(_is_within(root, Path(lib)) for lib in LIB_BIND_PATHS):
            raise UnsupportedPolicyError(
                "Protected roots under system library bindings cannot be shadowed "
                "reliably; bubblewrap must refuse instead of approximating"
            )
    argv: list[str] = [
        str(bwrap),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--clearenv",
        "--setenv",
        "TMPDIR",
        str(policy.tmp_root),
        "--setenv",
        GRANT_ENV_VAR,
        policy.grant().encode_env(),
    ]
    for lib in LIB_BIND_PATHS:
        argv.extend(["--ro-bind-try", lib, lib])
    argv.extend(["--ro-bind", str(interpreter), str(interpreter)])
    argv.extend(["--ro-bind", str(base_prefix), str(base_prefix)])
    if prefix != base_prefix:
        argv.extend(["--ro-bind", str(prefix), str(prefix)])
    worker_path = package_parent / "eventide/native_sandbox/worker.py"
    argv.extend(["--ro-bind", str(worker_path), str(worker_path)])
    for root in policy.read_only_roots:
        argv.extend(["--ro-bind", str(root), str(root)])
    argv.extend(["--bind", str(policy.tmp_root), str(policy.tmp_root)])
    if policy.profile.value == "workspace-write":
        argv.extend(["--bind", str(policy.workspace_root), str(policy.workspace_root)])
    else:
        argv.extend(["--ro-bind", str(policy.workspace_root), str(policy.workspace_root)])
    # Shadow protected roots that are reachable through mounted trees. Later
    # mounts win, so empty tmpfs mounts hide the original content completely.
    mounted_roots = [
        policy.tmp_root,
        policy.workspace_root,
        *policy.read_only_roots,
        base_prefix,
        prefix,
        worker_path,
    ]
    shadows = [
        root
        for root in policy.protected_roots
        if any(_is_within(root, mounted) for mounted in mounted_roots)
    ]
    shadows.sort(key=lambda path: len(path.parts), reverse=True)
    for root in shadows:
        if root.is_file():
            argv.extend(["--ro-bind", "/dev/null", str(root)])
        else:
            argv.extend(["--tmpfs", str(root)])
    argv.extend(["--chdir", str(policy.workspace_root)])
    argv.extend([str(interpreter), "-I", "-c", WORKER_BOOTSTRAP, str(package_parent)])
    return argv
