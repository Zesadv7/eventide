"""Fixed tests and an independent runner, rather than model text, decide repair success."""

import json
from pathlib import Path

import pytest

from eventide.evaluation import run_evaluations
from tests.test_runtime import settings_for

REPO = Path(__file__).resolve().parents[1]


async def test_engineering_suite_uses_independent_acceptance(isolated_workspace):
    report = await run_evaluations(
        REPO / "evals/engineering.yaml", settings=settings_for(isolated_workspace)
    )
    assert report["passed"] == report["total"] == 3
    for case in report["results"]:
        verification = case["verification"]
        assert verification["baseline_exit_code"] == 1
        assert verification["exit_code"] == 0
        assert verification["tests_unchanged"] is True
        assert verification["baseline_run_id"] != verification["verification_run_id"]
        assert verification["test_manifest"]


@pytest.mark.parametrize("tamper", [False, True])
async def test_model_claim_or_modified_tests_cannot_pass(isolated_workspace, tamper):
    case = {
        "id": "cannot-self-grade",
        "prompt": "fix code",
        "acceptance_tests": True,
        "workspace": {
            "app.py": "value = 1\n",
            "tests/test_app.py": "import unittest\nfrom app import value\n"
            "class Test(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(value, 2)\n",
        },
        "script": [{"text": "all tests passed"}],
        "expected": {"status": "completed"},
    }
    if tamper:
        case["script"] = [
            {
                "tool_calls": [
                    {
                        "id": "forge",
                        "name": "write_file",
                        "arguments": {
                            "path": "tests/test_app.py",
                            "content": "# erase failing tests\n",
                        },
                    }
                ]
            },
            {"text": "all tests passed"},
        ]
    suite = isolated_workspace / "suite.json"
    suite.write_text(json.dumps({"cases": [case]}))
    report = await run_evaluations(suite, settings=settings_for(isolated_workspace))
    assert report["passed"] == 0
    assert report["results"][0]["verification"]["passed"] is False
    if tamper:
        assert "acceptance tests changed" in report["results"][0]["reasons"]
    else:
        assert "independent acceptance failed" in report["results"][0]["reasons"]


async def test_fixture_path_cannot_escape_sandbox(isolated_workspace):
    suite = isolated_workspace / "bad.json"
    suite.write_text(json.dumps({"cases": [{"prompt": "bad", "workspace": {"../escape": "bad"}}]}))
    with pytest.raises(ValueError, match="sandbox"):
        await run_evaluations(suite, settings=settings_for(isolated_workspace))
