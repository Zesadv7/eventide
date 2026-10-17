"""Fixed, optional host desktop chooser. Never executes caller supplied commands."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel

Status = Literal["selected", "cancelled", "unavailable", "busy", "timed_out"]


class DirectoryChoice(BaseModel):
    status: Status
    path: str | None = None


MAC_SCRIPT = """activate
try
    return POSIX path of (choose folder with prompt "选择 Eventide 项目文件夹")
on error number -128
    return ""
end try"""
GTK_SCRIPT = """import json
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
ok, _ = Gtk.init_check([])
if not ok:
    raise SystemExit(2)
dialog = Gtk.FileChooserNative.new("选择 Eventide 项目文件夹", None,
    Gtk.FileChooserAction.SELECT_FOLDER, "选择文件夹", "取消")
try:
    result = dialog.run()
    print(json.dumps(dialog.get_filename() if result == Gtk.ResponseType.ACCEPT else None))
finally:
    dialog.destroy()
"""


def chooser_command() -> tuple[str, ...] | None:
    if sys.platform == "darwin":
        return ("/usr/bin/osascript", "-e", MAC_SCRIPT)
    if sys.platform.startswith("linux") and (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return ("/usr/bin/python3", "-I", "-c", GTK_SCRIPT)
    return None


def chooser_environment() -> dict[str, str]:
    # No provider credentials, PYTHONPATH, loader injection or arbitrary inherited variables.
    allowed = (
        "HOME",
        "USER",
        "LOGNAME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XAUTHORITY",
    )
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        **{key: os.environ[key] for key in allowed if key in os.environ},
    }


class NativeDirectoryChooser:
    def __init__(self, timeout: float = 120) -> None:
        self.timeout = timeout
        self.busy = False

    async def choose(
        self,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> DirectoryChoice:
        if self.busy:
            return DirectoryChoice(status="busy")
        self.busy = True
        cancel = asyncio.Event()
        # Only this owner task may release the slot. Repeated request cancellation cannot
        # interrupt spawning/reaping, orphan a dialog, or admit a second chooser.
        owner = asyncio.create_task(self._own(cancel, disconnected))
        try:
            return await asyncio.shield(owner)
        except asyncio.CancelledError:
            cancel.set()
            while not owner.done():
                try:
                    await asyncio.shield(owner)
                except asyncio.CancelledError:
                    continue
            owner.result()
            raise

    async def _own(
        self,
        cancel: asyncio.Event,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> DirectoryChoice:
        process: asyncio.subprocess.Process | None = None
        output: asyncio.Task[tuple[bytes, bytes]] | None = None
        try:
            command = chooser_command()
            if command is None:
                return DirectoryChoice(status="unavailable")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                env=chooser_environment(),
                cwd="/",
                start_new_session=True,
            )
            output = asyncio.create_task(process.communicate())
            deadline = asyncio.get_running_loop().time() + self.timeout
            while not output.done():
                if cancel.is_set() or await disconnected():
                    return DirectoryChoice(status="cancelled")
                if asyncio.get_running_loop().time() >= deadline:
                    return DirectoryChoice(status="timed_out")
                await asyncio.wait({output}, timeout=0.05)
            stdout, _ = output.result()
            if process.returncode != 0 or len(stdout) > 32768:
                return DirectoryChoice(status="unavailable")
            text = stdout.decode("utf-8")
            path = (
                text.removesuffix("\n") if command[0] == "/usr/bin/osascript" else json.loads(text)
            )
            if path is None or path == "":
                return DirectoryChoice(status="cancelled")
            if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
                return DirectoryChoice(status="unavailable")
            return DirectoryChoice(status="selected", path=path)
        except (OSError, ValueError, UnicodeError):
            return DirectoryChoice(status="unavailable")
        finally:
            try:
                if process is not None:
                    await self._reap(process)
                if output is not None:
                    await output
            finally:
                self.busy = False

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


chooser = NativeDirectoryChooser()
