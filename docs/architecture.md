# Nexus Agent Architecture

## Core Philosophy

Nexus Agent is a **harness**, not the intelligence itself. The model decides what to do; the harness provides the environment in which it can act.

```text
Agent = Model + Harness
        |        |
        |        +-- tools, memory, permissions, tasks, teammates, scheduling
        +----------- reasoning, planning, natural-language understanding
```

## The Loop

Every turn follows the same pattern:

```text
user input
  → cron/background notifications injected
  → context compaction
  → system prompt assembled from skills + memory + MCP state
  → LLM call
  → if tool_use blocks exist:
       PreToolUse hooks + permission check
       dispatch to handler (builtin, MCP, or background)
       PostToolUse hooks
       tool_result appended
       loop again
     else:
       Stop hooks
       return text
```

This loop lives in `nexus_agent/agent.py`.

## Module Map

| Module | Responsibility |
|--------|---------------|
| `config.py` | `.env` loading, Anthropic client, paths, constants. |
| `tools/` | Tool schemas and handlers. `dispatch.py` avoids circular imports. |
| `tasks/` | Durable task graph and git worktree isolation. |
| `teams/` | MessageBus, protocol state, subagents, persistent teammates. |
| `memory/` | Skill catalog + `MEMORY.md` long-term memory. |
| `scheduling/` | Cron scheduler and background task dispatch. |
| `mcp/` | Mock MCP servers and runtime tool-pool merging. |
| `llm.py` | Retry, model fallback, prompt-too-long detection. |
| `context.py` | Four-layer compaction: budget → snip → micro → summary. |
| `hooks.py` | Pre/post tool-use, user-prompt-submit, and stop hooks. |
| `agent.py` | Main loop wiring everything together. |
| `cli.py` | Interactive prompt and cron auto-run thread. |

## Context Compaction Pipeline

Before each LLM call, the harness tries the cheapest strategies first:

1. **`tool_result_budget`** — persist oversized individual tool outputs to disk.
2. **`snip_compact`** — drop a middle slice of old messages.
3. **`micro_compact`** — replace old tool results with a short placeholder.
4. **`compact_history`** — ask the model to summarize the whole conversation.

If the model still complains the prompt is too long, `reactive_compact` trims the oldest messages and keeps only the most recent tail.

## Permission Model

Permissions are implemented as `PreToolUse` hooks, not hardcoded inside tools:

- A deny list blocks commands like `rm -rf /`, `sudo`, `mkfs`.
- Destructive patterns (`rm `, `> /etc/`, `chmod 777`) trigger interactive approval.
- File tools that escape the workspace prompt the user.
- MCP tools containing `deploy` also prompt for approval.

## Multi-Agent Coordination

- **`task`** — one-shot subagent with isolated `messages[]`; only the final summary returns.
- **`spawn_teammate`** — persistent daemon thread with its own loop and mailbox.
- **`MessageBus`** — append-only JSONL mailboxes on disk.
- **Protocol state** — plan approval and shutdown requests carry `request_id` to prevent mismatched replies.
- **Worktrees** — tasks can be bound to git worktrees; teammate file tools automatically run in the bound directory.

## Error Recovery

`llm.with_retry` handles transient failures:

- `429` / rate limit → exponential backoff.
- `529` / overloaded → backoff; after repeated failures, switch to `FALLBACK_MODEL_ID`.
- `max_tokens` → first escalate `max_tokens`, then request a continuation.
- prompt too long → reactive compact and retry once.

## Adding a New Tool

1. Implement the handler in the appropriate `tools/` module.
2. Add the schema to `tools/registry.py` `BUILTIN_TOOLS`.
3. Add the handler to `tools/registry.py` `BUILTIN_HANDLERS`.
4. Write a test in `tests/test_*.py`.

No changes to the main loop are required.
