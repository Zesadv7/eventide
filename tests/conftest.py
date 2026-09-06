"""Shared pytest fixtures and monkeypatches."""

import pytest


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    """Give every test a clean set of environment variables."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_ID", "claude-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
