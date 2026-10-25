"""Offline Seatbelt profile/argv and bubblewrap argv construction tests."""

from pathlib import Path

import pytest

from eventide.native_sandbox.bubblewrap import build_bubblewrap_launch
from eventide.native_sandbox.policy import (
    NativeSandboxPolicy,
    NativeSandboxProfile,
    UnsupportedPolicyError,
)
from eventide.native_sandbox.seatbelt import build_seatbelt_launch, build_seatbelt_profile
from eventide.native_sandbox.worker import WORKER_BOOTSTRAP

INTERPRETER = Path("/usr/local/bin/python3.12")
PACKAGE_PARENT = Path("/opt/eventide")


def seatbelt_policy(tmp_path: Path, profile: NativeSandboxProfile) -> NativeSandboxPolicy:
    return NativeSandboxPolicy(
        profile=profile,
        workspace_root=tmp_path / "ws",
        tmp_root=tmp_path / "scratch",
        protected_roots=(tmp_path / "state",),
    )


def test_seatbelt_profile_deny_write_and_network(tmp_path: Path) -> None:
    text = build_seatbelt_profile(
        seatbelt_policy(tmp_path, NativeSandboxProfile.READ_ONLY), INTERPRETER
    )
    assert text.startswith("(version 1)\n")
    assert "(deny network*)" in text
    assert "(deny process-exec*)" in text
    assert f'(allow process-exec (literal "{INTERPRETER}"))' in text
    assert "(allow process-fork)" in text
    assert "(deny file-write*)" in text
    assert f'(allow file-write* (subpath "{tmp_path / "scratch"}"))' in text
    assert f'(allow file-write* (subpath "{tmp_path / "ws"}"))' not in text
    assert f'(deny file-read* (subpath "{tmp_path / "state"}"))' in text
    assert f'(deny file-write* (subpath "{tmp_path / "state"}"))' in text


def test_seatbelt_profile_workspace_write_allows_workspace(tmp_path: Path) -> None:
    text = build_seatbelt_profile(
        seatbelt_policy(tmp_path, NativeSandboxProfile.WORKSPACE_WRITE), INTERPRETER
    )
    assert f'(allow file-write* (subpath "{tmp_path / "ws"}"))' in text


def test_seatbelt_profile_quotes_special_characters(tmp_path: Path) -> None:
    tricky = tmp_path / 'we"ird path'
    policy = NativeSandboxPolicy(
        profile=NativeSandboxProfile.READ_ONLY,
        workspace_root=tmp_path / "ws",
        tmp_root=tmp_path / "scratch",
        protected_roots=(tricky,),
    )
    text = build_seatbelt_profile(policy, INTERPRETER)
    quoted = str(tricky).replace("\\", "\\\\").replace('"', '\\"')
    expected = f'(deny file-read* (subpath "{quoted}"))'
    assert expected in text


def test_seatbelt_argv_shape(tmp_path: Path) -> None:
    profile_path = tmp_path / "worker.sb"
    argv = build_seatbelt_launch(
        seatbelt_policy(tmp_path, NativeSandboxProfile.READ_ONLY),
        sandbox_exec="/usr/bin/sandbox-exec",
        interpreter=INTERPRETER,
        profile_path=profile_path,
        package_parent=PACKAGE_PARENT,
    )
    assert argv == [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile_path),
        str(INTERPRETER),
        "-I",
        "-c",
        WORKER_BOOTSTRAP,
        str(PACKAGE_PARENT),
    ]


def test_seatbelt_argv_rejects_foreign_binary(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_seatbelt_launch(
            seatbelt_policy(tmp_path, NativeSandboxProfile.READ_ONLY),
            sandbox_exec="/usr/bin/other-tool",
            interpreter=INTERPRETER,
            profile_path=tmp_path / "worker.sb",
            package_parent=PACKAGE_PARENT,
        )


def bubblewrap_policy(tmp_path: Path, profile: NativeSandboxProfile) -> NativeSandboxPolicy:
    return NativeSandboxPolicy(
        profile=profile,
        workspace_root=tmp_path / "ws",
        tmp_root=tmp_path / "scratch",
        read_only_roots=(tmp_path / "deps",),
        protected_roots=(tmp_path / "state",),
    )


def bubblewrap_argv(tmp_path: Path, profile: NativeSandboxProfile) -> list[str]:
    return build_bubblewrap_launch(
        bubblewrap_policy(tmp_path, profile),
        bwrap="/usr/bin/bwrap",
        interpreter=INTERPRETER,
        base_prefix=Path("/usr"),
        prefix=Path("/opt/venv"),
        package_parent=PACKAGE_PARENT,
    )


def test_bubblewrap_argv_unshares_network_and_mounts_allowlist(tmp_path: Path) -> None:
    argv = bubblewrap_argv(tmp_path, NativeSandboxProfile.WORKSPACE_WRITE)
    flags = ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--proc", "--dev")
    for flag in flags:
        assert flag in argv
    assert argv[argv.index("--setenv") + 1] == "TMPDIR"
    assert argv[argv.index("--setenv") + 2] == str((tmp_path / "scratch").resolve())
    assert argv.count("--ro-bind-try") == 4
    for lib in ("/usr/lib", "/lib", "/lib64", "/usr/lib64"):
        assert ["--ro-bind-try", lib, lib] in [argv[i : i + 3] for i in range(len(argv) - 2)]
    triples = [argv[i : i + 3] for i in range(len(argv) - 2)]
    assert ["--ro-bind", str(INTERPRETER), str(INTERPRETER)] in triples
    assert ["--ro-bind", "/usr", "/usr"] in triples
    assert ["--ro-bind", "/opt/venv", "/opt/venv"] in triples
    deps = str((tmp_path / "deps").resolve())
    assert ["--ro-bind", deps, deps] in triples


def test_bubblewrap_workspace_bind_matches_profile(tmp_path: Path) -> None:
    write_argv = bubblewrap_argv(tmp_path, NativeSandboxProfile.WORKSPACE_WRITE)
    read_argv = bubblewrap_argv(tmp_path, NativeSandboxProfile.READ_ONLY)
    workspace = str((tmp_path / "ws").resolve())
    write_triples = [write_argv[i : i + 3] for i in range(len(write_argv) - 2)]
    read_triples = [read_argv[i : i + 3] for i in range(len(read_argv) - 2)]
    assert ["--bind", workspace, workspace] in write_triples
    assert ["--ro-bind", workspace, workspace] in read_triples
    scratch = str((tmp_path / "scratch").resolve())
    assert ["--bind", scratch, scratch] in write_triples


def test_bubblewrap_shadows_protected_inside_mounted_trees(tmp_path: Path) -> None:
    policy = NativeSandboxPolicy(
        profile=NativeSandboxProfile.WORKSPACE_WRITE,
        workspace_root=tmp_path / "ws",
        tmp_root=tmp_path / "scratch",
        protected_roots=(tmp_path / "state", tmp_path / "ws" / ".eventide"),
    )
    argv = build_bubblewrap_launch(
        policy,
        bwrap="/usr/bin/bwrap",
        interpreter=INTERPRETER,
        base_prefix=Path("/usr"),
        prefix=Path("/usr"),
        package_parent=PACKAGE_PARENT,
    )
    assert ["--tmpfs", str((tmp_path / "ws" / ".eventide").resolve())] in [
        argv[i : i + 2] for i in range(len(argv) - 1)
    ]
    # The outside protected root is simply never mounted; no shadow entry.
    assert argv.count("--tmpfs") == 1
    assert argv.index("--tmpfs") > argv.index("--bind", argv.index("--bind") + 1)
    assert argv[-5:] == [str(INTERPRETER), "-I", "-c", WORKER_BOOTSTRAP, str(PACKAGE_PARENT)]
    assert argv[-7] == "--chdir"
    assert argv[-6] == str((tmp_path / "ws").resolve())


def test_bubblewrap_rejects_protected_under_library_bind(tmp_path: Path) -> None:
    policy = NativeSandboxPolicy(
        profile=NativeSandboxProfile.READ_ONLY,
        workspace_root=tmp_path / "ws",
        tmp_root=tmp_path / "scratch",
        protected_roots=(Path("/usr/lib/eventide-guard"),),
    )
    with pytest.raises(UnsupportedPolicyError):
        build_bubblewrap_launch(
            policy,
            bwrap="/usr/bin/bwrap",
            interpreter=INTERPRETER,
            base_prefix=Path("/usr"),
            prefix=Path("/usr"),
            package_parent=PACKAGE_PARENT,
        )
