"""Tests for the central policy boundary."""

from eventide.policy import (
    PolicyDecision,
    PolicyEngine,
    resolve_scoped_path,
    validate_agent_name,
)


def test_policy_classifies_commands(isolated_workspace):
    policy = PolicyEngine()
    assert (
        policy.evaluate("bash", {"command": "echo ok"}, isolated_workspace).decision
        == PolicyDecision.ALLOW
    )
    assert (
        policy.evaluate("bash", {"command": "rm file.txt"}, isolated_workspace).decision
        == PolicyDecision.ASK
    )
    assert (
        policy.evaluate("bash", {"command": "rm -rf /"}, isolated_workspace).decision
        == PolicyDecision.DENY
    )


def test_path_scope_and_agent_names(isolated_workspace):
    assert resolve_scoped_path(isolated_workspace, "safe/file.txt").is_relative_to(
        isolated_workspace
    )
    assert validate_agent_name("reviewer-1") is None
    assert validate_agent_name("../escape") is not None


def test_settings_custom_state_dir(isolated_workspace, monkeypatch):
    from eventide.config import Settings

    monkeypatch.setenv("EVENTIDE_STATE_DIR", "runtime-state")
    settings = Settings.from_env(isolated_workspace)
    assert settings.state_dir == (isolated_workspace / "runtime-state").resolve()
