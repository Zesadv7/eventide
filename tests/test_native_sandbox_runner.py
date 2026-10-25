"""Offline runner tests: real worker process, explicit failures, no fallback."""

from pathlib import Path

import pytest

from eventide.native_sandbox.policy import (
    NativeBackendKind,
    NativeSandboxPolicy,
    NativeSandboxProfile,
    UnsupportedPolicyError,
)
from eventide.native_sandbox.runner import (
    InterpreterInfo,
    LaunchSpec,
    NativeSandboxRunner,
    NativeSandboxUnavailableError,
    WorkerLaunchError,
    WorkerOperationError,
    WorkerProtocolError,
    _unsandboxed_test_launcher,
)
from eventide.native_sandbox.worker import GRANT_ENV_VAR, PROTOCOL_VERSION, WORKER_BOOTSTRAP


def make_policy(tmp_path: Path, **overrides: object) -> NativeSandboxPolicy:
    values: dict[str, object] = {
        "profile": NativeSandboxProfile.WORKSPACE_WRITE,
        "workspace_root": tmp_path / "ws",
        "tmp_root": tmp_path / "scratch",
    }
    values.update(overrides)
    return NativeSandboxPolicy(**values)  # type: ignore[arg-type]


async def test_runner_round_trip_over_real_worker_process(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    runner = NativeSandboxRunner(
        make_policy(tmp_path),
        backend=NativeBackendKind.SEATBELT,
        launcher=_unsandboxed_test_launcher,
    )
    try:
        await runner.start()
        written = await runner.call(
            "write_file",
            path=str(tmp_path / "ws" / "notes.txt"),
            content_b64="aGVsbG8=",
        )
        assert written == {"size": 5}
        read = await runner.call("read_file", path=str(tmp_path / "ws" / "notes.txt"))
        assert read["size"] == 5
    finally:
        await runner.aclose()


async def test_runner_operation_error_is_recoverable(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    runner = NativeSandboxRunner(
        make_policy(tmp_path),
        backend=NativeBackendKind.SEATBELT,
        launcher=_unsandboxed_test_launcher,
    )
    try:
        await runner.start()
        with pytest.raises(WorkerOperationError) as excinfo:
            await runner.call("read_file", path=str(tmp_path / "ws" / "missing.txt"))
        assert excinfo.value.code == "io_error"
        handshake = await runner.call("ping")
        assert handshake["protocol"] == PROTOCOL_VERSION
    finally:
        await runner.aclose()


async def test_runner_failed_handshake_raises_launch_error(tmp_path: Path) -> None:
    def crash_launcher(policy: NativeSandboxPolicy, interpreter: InterpreterInfo) -> LaunchSpec:
        crash_argv = (str(interpreter.executable), "-I", "-c", "import sys; sys.exit(3)")
        return LaunchSpec(argv=crash_argv, env={})

    runner = NativeSandboxRunner(
        make_policy(tmp_path),
        backend=NativeBackendKind.SEATBELT,
        launcher=crash_launcher,
    )
    with pytest.raises(WorkerLaunchError):
        await runner.start()


async def test_runner_launcher_requires_backend_kind(tmp_path: Path) -> None:
    runner = NativeSandboxRunner(make_policy(tmp_path), launcher=_unsandboxed_test_launcher)
    with pytest.raises(ValueError):
        await runner.start()


async def test_runner_without_backend_is_explicit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eventide.native_sandbox.runner.preferred_backend", lambda: None)
    runner = NativeSandboxRunner(make_policy(tmp_path))
    with pytest.raises(NativeSandboxUnavailableError):
        await runner.start()


async def test_runner_never_starts_for_unsupported_policy(tmp_path: Path) -> None:
    runner = NativeSandboxRunner(
        make_policy(tmp_path, allow_network=True),
        backend=NativeBackendKind.SEATBELT,
        launcher=_unsandboxed_test_launcher,
    )
    with pytest.raises(UnsupportedPolicyError):
        await runner.start()


async def test_runner_rejects_double_start(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    runner = NativeSandboxRunner(
        make_policy(tmp_path),
        backend=NativeBackendKind.SEATBELT,
        launcher=_unsandboxed_test_launcher,
    )
    try:
        await runner.start()
        with pytest.raises(WorkerProtocolError):
            await runner.start()
    finally:
        await runner.aclose()


def test_unsandboxed_launcher_spec_shape(tmp_path: Path) -> None:
    interpreter = InterpreterInfo.resolve()
    spec = _unsandboxed_test_launcher(make_policy(tmp_path), interpreter)
    assert spec.argv[0] == str(interpreter.executable)
    assert list(spec.argv[1:4]) == ["-I", "-c", WORKER_BOOTSTRAP]
    assert spec.argv[4] == str(interpreter.package_parent)
    assert GRANT_ENV_VAR in spec.env
    assert spec.env["TMPDIR"] == str((tmp_path / "scratch").resolve())
