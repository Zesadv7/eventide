"""The shipped workbench must be complete, current, and served without Node."""

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import eventide.api as api_module
from eventide.api import WORKBENCH_DIR, create_app
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


@pytest.mark.parametrize("stale", ["eventide/cli.py", "eventide/web/app.js",
                                  "eventide/workbench/assets/obsolete.js"])
def test_wheel_gate_rejects_stale_build_outputs(tmp_path, stale):
    wheel = tmp_path / "stale.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(stale, "stale")
        archive.writestr("eventide-0.4.0.dist-info/entry_points.txt",
                         "[console_scripts]\neventide-server = eventide.server:main\n")
        archive.writestr("eventide/workbench/manifest.json", json.dumps({"files": {}}))
    checker = Path(__file__).resolve().parents[1] / "frontend/scripts/check_wheel.py"
    result = subprocess.run([sys.executable, str(checker), str(wheel)], capture_output=True)
    assert result.returncode != 0
    assert b"AssertionError" in result.stderr


def test_packaged_resources_match_sources_and_content_manifest():
    manifest = json.loads((WORKBENCH_DIR / "manifest.json").read_text())
    assert manifest["version"] == 1
    actual = {
        p.relative_to(WORKBENCH_DIR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in WORKBENCH_DIR.rglob("*") if p.is_file() and p.name != "manifest.json"
    }
    assert actual == manifest["files"]
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    files = [p for p in (frontend / "src").rglob("*") if p.is_file()]
    files += [frontend / p for p in (
        "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json",
        "scripts/package_web.mjs",
    )]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(frontend).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    assert digest.hexdigest() == manifest["source_sha256"], "Run frontend npm run build"


def test_fastapi_serves_workbench_and_all_referenced_assets(isolated_workspace):
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.get("/workbench/")
        assert response.status_code == 200
        assert '<div id="app">' in response.text
        resources = re.findall(r'(?:src|href)="(\./assets/[^"\s]+)"', response.text)
        assert len(resources) >= 2
        for resource in resources:
            asset = client.get("/workbench/" + resource.removeprefix("./"))
            assert asset.status_code == 200
            assert "text/html" not in asset.headers["content-type"]
        assert client.get("/workbench/assets/missing.js").status_code == 404
        assert client.get("/workbench/%2e%2e/host.py").status_code == 404
        assert client.get("/healthz").status_code == 200
        redirect = client.get("/", follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/workbench/"
        assert client.get("/assets/app.js").status_code == 404


def test_missing_bundle_is_explicit_and_does_not_break_api(isolated_workspace, monkeypatch):
    monkeypatch.setattr(api_module, "WORKBENCH_DIR", isolated_workspace / "missing")
    host = RuntimeHost(settings_for(isolated_workspace), ScriptedProvider([]))
    with TestClient(create_app(host)) as client:
        response = client.get("/workbench/")
        assert response.status_code == 503
        assert "assets missing" in response.json()["message"]
        assert client.get("/healthz").status_code == 200
