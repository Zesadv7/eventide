# Eventide 架构

## 核心关系

```text
RuntimeHost
  └─ Workspace
      └─ Session
          └─ Runtime Event Log
              ├─ MessagesProjection
              ├─ RuntimeStateProjection
              └─ ContextBuilder
```

RuntimeHost 是进程内唯一执行 Owner，持有状态根的 OS 文件锁、RuntimeStore、Provider 配置、SessionManager 和 Workspace 锁。AgentRuntime 是兼容门面，CLI、FastAPI 和 Eval 复用 Host。首版不含后台 daemon 或 IPC client：同状态根已有 Host 时第二个进程失败。进程终止由 OS 释放文件锁。

Workspace 保存稳定 ID、规范路径、Git 根、名称和创建时间；Git 子目录统一绑定仓库根。Session 的 Workspace 绑定不可改变；工具 cwd 只由该绑定解析。同 Workspace 全 run 串行，不同 Workspace 可并发。服务关闭时取消并等待活跃任务，工具执行线程结束后才释放 Workspace 所有权。

主要实现：`host.py` 管理生命周期与 Agent loop；`workspace.py` 管理路径、锁和 Git evidence；`store.py` 保存身份与事实；`projections.py`、`context_builder.py` 负责投影。生产工具位于 `tools/runtime_catalog.py`，不导入 legacy 注册表。

## 存储与事件契约

默认用户状态根中包含 `runtime.sqlite`、`host.lock` 和按需生成的 `secret.key`。EVENTIDE_STATE_DIR 优先；SQLite 使用 WAL、外键和 schema_migrations。首次创建版本 1，未知版本和未版本化旧数据库明确拒绝，不自动迁移或删除。

| 表 | 职责 |
|---|---|
| workspaces | Workspace 身份、规范路径、Git 根 |
| sessions | 身份、固定 Workspace 绑定、创建时间 |
| turns | 用户回合身份、session、continuation_of |
| runs | 执行身份、session、turn、开始时间 |
| runtime_events | 唯一运行事实源 |
| context_checkpoints | 可失效的摘要投影及覆盖证明 |
| provider_config | Host 级模型配置和加密凭据 |
| schema_migrations | 已应用 schema 版本 |

RuntimeEvent 包含 event_id、session_id、严格递增 session_seq、turn_id、run_id、ts、type、role、author、payload、partial、schema_version。SQLite 事务分配 session_seq；唯一索引防止重复序号，触发器禁止事件 UPDATE/DELETE。一次 run 的第一个终态胜出；终态后的普通事件拒绝写入。partial 不进入消息、状态投影，且不能作为终态。

身份表不保存独立运行状态。消息与状态查询每次折叠事件，不依赖不可重建的缓存或 messages 表。TraceStore 在 v0.3 中是 RuntimeStore 的别名；兼容 append_message() 追加 message.imported 事件，生产主链不调用它。

主要事件：

- run.started、message.user、model.request、model.response、model.retry；
- tool.prepared、tool.completed、tool.abandoned；
- approval.required、approval.resolved；
- workspace.checkpoint、context.configured、context.compacted、context.trimmed；
- run.completed、run.failed、run.interrupted。

模型响应保存结构化 content blocks，包括 provider_state 与带稳定 ID 的 tool_use。工具执行前提交 prepared，结束后提交 completed；工具组收齐结果后才形成 provider-neutral tool_result 消息。副作用结果之后另存 workspace checkpoint；若两者之间崩溃，Continue 拒绝缺失 checkpoint 的历史。

SSE/get_events 为旧客户端把 prepared/completed 映射成 tool.request/tool.result，并使用 session_seq 作为兼容 seq 游标；JSONL 导出使用 canonical 事件名称。事件先提交，再通知观察者；观察者失败不改变运行事实。

## 三类投影与上下文

MessagesProjection 只消费已提交消息和工具事实，保留工具配对、次序及 Responses 私有状态。未配对历史拒绝发送给模型；其他 Provider 按 adapter 契约忽略不适用的私有状态。

RuntimeStateProjection 从事件得到 running、waiting_for_user、completed、failed、interrupted、usage、步骤、工具和待审批状态。SessionManager 将最新 interrupted run 展示为 parked；pending approval 保留历史证据，审批提交还必须匹配该 run 与当前进程的 pending future。

ContextBuilder 每次读取日志，构造模型输入；根 AGENTS.md 最多读取 4,000 字符，拒绝指向 Workspace 外的链接。每轮 run 记录指令 hash 和工具目录 hash。消息与工具结果进入语义记录时会脱敏，但正文不静默截断；下一模型请求读取同一记录。Provider 返回的原始工具参数用于实际执行，持久事件中的敏感字段使用脱敏副本，避免审计规则改变工具行为。已知模型 API Key 在文本中也会替换。预算不足时，当前 turn 内较早的工具结果在构造请求时折叠为占位串，保留最近三条原文与 tool_use/tool_result 配对，事件日志仍保存原文。read_file 自行分页并报告文件总行数与下一页 offset。UI 可以独立裁剪展示。

持久 checkpoint 保存 covered_seq、source_digest、summary、policy_version、provider、model。摘要只覆盖结束 turn 的完整前缀；模型调用使用 summary + 未覆盖 raw tail。checkpoint 来源 digest、策略和模型身份不符时忽略；摘要生成失败且旧投影仍在预算内时使用旧投影；摘要之后仍超预算时先折叠当前 turn 的旧工具结果，仍不够才返回 context_overflow。压缩与折叠都不改变原始事件。当前 context_limit 仍按序列化消息字符数计量，不宣称 token 精确计量。

## 中断与 Continue

启动时扫描缺少终态的 run，追加 run.interrupted，不调用模型。单次 run 用尽步数预算（`max_steps`，默认 30）时所有工具结果已提交，Host 先记录工作区 checkpoint，再写入 run.interrupted，Session 停驻，用户可 Continue 到新 turn 并获得新的步数预算。用户主动 Continue 必须通过 Workspace 锁内检查：

1. 最新 run 为 interrupted，且 Workspace 为可验证的 Git 仓库。
2. 当前 HEAD、暂存与未暂存 binary diff digest、未跟踪文件路径和内容 digest 与最后可信 checkpoint 相同。
3. 不存在结果未知的非只读 prepared 调用；MCP 即使声明只读，也采用保守的恢复策略。
4. 已完成副作用工具后存在 checkpoint；无法采集证据时拒绝继续。

Continue 创建新 turn/run，并以唯一 continuation_of 关联来源 run；不会重放旧工具或重复用户消息。未完成 read_file/glob/compact 和确定尚未派发的调用写入 abandoned，补齐下一次模型请求的 tool-result 结构。只读结果也不伪装成成功。

没有 HEAD、非 Git、存在 submodule、证据采集失败、未知副作用或源码变化时维持 parked 并解释原因。用户可以显式 abandon：Runtime 创建一个不调用模型的关联 run，把未配对调用记录为结果未知且未重放，保留全部历史并解除 Session 停驻。范围仅为 Git 可见源码；ignored 文件、外部 MCP 服务和 OS 全局状态不在快照内。不是指令级恢复，也不是操作系统沙箱。

## 入口、模型与 MCP

保留 AgentRuntime.run(RunRequest)、create_session、Provider 配置和 RunResult 原字段；RunResult 新增带默认值的 turn_id、continuation_of。RuntimeHost 还提供 resolve_or_register_workspace、continue_session、session_status。

CLI 提供 run/chat/serve 的 --workspace、workspace add/list/show/remove 和 continue。无指定 Workspace 时使用启动 cwd。HTTP 保留原路由，新增 /api/workspaces、/api/workspaces/{id}/sessions、/api/sessions/{id}、/messages、/runs 和 /continue。Continue 同步等待结果；原 run 提交仍为 202。Web 不新增独立 Continue 执行路径，而是观察 Session/Run Projection 发现同步 Continue 创建的新 Run。HTTP 同 Workspace 已有请求时返回 409；Python Host 的请求按 Workspace 锁排队。

模型配置为 Host 级，活跃 run 期间不允许修改。Provider 客户端惰性创建，连接检查不切换活动 Provider。密钥继续使用 Fernet；主密钥优先 EVENTIDE_SECRET_KEY，否则状态根 secret.key。解密失败不退回明文。凭据管理及 Workspace 注册/移除仅接受本机回环请求。

每个 run 按 Workspace 读取 mcp.json；MCP SDK transport 在同一 owning task 内连接和关闭，避免跨 task 的资源退出。所有工具统一进入 ToolExecutor/PolicyEngine，文件工具内部再次检查路径。生产默认目录仅包含文件、Shell 和 compact；旧 task/worktree/teammate/cron 保留为兼容代码。离线 Eval 显式关闭真实 MCP，使用独立评测数据库和 scripted Provider。

## Web 展示投影与交互

Web 是原生 ES modules，无构建步骤。`app.js` 协调 Workspace/Session 选择、API 状态、审批、Continue 与工作记录页；`projection.js` 提供纯展示投影；`transport.js` 消费同一 SSE 路由；`view.js` 处理稳定 DOM 与安全 Markdown 子集；`config.js` 管理 Host 模型配置。

传输层将兼容 tool.request/result 名称归一化，按 event_id 或 run/seq 去重、按 session_seq 排序；流断开后从最后已消费游标补齐，即使 Run 已终止也不跳过尾部事件。运行状态和 pending approval 始终查询服务端 Runtime Projection，事件通知触发状态协调，周期查询发现其他入口启动的 Run。异步结果按所属 Workspace/Session 缓存，不写入当前选中 Session 的其他记录。

Semantic Work Projection 从 canonical 事件证据与 Run 关联生成只读工作片段：工具 prepared/completed 按 run_id/call_id 配对，保留请求参数与结果；Continue 的 abandoned 沿 continuation_of 找到原操作，未派发调用可关联原 model.response。调用状态区分等待结果、中断时结果未确认、完成、失败、拒绝和 abandoned。普通 checkpoint 不切分阶段，也不在主视图占一行；null 不被描述成有效证据。有限的命令分类只识别明确调用，未知 Bash/MCP 保持中性措辞；操作完成不推导未记录的验证结论。

每条用户意图和其 Continue 链组成一个章节；最新章节默认展开，旧章节的终态事件按需加载，工具详情默认折叠。Execution Block 包含稳定 ID、来源事件、Run、操作状态和可读描述，不写 SQLite。最终输出只在最新成果区或对应旧章节展示一次。界面显示的“本次执行结束”区别于 Session 生命周期结束。

Continue 保留同步 POST 契约；等待返回期间并行发现新 Run 并立即订阅，包括处理新审批。仅最新 parked Session 提供 Continue；409 留在恢复区，不发普通 Prompt 绕过。审批按钮提交前检查服务端 pending 状态。历史展开、草稿、阅读位置按 Session/章节保存于页面内存；选择项保存在 localStorage。按稳定 key 更新发生变化的 DOM，Inspector 以 ID 获取最新投影，使用原生 dialog 支持 Escape 和焦点返回。Markdown 通过 DOM 文本构造，禁用原始 HTML 和非 HTTP(S)/mailto 链接协议。

## 工程边界

系统仍面向可信单机使用：没有账号、多租户、公网鉴权、容器沙箱、分布式锁或 Agent Graph。状态根锁不等于工具安全隔离。AGENTS.md 是模型指令，不会扩张 PolicyEngine 权限。生产代码的 import、--help、测试与离线评测不需要 API Key 或网络。

完整验证门禁见 AGENTS.md。测试创建隔离目录和独立状态根，Git 测试使用测试仓库；不使用或删除用户级真实状态。
