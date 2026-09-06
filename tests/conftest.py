"""Shared pytest fixtures and monkeypatches."""

import os
import pytest


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch, tmp_path):
    """Give every test a clean working directory and fake API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_ID", "claude-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    monkeypatch.chdir(tmp_path)
