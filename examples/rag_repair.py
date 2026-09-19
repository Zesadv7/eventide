"""Offline engineering demonstration; scripted behavior is not model intelligence.

Run with: uv run python -m examples.rag_repair
All files, Git history and Runtime databases live in a temporary directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.index import IndexStore

_BROKEN = "def discount_price(price, percent):\n    return price * percent / 100\n"
_TESTS = """import unittest
from pricing import discount_price

class PricingTests(unittest.TestCase):
    def test_twenty_percent_discount(self):
        self.assertEqual(discount_price(100, 20), 80)

    def test_zero_discount(self):
        self.assertEqual(discount_price(45, 0), 45)

    def test_full_discount(self):
        self.assertEqual(discount_price(45, 100), 0)
"""


def _seed(workspace: Path) -> None:
    (workspace / "pricing.py").write_text(_BROKEN, encoding="utf-8")
    (workspace / "test_pricing.py").write_text(_TESTS, encoding="utf-8")
    (workspace / "pricing.md").write_text(
        "# Pricing contract\n"
        "discount_price returns the amount payable, not the saved amount.\n"
        "A 20 percent discount on 100 must return 80.\n",
        encoding="utf-8",
    )
    for arguments in (
        ["init"],
        ["config", "user.name", "eventide-demo"],
        ["config", "user.email", "demo@localhost"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(workspace), *arguments], check=True, capture_output=True)
    (workspace / ".git" / "info" / "exclude").write_text(".eventide/\n", encoding="utf-8")
    for arguments in (["add", "."], ["commit", "-m", "seed pricing regression"]):
        subprocess.run(["git", "-C", str(workspace), *arguments], check=True, capture_output=True)


def _run_checks(workspace: Path) -> subprocess.CompletedProcess[str]:
    # This independent verifier does not trust the assistant's final text, tool
    # error flag, or PATH's Python. It executes the unchanged seeded test suite.
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "test_pricing"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )


async def run_demo() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="eventide-rag-repair-") as directory:
        workspace = Path(directory)
        _seed(workspace)
        before = _run_checks(workspace)
        IndexStore(workspace).build()
        settings = Settings(
            workdir=workspace,
            state_dir=workspace / ".eventide",
            provider="scripted",
            api_key=None,
            base_url=None,
            model="scripted",
            fallback_model=None,
            max_steps=2,
        )
        discovery = ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "find",
                            "name": "search_knowledge",
                            "arguments": {
                                "query": "discount_price amount payable",
                                "k": 3,
                            },
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "id": "read",
                            "name": "read_file",
                            "arguments": {
                                "path": "pricing.py",
                            },
                        }
                    ]
                },
            ]
        )
        host = RuntimeHost(settings, discovery, enable_mcp=False)
        try:
            first = await host.run(RunRequest("Locate and fix the discount calculation."))
            first_events = host.store.run_events(first.run_id)
            parked = host.session_status(first.session_id)["status"] == "parked"
        finally:
            await host.close()

        command_args = [sys.executable, "-B", "-m", "unittest", "test_pricing"]
        command = (
            subprocess.list2cmdline(command_args) if os.name == "nt" else shlex.join(command_args)
        )
        repair = ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "fix",
                            "name": "edit_file",
                            "arguments": {
                                "path": "pricing.py",
                                "old_text": "price * percent / 100",
                                "new_text": "price * (100 - percent) / 100",
                            },
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "id": "escape",
                            "name": "read_file",
                            "arguments": {
                                "path": "../outside-secret.txt",
                            },
                        }
                    ]
                },
                {"tool_calls": [{"id": "test", "name": "bash", "arguments": {"command": command}}]},
                {"text": "The pricing regression is fixed and verified."},
            ]
        )
        # Reopen the Host: Continue must recover persisted state, not Python objects.
        reopened = RuntimeHost(replace(settings, max_steps=4), repair, enable_mcp=False)
        try:
            second = await reopened.continue_session(first.session_id)
            events = first_events + reopened.store.run_events(second.run_id)
        finally:
            await reopened.close()
        after = _run_checks(workspace)
        completed = {
            event["payload"]["call_id"]: event["payload"]
            for event in events
            if event["type"] == "tool.completed"
        }
        evidence = {
            "regression_failed_before": before.returncode != 0
            and "FAILED" in before.stderr
            and "Ran 3 tests" in before.stderr
            and "FAIL:" in before.stderr,
            "search_returned_citation": "pricing.md:" in completed["find"]["content"],
            "parked_at_step_limit": parked and first.status == "interrupted",
            "continued_after_host_reopen": second.status == "completed",
            "recovered_prior_tool_results": "pricing.md:"
            in json.dumps(repair.requests[0].messages),
            "workspace_escape_denied": completed["escape"]["is_error"]
            and "Permission denied" in completed["escape"]["content"],
            "agent_ran_tests": "Ran 3 tests" in completed["test"]["content"]
            and "OK" in completed["test"]["content"],
            "independent_tests_passed": after.returncode == 0 and "Ran 3 tests" in after.stderr,
            "test_suite_unchanged": (workspace / "test_pricing.py").read_text(encoding="utf-8")
            == _TESTS,
            "patch_present": "price * (100 - percent) / 100"
            in (workspace / "pricing.py").read_text(encoding="utf-8"),
        }
        return {
            "mode": "offline-scripted",
            "regression_smoke": True,
            "passed": all(evidence.values()),
            "before_exit_code": before.returncode,
            "after_exit_code": after.returncode,
            "evidence": evidence,
            "tool_sequence": [
                event["payload"]["name"] for event in events if event["type"] == "tool.prepared"
            ],
            "note": "Scripted lifecycle regression; not a real-model task success rate.",
        }


if __name__ == "__main__":
    report = asyncio.run(run_demo())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
