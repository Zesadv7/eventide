# Codex Instructions: Nexus Agent

## Project Overview

Nexus Agent is a modular Python implementation of an **Agent Harness** — the infrastructure that gives a language model tools, memory, permissions, tasks, teamwork, scheduling, and external capabilities.

It was extracted and refactored from `learn-claude-code/s20_comprehensive/code.py` (MIT license, shareAI Lab).

- **Language**: Python 3.10+
- **Package**: `nexus_agent`
- **Entry point**: `python -m nexus_agent`
- **Tests**: `python -m pytest -q` (37 tests, all passing)
- **Repo**: `e:\projects\nexus-agent`

## Core Philosophy

> **The model decides. The harness executes.**

Do not add workflow orchestration, hardcoded routing, or prompt chains.
The harness provides the environment; the LLM chooses when to call tools.

## Architecture

```text
user input
  → cron/background notifications
  → context compaction
  → system prompt (skills + memory + MCP state)
  → LLM call
  → if tool_use blocks:
       PreToolUse hooks + permission check
       dispatch to handler (builtin / MCP / background)
       PostToolUse hooks
       tool_result appended
       loop again
    else:
       Stop hooks → return text
```

## Module Map

| Module | Responsibility |
|---|---|
| `nexus_agent/config.py` | `.env` loading, Anthropic client, paths, constants. |
| `nexus_agent/agent.py` | Main loop. |
| `nexus_agent/cli.py` | Interactive CLI + cron auto-run thread. |
| `nexus_agent/tools/` | Tool schemas and handlers. `dispatch.py` avoids circular imports. |
| `nexus_agent/tasks/` | Task graph and git worktree isolation. |
| `nexus_agent/teams/` | MessageBus, protocol, subagents, teammates. |
| `nexus_agent/memory/` | Skills catalog + MEMORY.md. |
| `nexus_agent/scheduling/` | Cron scheduler + background task dispatch. |
| `nexus_agent/mcp/` | Mock MCP servers + runtime tool-pool merging. |
| `nexus_agent/llm.py` | Retry, model fallback, prompt-too-long detection. |
| `nexus_agent/context.py` | Four-layer context compaction. |
| `nexus_agent/hooks.py` | Hook registry + permission layer. |
| `nexus_agent/utils.py` | Shared helpers (`extract_text`, `has_tool_use`). |

## Coding Rules

1. **Layer imports bottom-up.** `config.py` imports nothing internal. `agent.py`/`cli.py` import everything.
2. **Avoid circular imports.** If two modules need a helper, put it in `nexus_agent/utils.py` or `nexus_agent/tools/dispatch.py`.
3. **Tool handlers receive `cwd` explicitly.** Do not rely on global `WORKDIR`.
4. **Hook registry is global but test-clearable.** Use `hooks.clear_hooks()` in tests.
5. **Preserve MIT license and shareAI Lab attribution.**
6. **Every change needs a test.** Run `python -m pytest -q` before committing.

## Common Tasks

### Add a tool

1. Implement handler in `nexus_agent/tools/<module>.py`.
2. Add schema to `BUILTIN_TOOLS` in `nexus_agent/tools/registry.py`.
3. Add handler to `BUILTIN_HANDLERS` in `nexus_agent/tools/registry.py`.
4. Add a test in `tests/`.

### Change the system prompt

Edit `assemble_system_prompt()` in `nexus_agent/agent.py`.

### Add a skill

Create `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`).
Restart the agent to pick it up.

### Add a model provider

Refactor `nexus_agent/llm.py` so `client.messages.create` goes through a provider-specific adapter.

## Verification Checklist

Before finishing any task:

- [ ] `python -m pytest -q` passes.
- [ ] `echo "q" | python -m nexus_agent` starts and exits cleanly.
- [ ] No `.env` or runtime state directories are staged.
- [ ] New code matches existing style.

## Current Status

- Core harness: complete.
- Tests: 37 passing.
- CLI: runnable.
- Open items: see `docs/plan.md`.
