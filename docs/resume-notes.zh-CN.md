# 中文简历素材

以下描述只引用 2026-09-07 已生成的本机离线报告。真实模型数据完成 live 验收后再补，不要把离线毫秒延迟包装成线上推理性能。

## 可直接使用

- 将 shareAI Lab MIT 教学型 Agent Harness 生产化为 async-first Runtime，抽象 Anthropic/OpenAI-compatible Provider 与统一 ToolExecutor，支持同会话串行、跨会话并发隔离，并通过 SQLite/SSE 记录模型、工具、审批、压缩及 token 事件。
- 基于 `ALLOW/ASK/DENY` 实现 CLI/Web 一次性审批和默认拒绝策略，将路径校验下沉到文件工具，补充 worktree 基线提交检测、MCP 环境白名单及 trace 脱敏；10 项确定性安全/恢复评测全部通过。
- 使用官方 MCP Python SDK 打通真实 stdio 子进程和本地 Streamable HTTP 服务，搭建 FastAPI + 原生 Web 运行控制台；建立 Python 3.10–3.14 CI、Ruff/mypy/pytest 门禁，生产 Runtime 测试覆盖率 87.21%。

## 面试时主动说明

- 原始项目提供教学型工具、任务、协作等能力；本人新增的是 Runtime 边界与生产化基础设施，不把全部代码声称为原创。
- 当前 85.71% 工具成功率包含一个刻意失败的未知工具恢复用例；两次策略安全拦截不计为工具失败。
- 尚未写入简历：真实模型成功率、线上延迟、并发吞吐。它们需要用户 Key、固定模型版本和可重复的 live 报告。

## live 验收后模板

仅在报告真实存在后替换方括号：

> 在 `[模型与版本]` 上完成 `[N]` 项 live eval，通过率 `[X%]`，P50/P95 延迟 `[数值]`，工具成功率 `[数值]`；所有指标由脱敏 trace 自动聚合。
