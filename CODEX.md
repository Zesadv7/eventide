# Codex Instructions: Nexus Agent

## Project

Nexus Agent is a Python 3.10–3.14 async agent runtime derived from shareAI Lab's MIT `learn-claude-code` teaching example. Preserve attribution and distinguish v0.1 migrated capabilities from v0.2 production extensions.

## Production path

- `nexus_agent/runtime.py`: session isolation and model/tool loop.
- `nexus_agent/providers/`: Anthropic and OpenAI-compatible adapters.
- `nexus_agent/executor.py` + `policy.py`: the shared execution boundary.
- `nexus_agent/observability.py`: SQLite and redacted events.
- `nexus_agent/mcp/client.py`: official SDK transports; legacy mocks are fixtures only.
- `nexus_agent/api.py` + `web/`: FastAPI, SSE, approvals, console.
- `nexus_agent/evaluation.py`: deterministic offline and opt-in live evals.

`agent.py`, `context.py`, old hooks, cron, and legacy orchestration remain for source compatibility. Do not extend them for new runtime behavior unless compatibility specifically requires it.

## Rules

1. Keep imports side-effect free: help/tests/offline eval must not need a key or network.
2. Route every tool call through `ToolExecutor` and `PolicyEngine`; retain defense-in-depth inside filesystem tools.
3. Default `ASK` to deny when no approval surface responds.
4. Never persist secrets or unrestricted large tool output.
5. Preserve per-session locks and avoid process-global session state.
6. Use official MCP transports and namespace tools as `mcp__server__tool`.
7. Treat the local shell as unsandboxed.
8. Add deterministic tests for each behavior; live checks cannot replace offline tests.

## Verification

```bash
uv run python -m nexus_agent --help
uv run pytest -q
uv run pytest --cov=nexus_agent --cov-fail-under=85
uv run ruff check nexus_agent tests examples
uv run mypy nexus_agent
uv run nexus-agent eval evals/smoke.yaml
```

Never push, create a remote, publish, or use a live API key without explicit user authorization.
