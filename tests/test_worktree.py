"""Tests for git worktree helpers."""

import subprocess

import pytest

from nexus_agent.tasks import worktree as worktree_module
from nexus_agent.tasks.worktree import (
    create_worktree,
    remove_worktree,
    run_git,
    validate_worktree_name,
)


def test_validate_worktree_name():
    assert validate_worktree_name("feature-x") is None
    assert validate_worktree_name("") is not None
    assert validate_worktree_name("..") is not None


def test_create_and_remove_worktree(isolated_workspace, monkeypatch):
    repo = isolated_workspace / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("test", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    worktrees = repo / ".worktrees"
    worktrees.mkdir()
    monkeypatch.setattr(worktree_module, "WORKDIR", repo)
    monkeypatch.setattr(worktree_module, "WORKTREES_DIR", worktrees)
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("Project root is not a git repo")

    name = "test_wt"
    msg = create_worktree(name)
    assert "created" in msg or "already exists" in msg
    if "created" in msg:
        worktree_path = worktrees / name
        (worktree_path / "change.txt").write_text("committed", encoding="utf-8")
        subprocess.run(["git", "add", "change.txt"], cwd=worktree_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "worktree change"],
            cwd=worktree_path,
            check=True,
            capture_output=True,
        )
        refusal = remove_worktree(name)
        assert "1 commit(s)" in refusal
        remove_worktree(name, discard_changes=True)


def test_run_git_survives_non_ascii_output(isolated_workspace, monkeypatch):
    repo = isolated_workspace / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / "中文.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(worktree_module, "WORKDIR", repo)
    ok, output = run_git(["-c", "core.quotepath=false", "status", "--porcelain"])
    assert ok is True
    assert "中文.txt" in output


def test_run_git_survives_missing_streams(monkeypatch):
    class Result:
        stdout = None
        stderr = None
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    assert run_git(["status"]) == (True, "(no output)")
