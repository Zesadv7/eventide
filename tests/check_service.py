"""Offline CI deployment smoke using a disposable service process and state root."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def check_service() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="eventide-service-smoke-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "EVENTIDE_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "EVENTIDE_SECRET_KEY",
                "EVENTIDE_BASE_URL",
                "ANTHROPIC_BASE_URL",
            }
        }
        env.update(
            PYTHONPATH=str(repository),
            EVENTIDE_STATE_DIR=str(root / "state"),
            EVENTIDE_PROVIDER="anthropic",
            EVENTIDE_LOCAL_PROXY_PEERS="",
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "eventide", "--port", str(port), "--workspace", str(project)],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            address = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise AssertionError(
                        "Service exited before readiness: " + process.communicate()[1]
                    )
                try:
                    with urlopen(address + "/healthz", timeout=1) as response:
                        assert json.load(response) == {"status": "ok", "runtime": "eventide"}
                    break
                except URLError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("Service readiness timed out") from None
                    time.sleep(0.05)
            with urlopen(address, timeout=3) as response:
                assert response.url == address + "/workbench/"
                assert b'<div id="app">' in response.read()
            with urlopen(address + "/api/workspaces", timeout=3) as response:
                assert [item["path"] for item in json.load(response)] == [str(project.resolve())]
            with urlopen(address + "/api/costs", timeout=3) as response:
                assert json.load(response)["attempt_count"] == 0
        finally:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        # Uvicorn re-raises the handled signal after its graceful shutdown.
        allowed = {0, 1} if os.name == "nt" else {0, -signal.SIGTERM}
        assert process.returncode in allowed
    print("Offline service readiness, Vue redirect, isolated project and zero model calls passed")


if __name__ == "__main__":
    check_service()
