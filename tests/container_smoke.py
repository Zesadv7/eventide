"""Explicit deployment check using disposable Docker containers and named volumes.

Run after building the image: python tests/container_smoke.py --image eventide:validation
No user state, project directory, model credentials, or network provider requests
are used. The frozen development suite runs through the real offline HTTP path.
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

    created_volumes: list[str] = []
    try:
        for volume in (state, project):
            docker("volume", "create", volume)
            created_volumes.append(volume)
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
        queue = json.loads(request("/api/queue"))
        assert queue["limits"] == {"active_roots": 4, "waiting_roots": 20, "model_requests": 2}
        assert not queue["queued"] and not queue["active"]
        assert all(value == 0 for value in queue["counts"].values())
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
                "expected_config_hash": json.loads(request("/api/config/provider"))["config_hash"],
            },
            "PUT",
            **{**origin, "Idempotency-Key": "container-configuration"},
        )
        if not ipaddress.ip_address(peer).is_loopback:
            reject("/api/workspaces", {"path": "/workspaces/demo"})
        reject("/api/workspaces", {"path": "/workspaces/demo"}, Origin="https://invalid.example")
        source_listing = docker("exec", name, "python", "-c", (
            "from pathlib import Path; "
            "print(sorted(p.as_posix() for p in Path('.').rglob('*')))"
        ))
        suites = json.loads(request("/api/evaluations/suites"))
        suite = next(item for item in suites if item["id"] == "engineering-v1")
        evaluation_body = {
            "suite_id": suite["id"],
            "suite_sha256": suite["suite_sha256"],
            "split": "development",
            "mode": "offline",
        }
        evaluation_path = f"/api/projects/{workspace['project_id']}/evaluations"
        evaluation_headers = {**origin, "Idempotency-Key": name + "-evaluation"}
        accepted = json.loads(request(
            evaluation_path, evaluation_body, "POST", **evaluation_headers,
        ))
        replayed = json.loads(request(
            evaluation_path, evaluation_body, "POST", **evaluation_headers,
        ))
        assert accepted == replayed, "An identical retry created another evaluation"
        assert accepted["evaluation_id"] == accepted["run_id"]
        evaluation_id = accepted["evaluation_id"]
        detail_path = f"/api/evaluations/{evaluation_id}"
        deadline = time.monotonic() + 180
        while True:
            evaluation = json.loads(request(detail_path))
            if evaluation["status"] not in {"queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Container offline evaluation did not finish")
            time.sleep(0.3)
        assert evaluation["status"] == "completed", evaluation
        assert evaluation["measurement_kind"] == "scripted_mechanism_validation"
        assert evaluation["billing_applicable"] is False
        assert evaluation["total"] == evaluation["attempted"] == evaluation["passed"] == 12
        assert evaluation["failed"] == evaluation["remaining"] == 0
        assert evaluation["report_available"] is True
        report_path = f"{detail_path}/report"
        report = request(report_path)
        report_data = json.loads(report)
        assert report_data["total"] == report_data["attempted"] == report_data["passed"] == 12
        assert len(report_data["results"]) == 12
        assert b"unused-verification-value" not in report
        assert docker("exec", name, "cat", "writable-check") == "retained"
        assert docker("exec", name, "python", "-c", (
            "from pathlib import Path; "
            "print(sorted(p.as_posix() for p in Path('.').rglob('*')))"
        )) == source_listing, "Evaluation wrote into the selected project"
        docker("exec", name, "python", "-c", (
            "import subprocess; from pathlib import Path; "
            "subprocess.run(['git','init','.'],check=True,capture_output=True); "
            "Path('.git/info/exclude').write_text('.eventide/\\n'); "
            "Path('index-fixture.txt').write_text('container retrieval validation')"
        ))
        index_config = json.loads(request("/api/indexes/configuration"))
        index_body = {"config_hash": index_config["config_hash"]}
        index_endpoint = f"/api/projects/{workspace['project_id']}/indexes"
        index_headers = {**origin, "Idempotency-Key": name + "-index"}
        index_receipt = json.loads(request(index_endpoint, index_body, "POST", **index_headers))
        assert json.loads(request(index_endpoint, index_body, "POST", **index_headers)) == (
            index_receipt
        )
        index_path = index_receipt["status_url"]
        deadline = time.monotonic() + 30
        while True:
            index_detail = json.loads(request(index_path))
            if index_detail["status"] not in {"queued", "running"}:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Container index build did not finish")
            time.sleep(0.1)
        assert index_detail["status"] == "completed", index_detail
        assert index_detail["published"]["stats"]["files"] >= 1
        indexed_files = json.loads(docker("exec", name, "python", "-c", (
            "import json; from pathlib import Path; "
            "from eventide.rag.search import sparse_search; "
            "print(json.dumps([h.chunk.file for h in "
            "sparse_search(Path.cwd(), 'container retrieval validation')]))"
        )))
        assert "index-fixture.txt" in indexed_files
        index_usage = json.loads(request(f"/api/runs/{index_receipt['run_id']}/usage"))
        assert index_usage["attempt_count"] == 0
        docker("rm", "--force", name)
        boot(peer)
        index_headers["Origin"] = address
        assert json.loads(request(index_path)) == index_detail
        assert json.loads(request(index_endpoint, index_body, "POST", **index_headers)) == (
            index_receipt
        )
        assert json.loads(request("/api/sessions/" + session))["session_id"] == session
        provider = json.loads(request("/api/config/provider"))
        assert provider["model"] == "verification-only"
        assert provider["api_key_configured"]
        assert docker("exec", name, "cat", "writable-check") == "retained"
        assert json.loads(request("/api/queue")) == queue
        recovered = json.loads(request(detail_path))
        assert recovered["status"] == "completed"
        assert recovered["passed"] == 12
        assert request(report_path) == report, "Persisted evaluation report changed on restart"
        return {
            "image": image,
            "peer": peer,
            "assets_verified": len(manifest["files"]),
            "nonroot": True,
            "node_required": False,
            "management_origin_checks": "passed",
            "restart_persistence": "passed",
            "external_model_requests": 0,
            "index_publication_restart_replay": "passed",
            "queue_limits": queue["limits"],
            "queue_after_restart": "idle",
            "offline_evaluation": {
                "cases": 12,
                "passed": 12,
                "idempotent_admission": "passed",
                "report_after_restart": "unchanged",
                "selected_project": "unchanged",
                "measurement_kind": "scripted_mechanism_validation",
            },
        }
    finally:
        docker("rm", "--force", name, check=False)
        if created_volumes:
            docker("volume", "rm", *created_volumes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    print(json.dumps(verify(parser.parse_args().image), ensure_ascii=False, indent=2))
