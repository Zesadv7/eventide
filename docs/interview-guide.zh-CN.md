# 面试说明

## 30 秒介绍

Nexus Agent 从一个能调用工具的教学 harness 出发，升级成 async-first 的 agent runtime。重点不是增加更多 demo 工具，而是建立 provider、executor 和 runtime 边界，并补齐权限审批、MCP 生命周期、持久化 trace、确定性 eval、会话并发隔离和 Web 可视化。

## Agent loop

每轮把 session history、system prompt 和统一工具 schema 发给 provider。模型返回文本或工具调用；工具调用先经 policy，再执行并把 `tool_result` 追加到历史，直到出现最终文本或达到步骤上限。模型负责决策，Runtime 不硬编码工作流。

可追问：为什么限制最大步骤？防止模型/工具错误形成无限循环，并为失败 trace 提供确定终态。

## Provider boundary

Runtime 只认识 `ModelRequest/ModelResponse`。Anthropic 与 OpenAI-compatible adapter 负责消息块和 tool-call 格式转换。重试语义通过 `ProviderError.retryable` 回到 Runtime，所以供应商 SDK 不会渗入权限和追踪层。

可追问：为什么 OpenAI-compatible 用 Chat Completions？国内兼容服务的工具调用支持面更广；adapter 隔离了协议差异，之后可增加 Responses adapter 而不改 agent loop。

## MCP 生命周期

Runtime 初始化时读取配置，用官方 SDK 建立 stdio 或 Streamable HTTP client，发现工具后加命名空间，关闭时通过 `AsyncExitStack` 回收会话和子进程。stdio 只传白名单环境变量，避免把宿主全部 secrets 继承给第三方 server。

可追问：如何信任 MCP annotations？只读 hint 用于减少审批摩擦，但未知/写操作默认 `ASK`；高风险安全不只依赖服务端自报，文件与命令仍有本地不可覆盖规则。

## 权限决策

`ALLOW` 执行，`ASK` 交给 CLI/Web approval handler，`DENY` 不可覆盖。无人响应、非交互或超时默认拒绝。文件路径在 executor 入口和工具内部都校验，避免 subagent/teammate 直接调用 handler 绕过策略。

可追问：这是沙箱吗？不是。它是应用层能力策略，无法防御所有 shell 逃逸；真正隔离应在 `CommandExecutor` 后接容器或微虚拟机。

## 上下文压缩

Runtime 在每轮调用前按序列化预算做确定性压缩，保留最新完整 turn，并产生 `context.compacted` 事件。显式 `compact` 工具走同一机制。这样压缩是否发生、移除了多少消息都可测试，而不是隐藏在 provider 内。

## Trace 与 eval

SQLite 保存 run/model/tool/approval/compaction 事件；事件写入前脱敏和截断。离线 eval 用 scripted provider 固定模型响应，因此能在 CI 确定性验证工具选择、安全拒绝、恢复、重试和 session 隔离。live eval 用于证明某个真实模型效果，但不替代 CI。

可追问：为什么工具成功率不是 100%？smoke suite 故意注入未知工具来验证恢复；预期安全拦截从成功率分母分离，并单独报告。

## 并发隔离

锁的粒度是 session id。同一 session 的两个 run 串行，避免 history 交错；不同 session 可以并发。消息持久化按 session/sequence 分区，审批 future 也按 approval id 管理。测试用两个并发 session 验证输出和历史不串线。

## 取舍

- SQLite 换取单机可复现和零外部依赖，放弃水平扩展。
- 原生 Web UI 换取无 npm 的五分钟演示，放弃复杂组件生态。
- 同时保留 v0.1 兼容层与 v0.2 Runtime，降低迁移风险，但覆盖率口径必须清楚区分。
- 不在没有 Docker 的开发机上伪造容器验收，而是公开安全边界。
