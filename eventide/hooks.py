"""Hook registry and built-in permission/audit hooks."""

from eventide.config import WORKDIR
from eventide.policy import PolicyDecision, PolicyEngine

# Hook registry. Events: UserPromptSubmit, PreToolUse, PostToolUse, Stop.
HOOKS: dict[str, list] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: str, callback) -> None:
    """Register a callback for a hook event."""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """Run callbacks until one returns a non-None value, then return it.

    This lets PreToolUse hooks block execution by returning a denial string.
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def clear_hooks(event: str | None = None) -> None:
    """Clear all hooks or hooks for a specific event. Useful in tests."""
    if event is None:
        for key in HOOKS:
            HOOKS[key].clear()
    else:
        HOOKS[event].clear()


# ── Built-in hooks ──

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]
_POLICY = PolicyEngine()


def permission_hook(block) -> str | None:
    """Block or require approval for dangerous tool calls."""
    result = _POLICY.evaluate(block.name, dict(block.input), WORKDIR)
    if result.decision == PolicyDecision.DENY:
        return f"Permission denied: {result.reason}"
    if result.decision == PolicyDecision.ASK:
        print("\n\033[33m[permission] approval required\033[0m")
        print(f"  {block.name}: {block.input}")
        choice = input("  Allow? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            return "Permission denied by user"
        return None

    # Compatibility checks below retain the original teaching behavior.
    if block.name == "bash":
        command = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied: '{pattern}' is on the deny list"
        if any(token in command for token in DESTRUCTIVE):
            print("\n\033[33m[permission] destructive command\033[0m")
            print(f"  {command}")
            choice = input("  Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    if block.name in ("read_file", "write_file", "edit_file"):
        path = block.input.get("path", "")
        try:
            resolved = (WORKDIR / path).resolve()
            if not resolved.is_relative_to(WORKDIR):
                print("\n\033[33m[permission] Access outside workspace\033[0m")
                print(f"  {block.name}: {path}")
                choice = input("  Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
        except Exception:
            return f"Permission denied: invalid path '{path}'"

    return None


def log_hook(block) -> None:
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def large_output_hook(block, output) -> None:
    text = str(output)
    if len(text) > 100000:
        print(f"\033[33m[HOOK] large output from {block.name}: {len(text)} chars\033[0m")
    return None


def user_prompt_hook(query: str) -> None:
    print(f"\033[90m[HOOK] UserPromptSubmit: {WORKDIR}\033[0m")
    return None


def stop_hook(messages: list) -> None:
    tool_count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            tool_count += sum(
                1
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_result"
            )
    print(f"\033[90m[HOOK] Stop: {tool_count} tool result(s)\033[0m")
    return None


# Register default hooks.
register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", stop_hook)
