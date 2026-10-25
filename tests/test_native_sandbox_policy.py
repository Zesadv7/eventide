"""Offline policy, capability-matrix and default-path tests for native sandbox."""

from pathlib import Path

import pytest

from eventide.native_sandbox.policy import (
    SUPPORT_MATRIX,
    NativeBackendKind,
    NativeSandboxPolicy,
    NativeSandboxProfile,
    UnsupportedPolicyError,
    check_backend_support,
    default_protected_paths,
)


def make_policy(tmp_path: Path, **overrides: object) -> NativeSandboxPolicy:
    values: dict[str, object] = {
        "profile": NativeSandboxProfile.WORKSPACE_WRITE,
        "workspace_root": tmp_path / "ws",
        "tmp_root": tmp_path / "scratch",
    }
    values.update(overrides)
    return NativeSandboxPolicy(**values)  # type: ignore[arg-type]


def test_policy_canonicalizes_paths(tmp_path: Path) -> None:
    nested = tmp_path / "link"
    nested.symlink_to(tmp_path / "ws", target_is_directory=True)
    policy = make_policy(tmp_path, workspace_root=nested)
    assert policy.workspace_root == (tmp_path / "ws").resolve()


def test_policy_rejects_relative_and_control_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_policy(tmp_path, workspace_root="relative/ws")
    with pytest.raises(ValueError):
        make_policy(tmp_path, tmp_root=tmp_path / "sc\x00ratch")


def test_policy_rejects_tmp_inside_workspace_and_reverse(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_policy(tmp_path, tmp_root=tmp_path / "ws" / "scratch")
    with pytest.raises(ValueError):
        make_policy(tmp_path, workspace_root=tmp_path / "scratch" / "ws")


def test_policy_rejects_protected_equal_to_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_policy(tmp_path, protected_roots=(tmp_path / "ws",))
    with pytest.raises(ValueError):
        make_policy(tmp_path, protected_roots=(tmp_path / "scratch",))


def test_policy_rejects_tmp_inside_protected_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_policy(
            tmp_path, protected_roots=(tmp_path / "guard",), tmp_root=tmp_path / "guard" / "tmp"
        )


def test_policy_grant_matches_fields(tmp_path: Path) -> None:
    policy = make_policy(
        tmp_path,
        read_only_roots=(tmp_path / "deps",),
        protected_roots=(tmp_path / "state",),
    )
    grant = policy.grant()
    assert grant.profile == "workspace-write"
    assert grant.workspace_root == str((tmp_path / "ws").resolve())
    assert grant.read_only_roots == (str((tmp_path / "deps").resolve()),)
    assert grant.protected_roots == (str((tmp_path / "state").resolve()),)


def test_default_protected_paths_include_state_git_and_legacy(tmp_path: Path) -> None:
    roots = default_protected_paths(
        tmp_path / "ws",
        state_root=tmp_path / "state",
        git_common_dir=tmp_path / "main" / ".git",
    )
    assert (tmp_path / "ws" / ".eventide").resolve() in roots
    assert (tmp_path / "state").resolve() in roots
    assert (tmp_path / "main" / ".git").resolve() in roots


def test_network_and_subprocess_allow_refused_for_every_backend(tmp_path: Path) -> None:
    for backend in NativeBackendKind:
        with pytest.raises(UnsupportedPolicyError):
            check_backend_support(make_policy(tmp_path, allow_network=True), backend)
        with pytest.raises(UnsupportedPolicyError):
            check_backend_support(make_policy(tmp_path, allow_subprocess=True), backend)


def test_bubblewrap_refuses_read_only_root_inside_workspace(tmp_path: Path) -> None:
    policy = make_policy(tmp_path, read_only_roots=(tmp_path / "ws" / "vendor",))
    with pytest.raises(UnsupportedPolicyError):
        check_backend_support(policy, NativeBackendKind.BUBBLEWRAP)
    check_backend_support(policy, NativeBackendKind.SEATBELT)


def test_seatbelt_accepts_protected_root_inside_workspace(tmp_path: Path) -> None:
    policy = make_policy(tmp_path, protected_roots=(tmp_path / "ws" / ".eventide",))
    check_backend_support(policy, NativeBackendKind.SEATBELT)
    check_backend_support(policy, NativeBackendKind.BUBBLEWRAP)


def test_support_matrix_matches_documented_levels() -> None:
    assert SUPPORT_MATRIX[(NativeBackendKind.SEATBELT, "subprocess_deny")] == "full"
    assert SUPPORT_MATRIX[(NativeBackendKind.BUBBLEWRAP, "subprocess_deny")] == "partial"
    for backend in NativeBackendKind:
        assert SUPPORT_MATRIX[(backend, "network_allow_loopback_guard")] == "unsupported"
        assert SUPPORT_MATRIX[(backend, "network_deny")] == "full"


def test_unsupported_policy_error_is_value_error(tmp_path: Path) -> None:
    assert issubclass(UnsupportedPolicyError, ValueError)
