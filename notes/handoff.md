# Agent 接手说明

Nexus Agent v0.2 是生产化 runtime；`nexus_agent/agent.py` 等 v0.1 教学 harness 只作为兼容层保留。新功能应优先接入 `runtime.py`、`providers/`、`executor.py`、`policy.py` 和 `observability.py`。

## 接手顺序

1. 阅读 `README.md` 和 `docs/architecture.md`。
2. 运行 `uv sync --extra dev`。
3. 运行 `uv run pytest -q`、`uv run ruff check nexus_agent tests examples`、`uv run mypy nexus_agent`。
4. 运行 `uv run nexus-agent eval evals/smoke.yaml`。

## 关键约束

- 不在 import 时创建模型客户端或要求 API Key。
- 所有执行入口必须经过 `ToolExecutor` 和 `PolicyEngine`；文件工具仍需自校验。
- 同一 session 必须串行，不同 session 不共享历史或审批。
- 新事件写入 SQLite 前必须经过 `redact`。
- MCP 生产功能使用官方 SDK；mock 只供旧测试。
- 不声称本地 shell 是安全沙箱。
- 不提交 `.env`、`mcp.json`、`.nexus/`、数据库、trace 原文或 live 报告。

## 当前验证

80 tests，生产 Runtime 覆盖率 87.21%，Ruff/mypy 通过，offline eval 10/10，真实 stdio/HTTP MCP 通过。Web 配置已支持三类 Provider、加密持久化和真实连接检查；真实国内模型和 GitHub 发布尚未执行，因为需要用户凭证与明确授权。
