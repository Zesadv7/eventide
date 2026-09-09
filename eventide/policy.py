"""Central policy decisions shared by every runtime surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


_DENIED_COMMANDS = (
    re.compile(r"(^|[;&|]\s*)\s*(sudo|shutdown|reboot|mkfs)(\s|$)", re.I),
    re.compile(r"rm\s+-[^\n]*r[^\n]*f\s+[/~](\s|$)", re.I),
    re.compile(r"\bdd\s+if=", re.I),
)
_REVIEW_COMMANDS = (
    re.compile(r"(^|[;&|]\s*)\s*(rm|rmdir|del|erase|format)(\s|$)", re.I),
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-|branch\s+-D)\b", re.I),
    re.compile(r"\b(chmod\s+777|drop\s+(table|database))\b", re.I),
)
_SAFE_FILE_TOOLS = {"read_file", "glob"}
_WRITE_FILE_TOOLS = {"write_file", "edit_file"}


def resolve_scoped_path(base: Path, raw_path: str) -> Path:
    """Resolve a path and enforce that it remains inside base."""
    root = base.resolve()
    candidate = (root / raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise PermissionError(f"Path escapes workspace: {raw_path}")
    return candidate


def validate_agent_name(name: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name or "") or name in {".", ".."}:
        return "Agent name must use 1-64 letters, digits, dots, underscores, or dashes"
    return None


class PolicyEngine:
    """Classify tool calls. ASK requires a surface-specific approval handler."""

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        cwd: Path,
        *,
        trusted_readonly: bool = False,
    ) -> PolicyResult:
        if tool_name == "bash":
            command = str(arguments.get("command", ""))
            if any(pattern.search(command) for pattern in _DENIED_COMMANDS):
                return PolicyResult(
                    PolicyDecision.DENY, "Command matches the non-overridable deny policy"
                )
            if any(pattern.search(command) for pattern in _REVIEW_COMMANDS):
                return PolicyResult(
                    PolicyDecision.ASK, "Command may delete or irreversibly alter data"
                )
            return PolicyResult(PolicyDecision.ALLOW, "Command passed local policy")
        if tool_name in _SAFE_FILE_TOOLS | _WRITE_FILE_TOOLS:
            raw_path = str(arguments.get("path", arguments.get("pattern", "")))
            if tool_name != "glob":
                try:
                    resolve_scoped_path(cwd, raw_path)
                except PermissionError as exc:
                    return PolicyResult(PolicyDecision.DENY, str(exc))
            return PolicyResult(PolicyDecision.ALLOW, "Path is scoped to the workspace")
        if tool_name.startswith("mcp__"):
            if trusted_readonly:
                return PolicyResult(PolicyDecision.ALLOW, "MCP server declares this tool read-only")
            return PolicyResult(
                PolicyDecision.ASK, "External MCP tools require explicit approval unless read-only"
            )
        return PolicyResult(PolicyDecision.ALLOW, "No elevated risk detected")
