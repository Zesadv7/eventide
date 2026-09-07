# 后续计划

本轮作品集升级的代码范围已完成。以下事项不是遗留实现缺陷，而是需要外部凭证、平台或后续版本授权的工作。

## 发布前

1. 用户提供一个真实工具调用模型 Key，运行 CLI/Web/live eval 验收。
2. 从真实运行录制截图与短 GIF，更新 README；不使用模拟图冒充 live 结果。
3. 用户确认仓库名称和可见性后创建 remote、推送并观察 CI 矩阵。
4. 只把 live 报告中的脱敏聚合数字写入简历。

## 可选 v0.3

- Docker/微虚拟机 `CommandExecutor`，把策略层升级为真正的进程隔离。
- SQLite 事件迁移与 trace replay CLI。
- HTTP MCP 本地服务的端到端进程测试和 OAuth 流程。
- 结构化 provider capability negotiation，明确不同模型的 tool/vision/context 能力。

## 非目标

不在当前版本加入分布式队列、账号体系、云部署或前端框架。项目继续保持 coding-agent/runtime 定位，不转向单一业务领域。
