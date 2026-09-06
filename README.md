# Nexus Agent

A minimal but runnable **Agent Harness** extracted and modularized from [learn-claude-code](https://github.com/Zesadv7/learn-claude-code) by shareAI Lab.

> **Agent = Model + Harness.**  
> This project is the harness: tools, permissions, task graph, worktree isolation, subagents, teammate coordination, cron scheduling, and MCP integration — all wired into one agent loop.

## Quick Start

```bash
cd e:\projects\nexus-agent
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY and MODEL_ID
python -m nexus_agent
```

## What It Includes

- **Agent Loop**: `while True` + LLM + tool_use dispatch.
- **Tool System**: bash, file read/write/edit/glob, todo_write, task dispatch, skill loading.
- **Permissions**: `PreToolUse` hooks with deny/destructive lists and workspace escape checks.
- **Task Graph**: durable tasks with dependencies, claim/complete lifecycle.
- **Worktree Isolation**: git worktree per task so teammates don't step on each other.
- **Subagents & Teammates**: one-shot subagents and persistent teammate threads via MessageBus.
- **Context Compaction**: snip / micro / history compaction before each LLM call.
- **Error Recovery**: retry on 429/529, max_tokens escalation, prompt-too-long reactive compact.
- **Background Tasks**: slow bash operations run in daemon threads.
- **Cron Scheduler**: durable scheduled prompts.
- **MCP**: external tool integration through mock MCP servers.

## Project Structure

```text
nexus_agent/
  config.py          # Environment, paths, constants
  cli.py             # Interactive entry point
  agent.py           # Main loop
  llm.py             # Anthropic client + recovery
  context.py         # Context compaction
  hooks.py           # Hook registry + permission layer
  tools/             # Tool definitions and handlers
  tasks/             # Task graph + worktree
  teams/             # MessageBus, protocol, teammates, subagents
  memory/            # Skills + MEMORY.md
  scheduling/        # Cron + background tasks
  mcp/               # MCP client integration
```

## License

MIT License — see [LICENSE](LICENSE).  
Based on `learn-claude-code` by shareAI Lab.
