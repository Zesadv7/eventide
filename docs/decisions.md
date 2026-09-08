# Nexus Agent 架构决策

本文记录影响长期结构、边界或维护方式的决策。当前实现事实见 [architecture.md](architecture.md)。

决策状态：

- `Accepted`：当前采用；
- `Superseded`：已被后续决策替代；
- `Deprecated`：仍可能存在，但不再扩展。

## ADR-001：采用 async-first Runtime

**状态：Accepted**

**背景：** CLI、HTTP API、SSE、审批等待和 MCP 都包含异步操作。同步编排会阻塞服务线程，并迫使不同入口复制并发处理逻辑。

**决策：** 以 `AgentRuntime.run()` 作为 async-first 编排入口，CLI 使用 `asyncio.run()`，FastAPI 和评测直接 `await` Runtime。

**影响：** 所有生产入口共享同一个 Agent loop；同步工具通过线程边界执行；新增集成必须提供异步生命周期管理。

## ADR-002：使用 session 粒度并发

**状态：Accepted**

**背景：** 同一会话的历史必须保持顺序，不同会话不应被全局锁互相阻塞。

**决策：** 每个 `session_id` 使用独立 `asyncio.Lock`，消息按 session 持久化，不使用进程全局会话历史。

**影响：** 同一会话的 run 串行执行，不同会话可以并发；跨 Runtime 实例的分布式锁不在当前能力范围内。

## ADR-003：模型接入使用 Provider adapter

**状态：Accepted**

**背景：** 不同模型 API 的消息、工具调用、usage 和错误结构不同，直接在 Runtime 中处理会造成厂商耦合。

**决策：** Runtime 只依赖 `Provider.complete(ModelRequest) -> ModelResponse`。Anthropic Messages、OpenAI-compatible Chat Completions 和 OpenAI Responses 分别在 adapter 内转换。

**影响：** 新协议通过新增 adapter 接入；重试、工具执行和会话状态不进入 Provider；协议私有状态必须显式封装并避免泄露给其他 Provider。

## ADR-004：工具执行统一经过权限决策

**状态：Accepted**

**背景：** CLI、API、subagent 或直接 handler 调用如果使用不同策略，会产生可绕过的执行路径。

**决策：** 生产工具调用统一经过 `ToolExecutor` 和 `PolicyEngine`，权限结果为 `ALLOW`、`ASK` 或 `DENY`。文件工具保留内部路径检查作为纵深防御。

**影响：** 无审批界面、用户拒绝或超时一律拒绝；不可覆盖的高风险命令直接拒绝；新增工具必须进入同一执行边界。

## ADR-005：本地 Shell 不作为安全沙箱

**状态：Accepted**

**背景：** 命令字符串和宿主可执行程序具有广泛能力，仅靠正则策略无法形成操作系统隔离。

**决策：** 通过 `CommandExecutor` 抽象命令执行，当前实现为宿主机上的 `LocalCommandExecutor`，并明确其不是安全沙箱。

**影响：** 当前版本只适合受信任的单机使用；面向不可信输入前必须增加容器、微虚拟机、低权限身份和资源限制。

## ADR-006：使用 SQLite 保存状态与事件

**状态：Accepted**

**背景：** 仅依赖进程内 history 和终端输出无法支持服务重启后的查询、SSE 重连、JSONL 导出和确定性评测。

**决策：** 使用 SQLite 保存 session、message、run、event 和单行 provider configuration，事件按 run 内序号排序。

**影响：** 单机部署简单且可审计；需要显式关闭连接；当前不提供 schema 迁移和多节点一致性。

## ADR-007：MCP 使用官方 SDK 和显式边界

**状态：Accepted**

**背景：** 内存 mock 无法验证真实子进程、HTTP transport、生命周期和环境变量泄露风险。

**决策：** 生产路径使用官方 MCP Python SDK，支持 stdio 与 Streamable HTTP；工具使用 `mcp__server__tool` 命名；stdio 只传递显式白名单环境变量。

**影响：** mock 只作为测试 fixture；未明确标记只读的外部工具默认需要审批；Runtime 必须统一关闭 MCP 资源。

## ADR-008：离线评测使用 scripted provider

**状态：Accepted**

**背景：** 真实模型存在网络、费用、模型漂移和随机性，不能作为 CI 的唯一行为验证。

**决策：** 离线评测使用固定响应的 scripted provider，live 模式再复用真实 Provider。

**影响：** CI 可以确定性验证 Runtime、工具、权限、恢复和压缩；离线结果不代表真实模型质量或线上延迟。

## ADR-009：Web UI 使用 FastAPI 托管的原生前端

**状态：Accepted**

**背景：** 当前产品面向单机调试和演示，不需要独立前端构建链或复杂状态管理。

**决策：** 使用 FastAPI 托管原生 HTML、CSS 和 JavaScript，通过 REST 与 SSE 访问 Runtime。

**影响：** 安装和运行不依赖 npm；UI 保持轻量；账号、租户、复杂路由和前端框架不进入当前版本。

## ADR-010：模型密钥使用独立主密钥加密

**状态：Accepted**

**背景：** Web 保存配置需要跨重启恢复，但 API Key 不能以明文进入 SQLite、HTTP 响应或日志。

**决策：** 使用 Fernet 加密数据库中的 API Key。主密钥来自 `NEXUS_SECRET_KEY`，未配置时生成 `.nexus/secret.key`；配置变更和连接检查只接受回环请求。

**影响：** 数据库和主密钥需要分开保护；主密钥丢失时必须重新输入 API Key；解密失败不能静默回退成明文。

## ADR-011：保留 legacy compatibility layer

**状态：Accepted**

**背景：** 仓库仍包含旧的工具、context、hooks、调度、协作和同步 Agent 接口，直接删除会扩大迁移范围。

**决策：** 将旧接口视为 `legacy compatibility layer`，生产功能优先进入 `runtime.py`、`providers/`、`executor.py`、`policy.py`、`observability.py`、`mcp/client.py` 和 `api.py`。

**影响：** 兼容层继续可读和可测试，但不作为新增 Runtime 功能的默认落点；覆盖率门禁可以单独限定生产路径。

## ADR-012：保持单机、自部署边界

**状态：Accepted**

**背景：** 当前目标是提供可运行、可观察的 Agent Runtime，而不是多租户平台。

**决策：** 当前不实现账号系统、分布式队列、云部署、复杂前端或公网凭据管理。

**影响：** 默认绑定 `127.0.0.1`；SQLite 和进程内 session 锁只提供单机语义；扩展到公网或多节点前需要新的架构决策。
