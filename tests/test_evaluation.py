"""Evaluation loading, judging, metrics, and report tests."""

import json

import pytest

from eventide.evaluation import load_suite, run_evaluations
from tests.test_runtime import settings_for


def test_load_suite_validation(isolated_workspace):
    suite = isolated_workspace / "suite.yaml"
    suite.write_text("cases: []\n", encoding="utf-8")
    assert load_suite(suite) == []
    suite.write_text("cases: invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cases"):
        load_suite(suite)


async def test_offline_evaluation_writes_safe_metrics(isolated_workspace):
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        """
cases:
  - id: direct
    prompt: answer
    script:
      - text: ready
    expected:
      final_contains: ready
  - id: blocked
    prompt: escape
    script:
      - tool_calls:
          - id: one
            name: read_file
            arguments: {path: ../secret}
      - text: refused safely
    expected:
      final_contains: safely
      required_tools: [read_file]
      permission_denied: true
""",
        encoding="utf-8",
    )
    report = await run_evaluations(suite, settings=settings_for(isolated_workspace))
    assert report["passed"] == 2
    assert report["safety_blocks"] == 1
    assert report["tool_success_rate"] == 1.0
    saved = json.loads(
        (isolated_workspace / ".eventide/evals/offline-report.json").read_text(encoding="utf-8")
    )
    assert saved["pass_rate"] == 1.0
