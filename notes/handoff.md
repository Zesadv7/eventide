# Handoff Notes for the Next Agent

## What This Project Is

Nexus Agent is a modular Python implementation of an Agent Harness.
It was built by extracting and refactoring the 2120-line `s20_comprehensive/code.py`
from `learn-claude-code` into a multi-file package.

Core idea: **the model makes decisions; the harness provides the environment.**

## Quick Start

```bash
cd e:\projects\nexus-agent
pip install -r requirements.txt
cp .env.example .env
# fill in ANTHROPIC_API_KEY and MODEL_ID
python -m nexus_agent
```

Run tests:

```bash
python -m pytest -q
```

## Key Files to Know

| File | What it does |
|---|---|
| `nexus_agent/config.py` | Loads `.env`, creates Anthropic client, defines paths/constants. |
| `nexus_agent/agent.py` | Main loop: LLM call → tool dispatch → result → repeat. |
| `nexus_agent/cli.py` | Interactive prompt + cron auto-run thread. |
| `nexus_agent/tools/registry.py` | Lists all 27 tool schemas and their handler mappings. |
| `nexus_agent/hooks.py` | Permission hooks + hook registry. |
| `nexus_agent/context.py` | Context compaction pipeline. |
| `nexus_agent/llm.py` | Retry/fallback/error-recovery wrapper. |
| `nexus_agent/tasks/` | Task graph + git worktree isolation. |
| `nexus_agent/teams/` | MessageBus, subagents, teammates, protocol. |
| `nexus_agent/scheduling/` | Cron + background tasks. |
| `nexus_agent/memory/` | Skills + MEMORY.md. |
| `nexus_agent/mcp/` | Mock MCP client + tool pool assembly. |

## Common Tasks

### Add a new tool

1. Implement handler in `nexus_agent/tools/<module>.py`.
2. Add schema to `BUILTIN_TOOLS` in `nexus_agent/tools/registry.py`.
3. Add handler to `BUILTIN_HANDLERS` in `nexus_agent/tools/registry.py`.
4. Add a test in `tests/`.

### Change the system prompt

Edit `assemble_system_prompt()` in `nexus_agent/agent.py`.

### Add a new skill

Create `skills/<skill-name>/SKILL.md` with YAML frontmatter (`name`, `description`).
Restart the agent to pick it up.

### Connect a real MCP server

Currently only `docs` and `deploy` mock servers exist in `nexus_agent/mcp/client.py`.
To add a real server, extend `MCPClient` or replace the mock registry with stdio/SSE transport.

## Current State

- All 37 tests pass.
- CLI starts and exits cleanly.
- No `.env` is committed; user must create one.
- Project is ready for GitHub upload.

## Open Questions for the User

1. Do they want to keep the coding-agent domain or pivot to another domain?
2. Which model provider do they want to support next (Anthropic only, or also DeepSeek/OpenAI)?
3. Do they want a web UI before uploading to GitHub?
