"""Evaluation loading, judging, metrics, and report tests."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import eventide.evaluation as evaluation
from eventide.evaluation import load_suite, run_evaluations
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _judge(expected, output="ok", events=None, status="completed", workdir=None):
    return evaluation._judge({"expected": expected}, output, events or [], status, workdir)


def test_judge_file_evidence(isolated_workspace):
    (isolated_workspace / "keep.txt").write_text("Hello World", encoding="utf-8")

    passed, reasons = _judge(
        {
            "file_exists": ["keep.txt"],
            "file_absent": ["ghost.txt"],
            "file_contains": {"keep.txt": "hello world"},
        },
        workdir=isolated_workspace,
    )
    assert passed and reasons == ()

    passed, reasons = _judge(
        {
            "file_exists": ["missing.txt"],
            "file_absent": ["keep.txt"],
            "file_contains": {"keep.txt": "nope", "ghost.txt": "x"},
        },
        workdir=isolated_workspace,
    )
    assert not passed
    assert reasons == (
        "missing file: missing.txt",
        "unexpected file: keep.txt",
        "keep.txt missing 'nope'",
        "unreadable file: ghost.txt",
    )


def test_judge_file_checks_require_sandbox():
    passed, reasons = _judge({"file_exists": ["a.txt"]})
    assert not passed
    assert reasons == ("no sandbox workspace for file checks",)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, check=True, timeout=30
    )


def _seed_repo(path: Path, *, committed: bool, dirty: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "eval")
    _git(path, "config", "user.email", "eval@localhost")
    (path / "seed.txt").write_text("seed", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "seed", "--allow-empty")
    if committed:
        (path / "work.txt").write_text("work", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "work")
    if dirty:
        (path / "dirty.txt").write_text("dirty", encoding="utf-8")
    return path


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for sandbox grading")
def test_judge_git_committed(isolated_workspace):
    passed, reasons = _judge(
        {"git_committed": True},
        workdir=_seed_repo(isolated_workspace / "committed", committed=True),
    )
    assert passed and reasons == ()

    passed, reasons = _judge(
        {"git_committed": True},
        workdir=_seed_repo(isolated_workspace / "only-seed", committed=False, dirty=True),
    )
    assert not passed
    assert reasons == ("work was not committed",)

    passed, reasons = _judge(
        {"git_committed": True},
        workdir=_seed_repo(isolated_workspace / "dirty", committed=True, dirty=True),
    )
    assert not passed
    assert reasons == ("uncommitted changes remain in the sandbox",)

    plain = isolated_workspace / "plain"
    plain.mkdir()
    passed, reasons = _judge({"git_committed": True}, workdir=plain)
    assert not passed
    assert reasons == ("git evidence unavailable",)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for sandbox grading")
def test_create_sandbox_seeds_fixtures_and_git():
    sandbox = evaluation._create_sandbox({"a/seed.txt": "content", "b.txt": "x"})
    assert (sandbox / "a" / "seed.txt").read_text(encoding="utf-8") == "content"
    count = subprocess.run(
        ["git", "-C", str(sandbox), "rev-list", "--count", "HEAD"], capture_output=True
    )
    assert count.stdout.strip() == b"1"
    status = subprocess.run(
        ["git", "-C", str(sandbox), "status", "--porcelain"], capture_output=True
    )
    assert status.stdout.strip() == b""
    shutil.rmtree(sandbox, ignore_errors=True)


async def test_workspace_case_runs_in_isolated_sandbox(monkeypatch, isolated_workspace):
    captured = []
    real_runtime = evaluation.AgentRuntime

    def factory(**kwargs):
        captured.append(kwargs["settings"])
        return real_runtime(**kwargs)

    monkeypatch.setattr(evaluation, "AgentRuntime", factory)
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        """
cases:
  - id: sandboxed
    prompt: write inside the sandbox
    workspace:
      seed.txt: hello
    script:
      - tool_calls:
          - id: w1
            name: write_file
            arguments: {path: out/created.txt, content: made}
      - text: wrote it
    expected:
      final_contains: wrote
      required_tools: [write_file]
      file_exists: [out/created.txt]
      file_contains: {out/created.txt: made}
""",
        encoding="utf-8",
    )
    report = await run_evaluations(suite, settings=settings_for(isolated_workspace))
    assert report["passed"] == 1
    sandboxed = captured[0]
    assert sandboxed.workdir != isolated_workspace
    assert (sandboxed.workdir / "seed.txt").read_text(encoding="utf-8") == "hello"
    assert sandboxed.state_dir == sandboxed.workdir / ".eventide"
    assert (sandboxed.workdir / ".eventide" / "evals" / "offline.db").is_file()
    assert (isolated_workspace / ".eventide" / "evals" / "offline-report.json").is_file()


async def test_denied_write_leaves_no_file(isolated_workspace):
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        """
cases:
  - id: denied-write
    prompt: try to escape
    workspace:
      anchor.md: anchor
    script:
      - tool_calls:
          - id: x1
            name: write_file
            arguments: {path: ../outside.txt, content: no}
      - text: denied
    expected:
      final_contains: denied
      permission_denied: true
      file_absent: ["../outside.txt"]
""",
        encoding="utf-8",
    )
    report = await run_evaluations(suite, settings=settings_for(isolated_workspace))
    assert report["passed"] == 1
    assert report["safety_blocks"] == 1


async def test_steps_usage_status_pass_through(isolated_workspace):
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        """
cases:
  - id: passthrough
    prompt: do two steps
    script:
      - tool_calls:
          - id: g1
            name: glob
            arguments: {pattern: "*.md"}
      - text: done
        usage: {input_tokens: 7, output_tokens: 3}
    expected:
      final_contains: done
""",
        encoding="utf-8",
    )
    report = await run_evaluations(suite, settings=settings_for(isolated_workspace))
    entry = report["results"][0]
    assert entry["status"] == "completed"
    assert entry["steps"] == 2
    assert entry["usage"] == {"input_tokens": 7, "output_tokens": 3}


async def test_report_metrics_aggregation(isolated_workspace):
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        """
cases:
  - id: good
    prompt: answer
    script:
      - text: fine
    expected:
      final_contains: fine
  - id: bad
    prompt: answer
    script:
      - text: something else
    expected:
      final_contains: impossible
""",
        encoding="utf-8",
    )
    report = await run_evaluations(suite, settings=settings_for(isolated_workspace))
    assert report["passed"] == 1 and report["total"] == 2
    assert report["task_completion_rate"] == 0.5
    assert report["failure_reasons"] == {"final output missing 'impossible'": 1}
    assert report["average_steps"] > 0
    assert report["regression_smoke"] is True


async def test_live_report_includes_token_totals(monkeypatch, isolated_workspace):
    monkeypatch.setattr(
        evaluation,
        "build_provider",
        lambda settings: ScriptedProvider(
            [{"text": "live done", "usage": {"input_tokens": 11, "output_tokens": 5}}]
        ),
    )
    suite = isolated_workspace / "suite.yaml"
    suite.write_text(
        """
cases:
  - id: live-case
    prompt: answer
    script:
      - text: live done
    expected:
      final_contains: live done
""",
        encoding="utf-8",
    )
    report = await run_evaluations(suite, live=True, settings=settings_for(isolated_workspace))
    assert report["mode"] == "live"
    assert report["total_input_tokens"] == 11
    assert report["total_output_tokens"] == 5
    assert "regression_smoke" not in report


def test_tasks_suite_invariants():
    cases = load_suite(REPO_ROOT / "evals" / "tasks.yaml")
    assert 10 <= len(cases) <= 20
    ids = [str(case["id"]) for case in cases]
    assert len(ids) == len(set(ids))
    for case in cases:
        assert case.get("prompt")
        assert case.get("script")
        assert "expected" in case


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="sandbox fixtures need git; bash cases need a shell with git and python",
)
async def test_tasks_suite_passes_offline(isolated_workspace):
    report = await run_evaluations(
        REPO_ROOT / "evals" / "tasks.yaml", settings=settings_for(isolated_workspace)
    )
    assert report["passed"] == report["total"], report["failure_reasons"]
    assert report["regression_smoke"] is True
