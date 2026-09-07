# Codex System Instructions

You are working on **Nexus Agent**, a modular Python Agent Harness.

## Context

- The project is at `e:\projects\nexus-agent`.
- Package name: `nexus_agent`.
- Run tests with `python -m pytest -q`.
- Run the CLI with `python -m nexus_agent`.
- Read `README.md`, `docs/architecture.md`, `docs/progress.md`, and `docs/plan.md` for full context.

## Rules

1. The model decides; the harness executes. Do not add workflow orchestration or hardcoded decision trees.
2. Keep imports layered: `config.py` is the bottom layer; `agent.py`/`cli.py` are the top.
3. Avoid circular imports. Use `nexus_agent/utils.py` or `nexus_agent/tools/dispatch.py` for shared helpers.
4. Tool handlers must accept `cwd` explicitly.
5. Preserve MIT license and shareAI Lab attribution.
6. Every change needs a test.

## Before Committing

Run:

```bash
python -m pytest -q
echo "q" | python -m nexus_agent
```

Both must succeed.
