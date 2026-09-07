# Claude Context: Nexus Agent

## Project Identity

- **Name**: Nexus Agent
- **Type**: Python package — modular Agent Harness
- **Origin**: Refactored from `learn-claude-code/s20_comprehensive` (MIT, shareAI Lab)
- **Goal**: Resume-worthy, runnable agent harness with tests and docs

## How to Work on This Project

1. Prefer small, testable changes.
2. Run `python -m pytest -q` after every edit.
3. Keep imports layered: `config` is the bottom layer; `agent`/`cli` are the top.
4. Avoid circular imports. If two modules need the same helper, put it in `nexus_agent/utils.py` or `nexus_agent/tools/dispatch.py`.
5. Preserve MIT license and attribution to shareAI Lab.

## Key Constraints

- `nexus_agent/config.py` must not import other internal modules.
- Tool handlers receive `cwd` explicitly; do not rely on global `WORKDIR`.
- Hook registry is global but can be cleared for tests via `hooks.clear_hooks()`.
- MCP tools are merged at runtime by `assemble_tool_pool()`.
- The CLI must remain runnable with `python -m nexus_agent`.

## Testing

```bash
python -m pytest -q
```

- Use `monkeypatch` to mock the Anthropic client (`nexus_agent.agent.client.messages.create`).
- Use `monkeypatch.setattr("nexus_agent.llm.time.sleep", lambda _: None)` to speed up retry tests.
- Avoid `tmp_path` if it causes permission errors on Windows; use `tempfile.mkdtemp()` instead.

## Documentation Standards

- Keep README.md honest and demo-focused.
- Put design rationale in `docs/architecture.md`.
- Put progress in `docs/progress.md`.
- Put future plans in `docs/plan.md`.
- Put agent handoff context in `notes/handoff.md`.
