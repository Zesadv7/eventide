"""Run dependency-free presentation and transport checks when Node is installed."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_web_projection_and_transport():
    node = shutil.which("node")
    if not node:
        pytest.skip("Optional Web checks require Node.js 22+")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 22:
        pytest.skip("Optional Web checks require Node.js 22+")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/web.test.mjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
