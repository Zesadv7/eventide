# Eventide 维护约定

本文件是新 Codex 对话进入仓库时的统一上下文入口。仓库文件和 Git 状态是事实来源，不依赖其他对话的历史。

## 开始工作

1. 阅读 `README.md`、`docs/architecture.md` 和 `docs/decisions.md`。
2. 运行 `git status --short`，确认当前工作区和用户已有修改。
3. 运行 `git log -5 --oneline`，了解最近完成的工作。
4. 按任务定位实现和测试，不从旧聊天推测当前代码状态。

## 多对话与 worktree

- 只读分析可以共享当前 checkout。
- 可能写入代码或文档的并行任务使用独立 Git worktree 和 `codex/` 前缀分支。
- 同一功能只由一个写入型对话负责；不要在两个 worktree 中同时实现同一修改。
- 不覆盖、还原或整理其他对话和用户的未提交改动。
- 提交保持小而完整；Git commit 是跨对话交接状态的主要载体。

## 生产实现边界

- 新 Runtime 功能优先进入 `runtime.py`、`providers/`、`executor.py`、`policy.py`、`observability.py`、`mcp/client.py`、`api.py` 或 `web/`。
- `agent.py`、旧 context/hooks、cron 和旧协作路径属于 legacy compatibility layer，除兼容性任务外不继续扩展。
- import、`--help`、单元测试和离线评测不得依赖 API Key 或网络。
- 所有生产工具调用必须经过 `ToolExecutor` 和 `PolicyEngine`，文件工具继续执行内部路径校验。
- 没有审批处理器、用户拒绝或审批超时时，`ASK` 默认拒绝。
- 同一 session 必须串行，不同 session 不共享消息或审批状态。
- MCP 生产路径使用官方 SDK，工具名保持 `mcp__server__tool`。
- 本地 Shell 不是安全沙箱，不把策略校验描述为操作系统隔离。

## 文档同步规则

- 用户可见能力、配置或运行方式变化：更新 `README.md`。
- 当前结构、接口、数据模型或工程约定变化：更新 `docs/architecture.md`。
- 长期设计取舍变化：在 `docs/decisions.md` 新增 ADR，或把旧 ADR 标记为 `Superseded` 并链接替代项。
- 不新增 progress、handoff、roadmap、简历或面试 Markdown；任务状态通过 Git 和 Codex 对话管理。

## 安全与仓库卫生

- 保留 `LICENSE`。
- 不提交 `.env`、`mcp.json`、`.eventide/`、API Key、数据库、trace 原文或 live 运行产物。
- 写文件前检查目标路径和现有改动；删除或移动前确认精确目标。
- 不创建远程、不推送、不公开发布，除非用户明确授权。

## 完成工作

根据修改范围运行相关检查；完整门禁为：

```bash
uv run python -m eventide --help
uv run pytest -q
uv run pytest --cov=eventide --cov-fail-under=85
uv run ruff check eventide tests examples
uv run mypy eventide
uv run eventide eval evals/smoke.yaml
```

完成后：

1. 更新受影响的稳定文档。
2. 运行 `git diff --check` 并复查 `git status --short`。
3. 创建一个说明清楚的本地提交；不要夹带无关修改。
4. 在最终回复中报告修改内容、验证结果、commit hash 和仍需用户处理的阻塞。

## 回复风格

- 回复稍微带点大白话：先说结论，再补细节，少堆术语。
