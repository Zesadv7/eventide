"""Tests for hook registry and permission hook."""

from types import SimpleNamespace

from eventide import hooks


def test_register_and_trigger():
    hooks.clear_hooks()
    called = []
    hooks.register_hook("PreToolUse", lambda block: called.append("a") or None)
    hooks.register_hook("PreToolUse", lambda block: "blocked")
    hooks.register_hook("PreToolUse", lambda block: called.append("c") or None)
    result = hooks.trigger_hooks("PreToolUse", SimpleNamespace(name="x", input={}))
    assert result == "blocked"
    assert called == ["a"]  # Third hook should not run.


def test_permission_deny_list(monkeypatch):
    hooks.clear_hooks("PreToolUse")
    hooks.register_hook("PreToolUse", hooks.permission_hook)
    block = SimpleNamespace(name="bash", input={"command": "rm -rf /"})
    assert "Permission denied" in hooks.trigger_hooks("PreToolUse", block)


def test_permission_workspace_escape(monkeypatch):
    import tempfile

    hooks.clear_hooks("PreToolUse")
    hooks.register_hook("PreToolUse", hooks.permission_hook)
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr("eventide.hooks.WORKDIR", __import__("pathlib").Path(tmp))
    monkeypatch.setattr("builtins.input", lambda _: "n")
    block = SimpleNamespace(name="write_file", input={"path": "../escape.txt"})
    assert "Path escapes workspace" in hooks.trigger_hooks("PreToolUse", block)


def test_permission_allows_safe_bash():
    hooks.clear_hooks("PreToolUse")
    hooks.register_hook("PreToolUse", hooks.permission_hook)
    block = SimpleNamespace(name="bash", input={"command": "echo hello"})
    assert hooks.trigger_hooks("PreToolUse", block) is None
