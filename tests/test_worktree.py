"""Tests for git worktree helpers."""

import subprocess

import pytest

from nexus_agent.config import WORKDIR
from nexus_agent.tasks.worktree import (
    validate_worktree_name,
    create_worktree,
    remove_worktree,
)


def test_validate_worktree_name():
    assert validate_worktree_name("feature-x") is None
    assert validate_worktree_name("") is not None
    assert validate_worktree_name("..") is not None


def test_create_and_remove_worktree():
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=WORKDIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("Project root is not a git repo")

    name = "test_wt"
    msg = create_worktree(name)
    assert "created" in msg or "already exists" in msg
    if "created" in msg:
        remove_worktree(name, discard_changes=True)
