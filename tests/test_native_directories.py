from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eventide.http_api import directories
from eventide.http_api import native_directories as native


async def connected() -> bool:
    return False


class Process:
    pid = 54321

    def __init__(self, output: bytes = b'"/tmp/project"\n', code: int = 0) -> None:
        self.returncode: int | None = None
        self.output = output
        self.code = code
        self.finish = asyncio.Event()
        self.reaped = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self.finish.wait()
        self.returncode = self.code
        return self.output, b""

    async def wait(self) -> int:
        await self.reaped.wait()
        self.returncode = self.code
        return self.code


@pytest.mark.asyncio
async def test_selection_preserves_whitespace_and_minimal_environment(monkeypatch):
    process = Process(b'"/tmp/ project \\n"\n')
    process.finish.set()
    process.reaped.set()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(
        native, "chooser_command", lambda: ("/usr/bin/python3", "-I", "-c", native.GTK_SCRIPT)
    )
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("EVENTIDE_API_KEY", "must-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    chooser = native.NativeDirectoryChooser()
    result = await chooser.choose(connected)
    assert result.model_dump() == {"status": "selected", "path": "/tmp/ project \n"}
    assert not chooser.busy
    assert "EVENTIDE_API_KEY" not in spawn.call_args.kwargs["env"]
    assert "PYTHONPATH" not in spawn.call_args.kwargs["env"]
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert spawn.call_args.args[1] == "-I"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,code,status",
    [
        (b"null\n", 0, "cancelled"),
        (b"", 1, "unavailable"),
        (b'"relative"', 0, "unavailable"),
        (b"{}", 0, "unavailable"),
        (b"invalid", 0, "unavailable"),
    ],
)
async def test_cancel_and_missing_dependency(monkeypatch, output, code, status):
    process = Process(output, code)
    process.finish.set()
    process.reaped.set()
    monkeypatch.setattr(native, "chooser_command", lambda: ("/usr/bin/python3",))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert (await native.NativeDirectoryChooser().choose(connected)).status == status


@pytest.mark.asyncio
async def test_repeated_cancellation_holds_lock_until_process_reaped(monkeypatch):
    process = Process()
    spawn = AsyncMock(return_value=process)
    kills = []
    monkeypatch.setattr(native, "chooser_command", lambda: ("/usr/bin/python3",))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(native.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    chooser = native.NativeDirectoryChooser()
    task = asyncio.create_task(chooser.choose(connected))
    await asyncio.sleep(0.01)
    assert (await chooser.choose(connected)).status == "busy"
    task.cancel()
    await asyncio.sleep(0.06)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    assert chooser.busy
    assert (await chooser.choose(connected)).status == "busy"
    assert spawn.await_count == 1
    assert kills == [(process.pid, signal.SIGTERM)]
    process.finish.set()
    process.reaped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not chooser.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect,status", [(False, "timed_out"), (True, "cancelled")])
async def test_timeout_and_disconnect_reap(monkeypatch, disconnect, status):
    process = Process()
    monkeypatch.setattr(native, "chooser_command", lambda: ("/usr/bin/python3",))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    def kill(pid, sig):
        process.finish.set()
        process.reaped.set()

    monkeypatch.setattr(native.os, "killpg", kill)
    chooser = native.NativeDirectoryChooser(timeout=0)
    assert (await chooser.choose(AsyncMock(return_value=disconnect))).status == status
    assert process.reaped.is_set() and not chooser.busy


@pytest.mark.asyncio
async def test_missing_executable_and_headless(monkeypatch):
    monkeypatch.setattr(native, "chooser_command", lambda: None)
    chooser = native.NativeDirectoryChooser()
    assert (await chooser.choose(connected)).status == "unavailable"
    monkeypatch.setattr(native, "chooser_command", lambda: ("/usr/bin/python3",))
    monkeypatch.setattr(
        native.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)
    )
    assert (await chooser.choose(connected)).status == "unavailable"
    assert not chooser.busy


def test_platform_commands(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "darwin")
    assert native.chooser_command() == ("/usr/bin/osascript", "-e", native.MAC_SCRIPT)
    assert native.MAC_SCRIPT.startswith("activate")
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert native.chooser_command() is None
    monkeypatch.setenv("DISPLAY", ":0")
    assert native.chooser_command() == ("/usr/bin/python3", "-I", "-c", native.GTK_SCRIPT)
    monkeypatch.setattr(native.sys, "platform", "win32")
    assert native.chooser_command() is None


def test_origin_and_local_connection_required(monkeypatch):
    choose = AsyncMock(return_value=native.DirectoryChoice(status="cancelled"))
    monkeypatch.setattr(directories.chooser, "choose", choose)
    app = FastAPI()
    app.include_router(directories.create_router(None))
    with TestClient(app) as client:
        assert client.post("/api/directories/choose").status_code == 403
        assert (
            client.post(
                "/api/directories/choose", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        result = client.post("/api/directories/choose", headers={"Origin": "http://testserver"})
        assert result.json() == {"status": "cancelled"}
    with TestClient(app, client=("198.51.100.2", 1234), base_url="http://localhost") as client:
        assert (
            client.post(
                "/api/directories/choose", headers={"Origin": "http://localhost"}
            ).status_code
            == 403
        )
    assert choose.await_count == 1


@pytest.mark.asyncio
async def test_cancel_during_spawn_still_reaps(monkeypatch):
    process = Process()
    spawned = asyncio.Event()
    release = asyncio.Event()

    async def spawn(*args, **kwargs):
        spawned.set()
        await release.wait()
        return process

    def kill(pid, sig):
        process.finish.set()
        process.reaped.set()

    monkeypatch.setattr(native, "chooser_command", lambda: ("/usr/bin/python3",))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(native.os, "killpg", kill)
    chooser = native.NativeDirectoryChooser()
    task = asyncio.create_task(chooser.choose(connected))
    await spawned.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert chooser.busy
    assert (await chooser.choose(connected)).status == "busy"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.reaped.is_set() and not chooser.busy


@pytest.mark.asyncio
async def test_termination_escalates_and_waits(monkeypatch):
    process = Process()
    signals = []

    def kill(pid, sig):
        signals.append(sig)
        if sig == signal.SIGKILL:
            process.finish.set()
            process.reaped.set()

    monkeypatch.setattr(native.os, "killpg", kill)
    await native.NativeDirectoryChooser._reap(process)
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_macos_only_removes_protocol_newline(monkeypatch):
    process = Process(b"/tmp/trailing space \n\n")
    process.finish.set()
    process.reaped.set()
    monkeypatch.setattr(native, "chooser_command", lambda: ("/usr/bin/osascript",))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await native.NativeDirectoryChooser().choose(connected)
    assert result.path == "/tmp/trailing space \n"
