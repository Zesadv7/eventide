from pathlib import Path

from eventide.native_sandbox.bubblewrap import build_bubblewrap_launch
from eventide.native_sandbox.policy import NativeSandboxPolicy, NativeSandboxProfile
from eventide.native_sandbox.seatbelt import build_seatbelt_profile
from eventide.native_sandbox.worker import GRANT_ENV_VAR


def test_linux_launch_retains_worker_grant_and_installed_code(tmp_path):
    policy = NativeSandboxPolicy(
        NativeSandboxProfile.READ_ONLY, tmp_path / "ws", tmp_path / "scratch"
    )
    argv = build_bubblewrap_launch(
        policy,
        bwrap="/usr/bin/bwrap",
        interpreter=Path("/opt/python/bin/python"),
        base_prefix=Path("/opt/python"),
        prefix=Path("/opt/venv"),
        package_parent=Path("/opt/eventide"),
    )
    triples = [argv[i : i + 3] for i in range(len(argv) - 2)]
    assert ["--setenv", GRANT_ENV_VAR, policy.grant().encode_env()] in triples
    assert ["--ro-bind", "/opt/eventide/eventide/native_sandbox/worker.py",
            "/opt/eventide/eventide/native_sandbox/worker.py"] in triples


def test_seatbelt_readonly_dependency_stays_readonly_inside_workspace(tmp_path):
    policy = NativeSandboxPolicy(
        NativeSandboxProfile.WORKSPACE_WRITE,
        tmp_path / "ws",
        tmp_path / "scratch",
        read_only_roots=(tmp_path / "ws" / "deps",),
    )
    profile = build_seatbelt_profile(policy, Path("/usr/bin/python3"))
    assert f'(deny file-write* (subpath "{policy.read_only_roots[0]}"))' in profile
