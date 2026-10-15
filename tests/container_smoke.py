"""Explicit deployment check using disposable Docker containers and named volumes.

Run after building the image: python tests/container_smoke.py --image eventide:validation
No user state, project directory, model credentials, or provider requests are used.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def verify(image: str) -> dict[str, object]:
    name = "eventide-validation-" + uuid.uuid4().hex[:12]
    state, project = name + "-state", name + "-project"
    address = ""

    def request(path: str, data: object = None, method: str = "GET", **headers: str):
        body = None if data is None else json.dumps(data).encode()
        with urlopen(
            Request(
                address + path,
                data=body,
                method=method,
                headers={"Content-Type": "application/json", **headers},
            ),
            timeout=10,
        ) as response:
            return response.read()

    def boot(peer: str | None = None) -> None:
        nonlocal address
        options = ["--env", f"EVENTIDE_LOCAL_PROXY_PEERS={peer}"] if peer else []
        docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8000",
            "--mount",
            f"type=volume,src={state},dst=/data/eventide",
            "--mount",
            f"type=volume,src={project},dst=/workspaces/demo",
            *options,
            image,
        )
        address = "http://" + docker("port", name, "8000/tcp").splitlines()[0]
        deadline = time.monotonic() + 40
        while True:
            try:
                assert json.loads(request("/healthz"))["status"] == "ok"
                return
            except (URLError, TimeoutError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise AssertionError("Container failed to become healthy") from None
                time.sleep(0.2)

    def reject(path: str, data: object, **headers: str) -> None:
        try:
            request(path, data, "POST", **headers)
        except HTTPError as exc:
            assert exc.code == 403, exc.code
        else:
            raise AssertionError("Expected local management request to be rejected")

    docker("volume", "create", state)
    docker("volume", "create", project)
    try:
        boot()
        # Learn the socket peer only for this disposable test. Real deployment
        # must explicitly configure its own publishing peer; no runtime detection.
        logs = subprocess.run(
            ["docker", "logs", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        match = re.search(r'INFO:\s+(.+):\d+ - "GET /healthz HTTP/1.1"', logs.stdout + logs.stderr)
        assert match, "Health access log did not identify the forwarding peer"
        peer = str(ipaddress.ip_address(match[1].strip("[]")))
        if not ipaddress.ip_address(peer).is_loopback:
            reject("/api/workspaces", {"path": "/workspaces/demo"}, Origin=address)
        docker("rm", "--force", name)
        boot(peer)
        origin = {"Origin": address}
        assert docker("exec", name, "id", "-u") == "1000"
        assert docker("exec", name, "pwd") == "/workspaces/demo"
        assert (
            docker("exec", name, "python", "-c", "import shutil; print(shutil.which('node'))")
            == "None"
        )
        docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path; Path('writable-check').write_text('retained')",
        )
        assert b"<html" in request("/workbench/")
        manifest = json.loads(request("/workbench/manifest.json"))
        for path, checksum in manifest["files"].items():
            assert hashlib.sha256(request("/workbench/" + path)).hexdigest() == checksum, path
        workspaces = json.loads(request("/api/workspaces"))
        assert {workspace["path"] for workspace in workspaces} == {"/workspaces/demo"}
        workspace = json.loads(
            request(
                "/api/workspaces",
                {"path": "/workspaces/demo"},
                "POST",
                **origin,
            )
        )
        session = json.loads(
            request(
                "/api/sessions",
                {"workspace_id": workspace["workspace_id"]},
                "POST",
                **origin,
            )
        )["session_id"]
        # Only save a fake connection; never probe or send a model request.
        request(
            "/api/config/provider",
            {
                "provider": "openai_compatible",
                "model": "verification-only",
                "base_url": "http://127.0.0.1:1/v1",
                "api_key": "unused-verification-value",
            },
            "PUT",
            **origin,
        )
        if not ipaddress.ip_address(peer).is_loopback:
            reject("/api/workspaces", {"path": "/workspaces/demo"})
        reject("/api/workspaces", {"path": "/workspaces/demo"}, Origin="https://invalid.example")
        docker("rm", "--force", name)
        boot(peer)
        assert json.loads(request("/api/sessions/" + session))["session_id"] == session
        provider = json.loads(request("/api/config/provider"))
        assert provider["model"] == "verification-only"
        assert provider["api_key_configured"]
        assert docker("exec", name, "cat", "writable-check") == "retained"
        return {
            "image": image,
            "peer": peer,
            "assets_verified": len(manifest["files"]),
            "nonroot": True,
            "node_required": False,
            "management_origin_checks": "passed",
            "restart_persistence": "passed",
            "model_requests": 0,
        }
    finally:
        docker("rm", "--force", name, check=False)
        docker("volume", "rm", state, project)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    print(json.dumps(verify(parser.parse_args().image), ensure_ascii=False, indent=2))
