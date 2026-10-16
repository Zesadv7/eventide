"""CI/developer check of the Python evaluation interface; always offline."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from eventide.evaluation import run_evaluations


async def check_suites(paths: list[Path]) -> bool:
    passed = True
    for path in paths:
        report = await run_evaluations(path, live=False)
        print(
            json.dumps(
                {
                    "suite": report["suite"],
                    "passed": report["passed"],
                    "total": report["total"],
                    "measurement_kind": "scripted_mechanism_validation",
                    "report_path": report["report_path"],
                }
            )
        )
        passed = report["passed"] == report["total"] and passed
    return passed


if __name__ == "__main__":
    suites = [Path(value) for value in sys.argv[1:]] or [Path("evals/smoke.yaml")]
    raise SystemExit(0 if asyncio.run(check_suites(suites)) else 1)
