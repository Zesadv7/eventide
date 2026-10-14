"""Offline delivery demonstration; scripted behavior is not model intelligence.

Run with: uv run python -m examples.delivery_repair
Source, managed checkout and Runtime state are temporary. Checks use an explicitly
selected local Python, not an OS sandbox. The scripted writer only edits business
code; a separate verifier runs the unchanged tests before explicit patch adoption.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from eventide.config import Settings
from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider

_BROKEN = "def discount_price(price, percent):\n    return price * percent / 100\n"
_FIXED = "def discount_price(price, percent):\n    return price * (100 - percent) / 100\n"
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
_CHECK_ARGS = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"]


def _git(source: Path, *args: str) -> str:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=env,
    ).stdout.strip()


def _seed(source: Path) -> None:
    (source / "pricing.py").write_text(_BROKEN, encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_pricing.py").write_text(_TESTS, encoding="utf-8")
    _git(source, "init")
    hooks = source / ".git" / "demo-hooks"
    hooks.mkdir()
    for name, value in (
        ("user.name", "eventide-demo"),
        ("user.email", "demo@localhost"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        _git(source, "config", name, value)
    _git(source, "add", "pricing.py", "tests/test_pricing.py")
    _git(source, "commit", "-m", "seed pricing regression")


def _test_hash(source: Path) -> str:
    return hashlib.sha256((source / "tests" / "test_pricing.py").read_bytes()).hexdigest()


async def run_demo() -> dict[str, Any]:
    # The state root lives outside the source project; the managed checkout must
    # remain available until verification and adoption have both finished.
    with (
        tempfile.TemporaryDirectory(prefix="eventide-delivery-source-") as source_directory,
        tempfile.TemporaryDirectory(prefix="eventide-delivery-state-") as state_directory,
    ):
        source, state = Path(source_directory).resolve(), Path(state_directory).resolve()
        _seed(source)
        original_head, tests_sha256 = _git(source, "rev-parse", "HEAD"), _test_hash(source)
        check = subprocess.list2cmdline(_CHECK_ARGS) if os.name == "nt" else shlex.join(_CHECK_ARGS)
        settings = Settings(
            workdir=source,
            state_dir=state,
            provider="scripted",
            api_key=None,
            base_url=None,
            model="scripted",
            max_steps=4,
        )
        provider = ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "id": "fix_pricing",
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
                    "text": "Pricing patch is ready in the checkout; verification pending."
                },
            ]
        )
        host = RuntimeHost(settings, provider, enable_mcp=False)
        try:
            prepared = await host.manage_delivery("prepare", target=source, name="pricing-fix")
            delivery_id = prepared["delivery_id"]
            before = await host.manage_delivery("verify", delivery_id=delivery_id, checks=[check])
            worker = await host.manage_delivery(
                "run",
                delivery_id=delivery_id,
                goal="Fix discount_price. Edit only pricing.py; keep tests unchanged.",
            )
            worker_events = host.store.run_events(worker["worker_run_id"])
        finally:
            await host.close()

        # Restart discards Python objects. The Delivery receipt, worker history
        # and check results must be recovered from the one canonical Event Log.
        reopened = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
        try:
            recovered = await reopened.manage_delivery("show", delivery_id=delivery_id)
            verified = await reopened.manage_delivery(
                "verify",
                delivery_id=delivery_id,
                checks=[check],
            )
            checkout = Path(prepared["receipt"]["checkout"])
            patch = Path(verified["artifact"]).read_bytes()
            source_untouched = (
                (source / "pricing.py").read_text(encoding="utf-8") == _BROKEN
                and _test_hash(source) == tests_sha256
                and _git(source, "rev-parse", "HEAD") == original_head
                and not _git(source, "status", "--porcelain")
            )
            # This explicit control-plane call adopts the verified patch. The
            # model never approves its own patch and source stays unstaged.
            adopted = await reopened.manage_delivery("apply", delivery_id=delivery_id)
            after = await asyncio.to_thread(
                subprocess.run,
                _CHECK_ARGS,
                cwd=source,
                capture_output=True,
                text=True,
                timeout=30,
            )
            before_exit_code = before["checks"][0]["metadata"].get("exit_code")
            verified_exit_code = verified["checks"][0]["metadata"].get("exit_code")
            edit = next(
                (
                    event["payload"]
                    for event in worker_events
                    if event["type"] == "tool.completed"
                    and event["payload"]["call_id"] == "fix_pricing"
                ),
                None,
            )
            evidence = {
                "baseline_failed": before["status"] == "failed"
                and before_exit_code == 1
                and "Ran 3 tests" in before["checks"][0]["content"],
                "writer_changed_business_code": worker["status"] == "completed"
                and edit is not None
                and not edit["is_error"]
                and (checkout / "pricing.py").read_text(encoding="utf-8") == _FIXED,
                "delivery_recovered_after_restart": recovered["delivery_id"] == delivery_id
                and reopened.store.get_run(worker["worker_run_id"])["status"] == "completed",
                "independent_verification_passed": verified["status"] == "passed"
                and verified_exit_code == 0
                and "Ran 3 tests" in verified["checks"][0]["content"],
                "patch_hash_matches": bool(patch)
                and hashlib.sha256(patch).hexdigest()
                == verified["patch_sha256"]
                == adopted["patch_sha256"],
                "source_untouched_until_adoption": source_untouched,
                "independent_source_tests_passed": after.returncode == 0
                and "Ran 3 tests" in after.stderr
                and "OK" in after.stderr,
                "original_tests_unchanged": _test_hash(source)
                == _test_hash(checkout)
                == tests_sha256,
                "only_business_patch_adopted": _git(source, "diff", "--name-only") == "pricing.py"
                and (source / "pricing.py").read_text(encoding="utf-8") == _FIXED,
                "source_not_staged_or_committed": not _git(
                    source, "diff", "--cached", "--name-only"
                )
                and _git(source, "rev-parse", "HEAD") == original_head,
                "checkout_retained_through_completion": checkout.is_dir(),
                "checks_used_explicit_local_executor": before["executor"]
                == verified["executor"]
                == "local",
            }
            return {
                "mode": "offline-scripted",
                "regression_smoke": True,
                "passed": all(evidence.values()),
                "before_exit_code": before_exit_code,
                "after_exit_code": after.returncode,
                "patch_sha256": verified["patch_sha256"],
                "worker_run_id": worker["worker_run_id"],
                "check_run_id": verified["control_run_id"],
                "evidence": evidence,
                "note": "Scripted delivery regression, not real-model capability or an OS sandbox; "
                "all temporary source, checkouts and state are removed on exit.",
            }
        finally:
            await reopened.close()


if __name__ == "__main__":
    report = asyncio.run(run_demo())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
