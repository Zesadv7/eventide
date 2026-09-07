"""Shared pytest fixtures and monkeypatches."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    """Give every test a clean set of environment variables."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_ID", "claude-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")


@pytest.fixture
def isolated_workspace():
    root = Path.cwd() / ".task_outputs" / "pytest"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield Path(directory)
