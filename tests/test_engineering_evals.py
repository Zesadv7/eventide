"""Engineering fixtures fail for real bugs and pass without changing fixed tests."""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from eventide.executor import ToolContext, ToolExecutor
from eventide.models import ToolCall
from eventide.policy import PolicyEngine
from eventide.tools.runtime_catalog import HANDLERS

SUITE = Path(__file__).resolve().parents[1] / "evals" / "engineering.yaml"
CASES = yaml.safe_load(SUITE.read_text(encoding="utf-8"))["cases"]


def _fixed_tests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "tests").rglob("*"))
        if path.is_file()
    }


def _unittest(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
async def test_engineering_fixture_baseline_and_scripted_repair(case, isolated_workspace):
    assert case["acceptance_tests"] is True
    assert case["expected"] == {"status": "completed"}
    fixtures = case["workspace"]
    for relative, content in fixtures.items():
        destination = isolated_workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    original_tests = _fixed_tests(isolated_workspace)
    originals = {
        name: hashlib.sha256(content.encode()).hexdigest() for name, content in fixtures.items()
    }

    baseline = _unittest(isolated_workspace)

    assert baseline.returncode == 1, baseline.stderr
    assert "FAIL:" in baseline.stderr and "ERROR:" not in baseline.stderr
    count = re.search(r"Ran (\d+) tests", baseline.stderr)
    assert count is not None and int(count.group(1)) >= 5
    executor = ToolExecutor(HANDLERS)
    modified: set[str] = set()
    for response in case["script"]:
        for call in response.get("tool_calls", []):
            assert call["name"] in {"edit_file", "write_file"}
            path = call["arguments"]["path"]
            assert path in {"pricing.py", "ttl_cache.py", "config_merge.py"}
            modified.add(path)
            result = await executor.execute(
                ToolCall(call["id"], call["name"], call["arguments"]),
                ToolContext(isolated_workspace, "fixture_repair", PolicyEngine()),
            )
            assert not result.is_error, result.content
    assert len(modified) == 1
    assert _fixed_tests(isolated_workspace) == original_tests
    for name, digest in originals.items():
        if name not in modified:
            assert hashlib.sha256((isolated_workspace / name).read_bytes()).hexdigest() == digest

    acceptance = _unittest(isolated_workspace)

    assert acceptance.returncode == 0, acceptance.stderr
    assert "OK" in acceptance.stderr
    assert _fixed_tests(isolated_workspace) == original_tests
