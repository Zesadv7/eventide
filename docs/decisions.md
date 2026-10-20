# Eventide 架构决策

本文记录影响长期结构、边界或维护方式的决策。当前实现事实见 [architecture.md](architecture.md)。

决策状态：

- `Accepted`：当前采用；
- `Superseded`：已被后续决策替代；
- `Deprecated`：仍可能存在，但不再扩展。

## ADR-001：采用 async-first Runtime

**状态：Accepted**

**后续修订：** [ADR-043](#adr-043统一生产入口并退役教学兼容层) 修订了本条的旧 Python 入口/兼容代码与执行边界；当前行为见 architecture.md。

**背景：** CLI、HTTP API、SSE、审批等待和 MCP 都包含异步操作。同步编排会阻塞服务线程，并迫使不同入口复制并发处理逻辑。

**决策：** 以 `AgentRuntime.run()` 作为 async-first 编排入口，CLI 使用 `asyncio.run()`，FastAPI 和评测直接 `await` Runtime。

**影响：** 所有生产入口共享同一个 Agent loop；同步工具通过线程边界执行；新增集成必须提供异步生命周期管理。

## ADR-002：使用 session 粒度并发

**状态：Superseded**，由 ADR-014 的 Workspace 级互斥替代。

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

**状态：Superseded**，由 ADR-013 的唯一事件事实源替代。

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

**状态：Superseded，由 ADR-055 的 Vue 工作台及[ADR-085](#adr-085web唯一入口与用户终端退役)替代。**

**背景：** 当前产品面向单机调试和演示，不需要独立前端构建链或复杂状态管理。

**决策：** 使用 FastAPI 托管原生 HTML、CSS 和 JavaScript，通过 REST 与 SSE 访问 Runtime。

**影响：** 安装和运行不依赖 npm；UI 保持轻量；账号、租户、复杂路由和前端框架不进入当前版本。

## ADR-010：模型密钥使用独立主密钥加密

**状态：Accepted**

**背景：** Web 保存配置需要跨重启恢复，但 API Key 不能以明文进入 SQLite、HTTP 响应或日志。

**决策：** 使用 Fernet 加密数据库中的 API Key。主密钥来自 `EVENTIDE_SECRET_KEY`，未配置时生成 `.eventide/secret.key`；配置变更和连接检查只接受回环请求。

**影响：** 数据库和主密钥需要分开保护；主密钥丢失时必须重新输入 API Key；解密失败不能静默回退成明文。

## ADR-011：保留 legacy compatibility layer

**状态：Superseded**，由 [ADR-043](#adr-043统一生产入口并退役教学兼容层) 替代。

**背景：** 仓库仍包含旧的工具、context、hooks、调度、协作和同步 Agent 接口，直接删除会扩大迁移范围。

**决策：** 将旧接口视为 `legacy compatibility layer`，生产功能优先进入 `runtime.py`、`providers/`、`executor.py`、`policy.py`、`observability.py`、`mcp/client.py` 和 `api.py`。

**影响：** 兼容层继续可读和可测试，但不作为新增 Runtime 功能的默认落点；覆盖率门禁可以单独限定生产路径。

## ADR-012：保持单机、自部署边界

**状态：Accepted**

**背景：** 当前目标是提供可运行、可观察的 Agent Runtime，而不是多租户平台。

**决策：** 当前不实现账号系统、分布式队列、云部署、复杂前端或公网凭据管理。

**影响：** 默认绑定 `127.0.0.1`；SQLite 和进程内 session 锁只提供单机语义；扩展到公网或多节点前需要新的架构决策。

## ADR-013：Event Log 是唯一运行事实源

**状态：Accepted**，替代 ADR-006。

**决策：** runtime_events 追加保存消息、工具、审批、usage 和终态。Messages、Runtime State 和 Context Builder 只从该日志投影；身份表不保存第二份运行状态。上下文 checkpoint 是带来源 digest 的有损投影，不改变日志。

**影响：** 增加版本化 schema、不可变触发器和唯一终态约束。应用不会把旧 schema 直接当作当前数据库打开或自动删除；v0.2 数据通过 ADR-024 的显式只读导入迁移。TraceStore 名称和常用入口保留兼容，JSONL 输出 canonical 事件，旧 SSE 名称通过适配提供。

## ADR-014：RuntimeHost 拥有 Workspace 执行权

当前修订：并发及源锁规则由ADR-063/065修订；AgentRuntime兼容门面已由ADR-043退役，用户CLI已由ADR-085退役。下文保留原决策背景。

**状态：Accepted**，替代 ADR-002。

**决策：** 一个状态根由一个进程内 Host 持有 OS 文件锁，Workspace 绑定规范项目目录，Session 永久绑定 Workspace。同 Workspace 全 run 互斥，不同 Workspace 可并发。Provider 配置属于 Host；MCP 和指令按 Workspace 解析。

**影响：** 默认状态移到用户级目录；serve 与临时 CLI Host 不能同时打开同一状态根。AgentRuntime 为兼容门面。后续同项目并行写作须经独立 Git worktree；常驻 Host 协议和 Agent Graph 延后。

## ADR-015：中断检测与用户主动 Continue

**状态：Accepted**。

**决策：** 重启补写 interrupted，Session parked。仅用户主动请求、Git 可见源码 checkpoint 一致且无未知副作用时创建关联旧 run 的新 turn/run。未知 Bash/MCP/写工具不自动重试；只读未完成调用明确 abandoned。

**影响：** 不承诺指令级恢复、ignored 文件或宿主机全状态恢复。无 HEAD、非 Git、submodule 或无法采集证据时拒绝 Continue。旧兼容调度和协作工具不进入生产 Host 默认工具目录。

## ADR-016：Web 使用语义化工作投影

Superseded（原生前端实现由ADR-055/085取代；事件权威与可重建投影原则保留）。下文为历史实现记录。

**状态：Accepted**。

**背景：** Runtime Event Log 是运行事实源，但 canonical event 粒度不适合直接作为用户界面。逐项展示模型请求和工具事件会把工作台变成日志查看器，掩盖 Workspace、Run 和恢复生命周期。

**决策：** Web 保留 RuntimeStateProjection 作为状态事实来源，在其上增加只读的 Semantic Work Projection。该投影按工具证据和事件边界将多个事件聚合为 Preparing context、Exploring workspace、Modifying workspace、Running validation、Checkpoint、Approval、Parked 和 Result 等 Execution Blocks。Model request/response 默认只进入 Inspector；retry、error、approval 和 interruption 等影响用户理解的事实才提升到主 Narrative。

**影响：** Execution Blocks 是可重建的展示状态，不写入 SQLite，也不改变 RuntimeHost、RuntimeStore、ToolExecutor 或 PolicyEngine 边界。无法由事件可靠证明的文件数量、验证结果或修改行为不得由 UI 猜测。Web 保持原生 HTML/CSS/JavaScript，不引入大型前端框架。具体工作记录组织和证据展示由 ADR-017 细化，普通 checkpoint 不再作为独立主视图片段。

## ADR-017：Session 作为持续的工作记录

Superseded（原生页面及JS门禁由ADR-055/085的Vue实现取代；Session/Run事实边界保留）。下文为历史实现记录。

**状态：Accepted**，细化 ADR-009、ADR-016 的产品表达与展示投影。

**背景：** 按 Run 编号排列阶段名称和调用数量仍接近日志摘要，无法充分表达持续工作；逐事件分类还会将工具请求和结果分开，误报修改或验证状态。

**决策：** 工作记录页先表达目标、当前状态、最近成果，再按用户意图及 Continue 链组织历史章节。Run 仍是执行事实身份，但不作为页面标题层级。请求和结果先按操作身份配对，再聚合工作片段；checkpoint、模型协议和原始 Events 进入临时 Inspector。审批与停驻恢复是主界面动作。Run completed 显示“本次执行结束”，不引入 Session 永久完成状态。

**影响：** 展示投影全部可由日志重建，不新增数据库事实或模型摘要调用。前端拆分原生模块、按所属 Session 协调异步请求；Continue 的 HTTP 契约由 ADR-025 改为接受后立即返回。验收增加离线 JS 投影/传输测试及内存 HTTP/SSE 浏览器场景，覆盖审批、恢复、断线补齐、选择隔离、历史折叠与小屏操作。

## ADR-018：当前轮工具结果按需折叠

**状态：Accepted**，细化 ADR-013 的模型输入投影。

**背景：** 上下文摘要只覆盖已结束 turn 的完整前缀，当前 turn 永远原样保留。一旦单个 turn 自身的序列化消息超过 `context_limit`，任何 checkpoint 都无法缩小它，运行只能以 `context_overflow` 失败；实测中一次读文档加查看 Git 的长 turn 因此整轮作废。

**决策：** 构造模型请求时，若摘要之后仍超预算，把当前 turn 内较早的 `tool_result` 内容替换为占位串，直到落入预算：保留最近三条工具结果原文，只替换 `content` 字符串以维持 tool_use/tool_result 配对，事件日志不写入折叠结果，并记录 `context.trimmed` 供界面展示。

**影响：** 折叠是请求侧的有损投影，不改变事件日志、checkpoint 的 `source_digest`、Continue 的 Git 证据或 JSONL 导出；原文仍可通过工作记录和日志取回。模型可能丢失早期工具输出细节，因此保留最近证据并让折叠可观测。不做输出落盘，也不自动重试副作用工具。

## ADR-019：步数预算用尽后停驻可继续

**状态：Accepted**，扩展 ADR-015 的停驻来源。

**背景：** 单次 run 受 `max_steps`（默认 30）限制。用尽后原实现抛异常并以 `run.failed` 结束：工具结果虽已提交，但用户既不能 Continue，也无法让模型接着做，只能重新描述任务。大型任务因此常在步数而非上下文上失败。

**决策：** 步数用尽且所有工具结果已提交时，Host 先记录工作区 checkpoint，再写入 `run.interrupted`，Session 停驻；用户 Continue 创建新 turn/run 并获得新的步数预算。终端事件仍复用 `run.interrupted`，不新增状态类型，也不自动放大步数预算。

**影响：** Continue 的安全检查不变（Git 证据、未知副作用、post-tool checkpoint）；停驻时补记 checkpoint，使纯只读 run 也能提供可信证据，因此 checkpoint 必须与 JSON 往返后的形态可比。模型输出被 `max_tokens` 截断且没有工具调用时改为失败并提示提高预算，不再静默报成功。

## ADR-020：停驻恢复可以由用户显式放弃

**状态：Accepted**。

**背景：** 严格 Continue 会拒绝非 Git、源码变化或结果未知的副作用调用；若普通 Prompt 也继续禁止，Session 会永久停驻。

**决策：** 用户可以显式 abandon 当前停驻恢复。Runtime 创建不调用模型的关联 run，为未配对工具写入 `tool.abandoned`，记录 `recovery.abandoned` 并恢复普通 Prompt。历史、未知结果和审批证据全部保留，不声称工具失败或成功，也不自动重放。

**影响：** abandon 会消费该 interrupted run 的 continuation 关系，因此之后不能再对同一来源 Continue；Web、HTTP 和 CLI 提供同一操作。Workspace 路径仍必须在后续实际 Run 前有效。

## ADR-021：Run 必须可按身份停止并受外部等待上限约束

**状态：Accepted**。

**背景：** 仅在 Host 关闭时取消匿名 task，无法让用户停止单个长任务；Provider、MCP 或 Shell 卡住还会长期占用 Workspace。

**决策：** Host 维护活跃 run_id 到 owning task 的映射，并提供幂等边界明确的取消入口。取消沿异步调用传播，Shell 使用可终止进程树的异步子进程实现；同步线程工具仍等待实际结束后释放 Workspace。模型请求、MCP 连接/调用和 Shell 使用可配置超时。

**影响：** 用户取消写入 `run.interrupted` 并进入既有 parked/Continue/abandon 流程。HTTP 与 Web 可以停止特定 Run；已结束或不存在的 Run 不伪装成成功取消。本地 Shell 仍不是安全沙箱。

## ADR-022：Session 历史通过归档退出主工作流

**状态：Accepted**。

**背景：** Session 是不可变运行事实的长期容器，但只有创建和查看入口。真实用户无法给工作记录命名，也无法把结束的记录移出日常列表；直接删除有历史的 Session 又会破坏审计链。

**决策：** Session 身份表增加显式标题和归档标记，schema 从版本 1 自动迁移到版本 2。API 支持修改标题与归档状态，项目 Session 列表默认隐藏归档记录。硬删除只允许没有 Run 和 Runtime Event 的空 Session；有历史的记录必须归档。

**影响：** 显式标题优先于首条用户消息生成的回退标题，事件日志仍不变。归档是可逆的展示生命周期，不代表执行完成，也不会解除 Workspace 对历史记录的保护。Web 通过右键或可见的更多按钮打开同一管理入口，并可切换查看、恢复已归档记录。

## ADR-023：长期历史使用轻量索引和游标分页

**状态：Accepted**。

**背景：** Event Log 可以长期增长，但导航状态、Run History 和 SSE 增量读取原先都会重复解码完整事件范围；单次 Session 使用越久，刷新与流式轮询越慢。

**决策：** Session 列表通过定向 SQL 读取首次意图、最新活动和最新 Run 摘要；Run History 只投影索引所需的审批与终态，并用数据库聚合生成计数。HTTP 默认返回最近 100 条 Run，以稳定 `run_id` 作为向前游标。SSE 的 `after` 条件直接下推到 SQLite；完整 Event 与完整 Runtime Projection 仅在对应详情、恢复和上下文构建中读取。

**影响：** 不新增第二份运行事实，也不改变不可变日志。历史索引不再返回完整工具、usage 或 checkpoint 投影；需要这些事实的调用者读取单 Run 详情或事件。Web 显式加载更早页面，并在内存中按 Run 身份合并。

## ADR-024：v0.2 历史使用显式只读导入

**状态：Accepted**，修订 ADR-013 的迁移边界。

**背景：** v0.2 的项目内 `.nexus/nexus.db` 没有 Workspace、Turn 和 canonical Event 身份，直接原地升级既无法可靠选择 Workspace，也会让失败恢复和旧密钥处理变得含糊；完全拒绝又会让长期用户丢失产品内历史。

**决策：** 提供 `migrate-v02` 命令，由用户指定旧库和目标 Workspace。Importer 用 SQLite 只读连接验证旧 schema 和 JSON，预检所有身份引用与冲突，再用目标库单事务创建 Workspace 绑定的 Session、Turn、Run 和事件。旧 messages 作为完整 `message.imported` 历史；兼容工具事件映射到 canonical 名称；终态从旧 Run 记录合成，未结束 Run 作为 interrupted。源文件永不修改。

**影响：** 迁移可审计、可重试且失败不留半成品，但不是静默自动发现。旧 Provider 的名称、URL、模型可在目标未配置时恢复；旧 API Key 密文不复制，因为它绑定旧 `secret.key`，用户需要重新配置密钥。已有同名 Session 或 Run 时拒绝整批导入，不覆盖当前事实。

## ADR-025：Continue 在持久化执行身份后异步返回

**状态：Accepted**，修订 ADR-017 的 HTTP 等待契约。

**背景：** Continue 先前让单个 HTTP 请求一直等待模型和工具执行结束。反向代理超时、浏览器刷新或网络中断会让用户失去可靠反馈，虽然后端 Run 可能仍在继续。

**决策：** HTTP Continue 在 admission 阶段完成 parked 状态、Git checkpoint 和未知副作用校验，并先创建带 `continuation_of` 的 Run/Turn 身份；随后返回 `202 accepted` 和稳定 `run_id`，后台任务使用已创建身份执行。CLI 与 Python 调用仍可同步等待 RunResult。

**影响：** 收到 202 即保证 Run 可查询；若 Host 在后台任务开始前崩溃，启动恢复会把无终态 Run 标为 interrupted。Web 不再轮询一个未返回的 POST，而是立即订阅返回的 run_id。校验失败仍返回 409，且不会创建遮蔽原 parked Run 的新身份。

## ADR-026：Workspace 身份与 Session 工作目录分离

**状态：Accepted**，细化 ADR-014。

**背景：** Git 子目录注册时会规范化到仓库根，这对锁、历史和恢复证据是正确的，但也导致 monorepo 用户的 Shell、相对文件路径和模型提示始终从仓库根开始，无法把一段长期工作固定在子项目。

**决策：** Workspace 继续代表规范仓库根和并发/恢复边界；Session 另存相对 Workspace 的 `working_directory`，schema 升级到版本 3。创建 Session 时解析并验证该目录真实存在、位于 Workspace 内且没有通过符号链接逃逸。Shell 与内置文件工具以它作为 cwd 和文件边界；MCP 配置、根 AGENTS.md 与 Git checkpoint 仍属于 Workspace。

**影响：** Session cwd 创建后不可变，API 状态返回解析后的绝对路径。CLI `--cwd` 和 Session POST 暴露该能力；把 `--workspace` 指向 Git 子目录时，run/chat 自动沿用该子目录。旧 Session 和 v0.2 导入记录迁移为 `.`，行为保持不变。

## ADR-027：长任务只展示可证明的活动指标

**状态：Accepted**。

**背景：** “正在执行”不足以区分正常长任务与停滞，但 Agent 没有可信的总工作量，百分比进度会制造错误预期。

**决策：** 最新 Run 的轻量摘要从事件聚合模型 step、工具调用数和最后活动时间；Web 与当前 SSE 事件合并，实时展示已运行时长、已观察 step 和距最近持久事件的时间。停止仍按稳定 run_id 操作。不计算完成百分比，也不把模型文字当作进度事实。

**影响：** 用户刷新页面后仍能看到服务端活动时间，断线期间也不会把 UI 动画误当成后台进展。每秒更新只重绘当前状态文本，不增加网络轮询；权威状态仍由 Runtime Projection 和事件终态决定。

## ADR-028：审计导出始终使用 canonical Event

**状态：Accepted**，细化 ADR-013。

**背景：** RuntimeStore 已能写 JSONL，但用户只能通过 Python 内部对象调用；SSE 为旧客户端把工具事件改成 request/result，不能直接充当长期审计导出。

**决策：** HTTP 提供按 Run 的 NDJSON 流式下载，直接分页读取 canonical Runtime Event；CLI 提供等价文件导出，并默认拒绝覆盖已有目标。导出保留完整持久字段和 session_seq，不增加 SSE 的 `seq` 或事件别名。

**影响：** 大 Run 不需要一次性在 HTTP 内存中物化；导出仍受本地服务现有信任边界约束。JSONL 可能包含用户工作内容和工具结果，调用者负责保存位置和后续共享范围。

## ADR-029：Skill 按 Workspace 渐进式加载

**状态：Accepted**。

**后续修订：** [ADR-043](#adr-043统一生产入口并退役教学兼容层) 修订了本条的旧 Python 入口/兼容代码与执行边界；当前行为见 architecture.md。

**背景：** 仓库已有 `skills/*/SKILL.md` 和进程全局 legacy loader，但 v0.3 Runtime 只读取根 AGENTS.md。把所有 Skill 全文提前塞入 system prompt 会浪费上下文，也无法稳定表达每次 Run 实际看到的 Skill 版本。

**决策：** 每次 Run 从 Workspace 根目录发现并快照直接子目录中的 `SKILL.md`。system prompt 只暴露 Skill 名称和简介；存在有效 Skill 时动态注册只读 `load_skill`，由模型按需取得完整快照。目录和 manifest 解析不得逃出 Workspace。`context.configured` 记录 Skill 数量与包含内容摘要的目录 hash，加载操作继续使用 canonical tool 事件。

**影响：** 不新增数据库 schema 或进程全局 Skill 状态；不同 Workspace 的目录彼此隔离，Session working directory 不改变查找根。运行中对 Skill 的修改只影响下一次 Run。旧 `eventide.memory.skills` 保留兼容，但生产 Host 不导入它。

## ADR-030：大型工具结果使用无损日志和分页请求投影

**状态：Accepted**，扩展 ADR-018。

**后续修订：** [ADR-043](#adr-043统一生产入口并退役教学兼容层) 对 Shell 引入有界采集：执行器丢弃的中段不会进入日志，分页只能读取实际保留的内容。

**背景：** ADR-018 只在整个请求超预算后折叠较早结果，并固定保护最近三条。单次 Shell、MCP 或自定义工具可以返回接近完整 context limit 的正文；当它位于当前 turn 的受保护尾部时，摘要和旧结果折叠都无法避免 `context_overflow`。把完整正文反复发送给模型也会让大型任务快速消耗上下文。

**决策：** canonical `tool.completed` 继续保存执行器返回的完整正文。ContextBuilder 对超过 12,000 字符的结果始终生成有界头尾预览，预览携带 run_id/call_id；Runtime 提供只读、Session 隔离的 `read_tool_result`，按字符 offset/limit 从 Event Log 读取任意页，单页上限 12,000 字符。若请求仍超预算，再沿用 ADR-018 的较早结果折叠。预览与折叠都记录 `context.trimmed`，不写回事件正文。

**影响：** 最新的大结果不再单独占满请求，模型仍能按证据需要无损回读具体区间；审计导出、工作记录和默认 MessagesProjection 保持原文。分页身份必须同时匹配当前 Session、Run 和 call，不能跨 Session 探测结果。不引入外部 artifact 文件、数据库迁移或自动任务拆分；TodoWrite/Task System 属于后续调度层工作。

## ADR-031：Session 计划以事件快照驱动执行

**状态：Accepted**。

**后续修订：** [ADR-043](#adr-043统一生产入口并退役教学兼容层) 修订了本条的旧 Python 入口/兼容代码与执行边界；当前行为见 architecture.md。

**背景：** Runtime 已能压缩已结束 turn、分页大结果并在步数用尽后 Continue，但模型没有显式的中间任务状态。复杂请求只能从不断增长的对话重新推断剩余工作，容易漏步骤、重复探索或试图在一个 run 内完成全部内容。仓库中的旧 `eventide.tasks` 使用进程全局文件目录和 task graph，不满足 Workspace/Session 绑定及 canonical Event Log 约束。

**决策：** 生产 Host 注入 `todo_write`，用于原子替换当前 Session 的完整计划。计划最多 20 项，状态限定为 pending、in_progress、completed、blocked，同时最多一个 in_progress。合法更新追加 `task.plan_updated`；最新事件是当前事实，Host 每个 step 只把未完成项及完成数量加入 system。ContextBuilder 将历史 `todo_write` 参数替换为空清单但保持 tool_use/tool_result 配对，完整参数仍留在模型响应事件、默认消息投影和审计导出。该工具只改变 Event Log，标记为 recovery-safe，不产生 Workspace checkpoint。

**影响：** 计划可跨模型 step、Host 重启和 Continue 恢复，Session API 状态可读取最新完整清单；重复更新不会线性挤占模型上下文。计划是执行提示而非调度器：不自动运行任务、不创建 Subagent/worktree、不表达依赖图，也不把勾选完成当作外部验证证据。旧文件式 task graph 继续留在 compatibility layer，不进入生产 Host。

## ADR-032：任务身份由计划事件承载并在写入时分配

**状态：Accepted**，细化 ADR-031。

**背景：** ADR-031 的待办项只有 `content` 和 `status`，身份实际上是那串文字本身：改一个字的措辞就等于删掉旧项、新增一项。任何跨事件引用（激活、完成、证据）都无处挂载；内容相同的两项还会被判为重复而拒绝。

**决策：** 每个计划项在写入时由 Runtime 分配稳定 `id`（`t1`、`t2`……），随 `task.plan_updated` payload 一并持久化。身份保留规则固定为：模型回传的 `id` 必须已存在于上一版计划，保留其身份（可同时改写 `content` 与 `status`）；省略 `id` 时按内容与尚未被认领的上一版项匹配并复用；两者都不成立才分配新 `id`。序号单调递增且**永不回收**，计数器随 payload 的 `next_task_seq` 落盘，因此删除一项不会让它的 `id` 之后指向另一个任务。未知 `id`、格式非法或同一份计划内重复使用 `id` 一律拒绝该次更新，返回一条可重试的工具错误。旧事件缺少 `id` 时只读兜底，**不回写日志**，由模型下一次提交计划时自然补齐。

**影响：** 任务可以被事件稳定引用，且身份不随措辞变化。`prompt()` 因此需要渲染 `id`（模型没见过就无法回传），这部分开销只在存在计划时出现。不新增事件类型、不新增状态表：身份仍然是 canonical 计划事件的一部分，投影层不做 fold，`store.task_plan()` 保持 O(1)。代价是模型不回传 `id` 时会退化为新建身份，用工具描述中的显式要求缓解。

## ADR-033：活跃任务由计划事件派生，不新增生命周期事件

**状态：Accepted**，细化 ADR-031、ADR-032。

**背景：** ADR-032 给了任务稳定身份，但"当前在做哪一条"仍然只是 system 里一行 `[in_progress]` 文本，模型可以一项都不标，外部也无法查询。直觉做法是新增 `task.activated` / `task.completed` / `task.blocked` 事件，但那会让同一件事同时存在于计划快照和生命周期事件里：崩溃、并发或模型乱写时两者可能不一致，而 Runtime 若靠 diff 计划补发激活事件，模型只要写一个 `pending → completed` 的计划就能得到一条"没有激活记录的完成"。

**决策：** 不新增任何任务生命周期事件。`task.plan_updated` 保持唯一写入者，`TaskPlanProjection` 从计划快照折叠出任务列表与 `active_task_id`（最新计划里唯一的 `in_progress` 项）。该值通过 `session_status` 的 `active_task_id` / `task_state` 暴露，并写进续跑 run 的 `run.started.resumed_task_id`。计划里没有任何 `in_progress` 时，`prompt()` 追加一行提示让模型标出正在做的一项；不强制 `pending → in_progress → completed` 两段式，那样每个任务要多花一次工具调用，直接吃掉步数预算。

**影响：** 不存在"计划说一件事、生命周期事件说另一件事"的分歧，也没有伪造激活记录的入口。投影是可重建的只读状态，不落库；`store.task_events()` 只读计划与工具结果三类事件。代价是每次查询要扫描该 Session 的这三类事件，而上下文构建本来就每步全量读取日志，当前规模下可接受；若日后成为瓶颈，再补一个 `(session_id, type, session_seq)` 索引，不改变事实模型。

## ADR-034：单任务步数上限是 run 内软边界

**状态：Accepted**，扩展 ADR-019。

**背景：** 步数预算（`max_steps`，默认 30）是 run 级的。一条任务卡住会烧光整个 run 的预算，其他任务根本没有机会开始，用户只能重新描述目标。

**决策：** Host 在 run 循环内维护一个影子计数：跟踪当前 `active_task_id`，它变化时把起点重设为当前 step；同一条任务连续占用超过 `task_max_steps`（默认 12，`EVENTIDE_TASK_MAX_STEPS` 可调，`0` 关闭）个 step 时，按 ADR-019 的既有路径停驻——先补 `workspace.checkpoint`，再写 `run.interrupted`，并把 `reason` 记为 `task_step_budget`，错误信息点名任务 id。该计数不落盘、不进事件 payload，因此 Continue 后自然归零。

**影响：** 一条卡住的任务不再独占整个 run，用户 Continue 一次就能给它一段新预算，也可以把 `in_progress` 移到别的任务自助继续。`max_steps` 仍是硬上限，Host 不会因为单任务超时把 run 变成无限长。**Runtime 不代写 `blocked`**：`task.plan_updated` 的 `author="model"` 是日志诚实性的基础，由 Runtime 改写计划状态会让读者分不清"模型说自己卡住了"还是"系统判定它卡住了"。代价是一个持续卡住的任务在多次 Continue 下仍可续命，但每次都需要用户主动点一下。

## ADR-035：完成摘要是模型的声明，证据由 Runtime 从工具事件派生

**状态：Accepted**，细化 ADR-032、ADR-033。

**背景：** ADR-031 的计划把已完成项从模型视野里整个删掉，只留一个计数：模型干完一件事之后看不到自己干过什么，外部也无法回答"这条任务凭什么算完成"。同时，如果让模型自己提供证据引用，它可以编造一个不存在的 `call_id`，而 Runtime 需要逐条回查校验并处理失败重试。

**决策：** 把"声明"和"证据"分开，两者都留在同一个计划事件里但来源不同。模型在把某项标为 `completed` 时**必须**给出 `summary`（≤300 字符，说明做了什么，不是判断对不对）；重复提交计划时可省略，Runtime 沿用已保存的那条，因此计划快照不会因为反复重述摘要而膨胀。证据则由 `TaskPlanProjection` 从该任务首次出现到结算之间的 `tool.completed` / `tool.abandoned` 事件派生，条目就是事件本身（`run_id`、`call_id`、工具名、`is_error`、事件 id），上限 8 条并报告省略数量。`prompt()` 只注入最新一条完成摘要（截断 120 字符），因此计划再长这块也是常数。

**影响：** 证据不可能引用不存在或跨 Session 的调用，因为它不是被"提供"的，而是被投影出来的。**失败的调用同样计入证据** —— 证据回答的是"发生过什么"，而不是"是否成功"；`completed` 始终只是调度状态，任何文档、工具描述和界面措辞都不得把它读作验证通过。工具描述里明确要求"总结做了什么，而不是对不对"，并由测试守住不得出现 verified/passed/tested 这类措辞。代价是模型忘记写摘要时会多一次工具往返，换来的是一条可追溯且无法伪造的完成记录。

## ADR-036：崩溃缺口用文件系统证据补 checkpoint

**状态：Accepted**，扩展 ADR-015、ADR-019。

**背景：** 崩溃可以让 Host 死在「副作用工具已提交 `tool.completed`、它的 `workspace.checkpoint` 还没写」之间。历史里于是留下一个已完成的写工具却没有任何更晚的 checkpoint，Continue 的第 4 条检查永久拒绝这个 Session，用户只能放弃恢复。但重启时 Host 是活着的，能重新测量工作区——缺的只是「现在测到的状态」能否替代「崩溃那一刻该拍的那张照片」。

**决策：** 启动恢复时，对每个缺少终态的 run，用文件系统本身当证据：取该 run 最后一条持久事实的时间戳，比较 Git 可见路径（已跟踪加未忽略的未跟踪）的 mtime，并检查已跟踪文件是否缺失。全部严格早于该时刻且没有缺失时，认定当前工作区就是 Host 死亡时的状态，先补记 `workspace.checkpoint` 再写 `run.interrupted`：Session 停驻，但 Continue 的第 4 条检查自然满足。查到改动时不补 checkpoint，把改动路径写进终态 payload 的 `gap_files`（最多 20 条），保留原有拒绝语义，同时让用户看到该核对哪些文件。

**影响：** 比较严格使用 `>` 而不减任何容差：丢失那张 checkpoint 涉及的文件正是最后一条事实前几微秒写下的，任何容差都会把它们误判成改动，使检查永远无法通过；代价是与崩溃落在同一时钟刻内的改动不可见。判定是文件系统层面的证据，不是内容一致的证明：同一刻内的改动、被刻意保留时间戳的改动、以及缺口期内「创建后又删除的未跟踪文件」都不会被发现，所以 `gap_files` 非空时仍然拒绝——不新增「用户确认即放行」的通道。`changed_since` 无法作答（非 Git、不可读、文件数超过扫描上限）时按「没有证据」处理，行为与本次修改前完全一致。该时刻**之前**发生的第三方改动不需要单独识别：丢失的那张 checkpoint 同样会在那一刻之后拍摄，两者包含的信息相同。

## ADR-037：任务级评测只以沙箱文件系统与 Git 证据判分

**状态：Accepted**，扩展 ADR-008。

**背景：** smoke 套件只验证机制（工具被调用、权限被拒绝），回答不了"任务做完了吗"。若用模型自述或模型评审来判断完成，评测会重新引入 ADR-008 想避开的随机性与漂移；"模型自称完成"也与 ADR-035 的证据诚实性原则冲突——完成声明只是调度状态，不构成验证。

**决策：** 任务级套件（`evals/tasks.yaml`）的每条用例在一次性 Git 沙箱中运行：fixture 预先写入并初始提交，Runtime 以沙箱为 Workspace、沙箱内为独立状态根与评测数据库。判分只读文件系统与 Git：文件存在、缺席、内容子串，以及"初始提交之外有新提交且工作区干净"；判分器不调用模型，模型输出只参与辅助的 `final_contains` 检查。离线 scripted 结果在报告与 CLI 中明确标注 regression smoke，不得外推为真实模型任务完成率。

**影响：** 评测可复现、可在 CI 无密钥运行；沙箱保留在临时目录便于排查失败。代价是判分只覆盖"文件与 Git 可见"的任务形态，bash 用例依赖 PATH 中的 git/python，成为套件的可移植性边界（缺 bash 的环境跳过对应测试）；live 模式复用同一判分器并汇总 token 用量，其完成率反映真实模型，但成本与随机性由调用方自行控制。

## ADR-038：Web 工作台三栏重构、运行模式与能力自省

**状态：Accepted**，细化 ADR-016、ADR-017、ADR-027 的界面表达。

**背景：** 旧工作记录页把任务计划放在正文、详情塞进临时弹窗（Inspector），模型配置入口藏在右上角；用户看不到工具耗时、token 用量和上下文占用，也无法表达"这次运行只读"或"这次运行不必等我审批"这类每次 Run 的意图。同时顶栏没有任何地方能回答"这个工作区有哪些 Skill 和 MCP 服务"。

**决策：** 界面改为三栏工作台：左侧导航（Workspace 切换、按时间分组的 Session 列表、主题切换、可折叠），中央正文（状态 pill、右对齐的用户原文气泡、按章执行统计行、Run 谱系徽标、内联注意块），右侧常驻面板固定四个分区——任务计划（进度与修订历史、证据联动工具明细）、工具活动（统计与按章耗时明细）、详情（运行时间线、事件过滤、审计导出）、用量（tokens、步数、上下文占用）；Inspector 弹窗取消，详情统一进右栏。composer 成为交互中枢：Enter 发送、附件 chips、模式选择、上下文指示器与模型配置入口集中在输入区。新增 `plan`/`agent` 每次 Run 的模式：plan 把工具目录收敛为只读白名单并同步裁剪 executor handlers，agent 在该 Run 内自动允许 ASK 并照常写审批审计；策略在 `run_policy.py` 保持纯函数。能力自省提供只读端点，纯文件解析（SkillCatalog + MCP 配置），不连接服务、不回传密钥/command/env。附件是 Session 级 UTF-8 文本，以 JSON base64 上传、内联进用户消息、存放在状态根 `attachments/{session_id}/`。验收扩展为三层：Python/前端离线单元、浏览器离线验收、真实 FastAPI 加脚本假 Provider 的 E2E 用户旅程。

**影响：** 模式与附件不绕过 ToolExecutor/PolicyEngine：plan 只是改变目录可见性与 handler 映射，agent 的自动批准仍以 `approval.resolved`（`auto: true`，`author="runtime"`）落事件，事件日志继续只追加。能力端点、运行设置和 Run 摘要 `usage` 都是既有事实的字段透出，不新增事实源，也不返回任何密钥载体；MCP 在 plan 模式下仍会连接（工具不可见），这一代价被明确写进文档而不是假装断连。附件不含二进制与图片，多模态留给后续契约预留；截断、大小上限和归属校验都在 202 前或内联时显式标注，不静默丢弃。右栏投影全部可由事件重建，不落库；界面性能靠每秒轻量 tick 与按版本缓存的章节投影维持，不引入前端框架。

## ADR-039：对话式结果流与按需胶囊工作面板

**状态：Accepted**，替代 ADR-038 中「右侧常驻面板固定四个分区」的布局决策，并修订 ADR-017 中「先表达目标、当前状态、最近成果」的排序。

**背景：** 三栏工作台运行一段时间后的真实使用反馈集中在展示层：最新回复置顶在“已有成果”区，对话上下文断裂，用户经常找不回自己刚才的提问；右侧常驻面板长期挤压正文宽度；工具明细的视觉权重压过对话内容本身。此外旧面板的关闭按钮存在状态属性写入缺陷——`toggleAttribute` 写入空值属性、CSS 选择器匹配不上，导致点 ✕ 实际无效。

**决策：** 取消右侧常驻面板，改为顶栏任务计划、工具、详情、用量四个胶囊按钮，各自弹出一张悬浮详情卡：约 400px 宽、自带滚动、位于顶栏下方右侧，同时只开一张；再点同一胶囊、点卡片外、按 Esc 或点卡片右上角 ✕ 都会关闭，窄屏时变为近全屏。四个分区的内部结构与只读投影逻辑不变，只是承载容器从常驻栏改为按需浮层。正文改为对话式时间流：删除顶部“已有成果”区，每轮的最终回复作为回复卡内联在该轮末尾（含最新一轮），自上而下按时间排布；失败/中断且没有结果的 run 仍以 run 徽章行展示。composer 的模式选择改为下拉菜单，Auto/Plan/Agent 三个选项各带一行说明（Auto 是默认、风险操作弹审批；Plan 只读调研不执行写入；Agent 自动批准风险操作，带警示标注），发送改为圆形 ↑ 按钮、运行中原位变为 ■ 停止，模型入口收为小 chip，上下文占用改为小进度条并在超过 80% 时警示。

**影响：** 全部限定在展示层：投影、事件与 API 契约不变，`openToolAt`/`openToolsForChapter` 等联动语义保持，只是目标容器从右栏分区改为浮层；计划证据条目点击仍定位到工具浮层中的对应调用。「详情」浮层继续承担原 Inspector 职责（时间线、过滤、审计导出）。右侧面板的窄屏抽屉形态随浮层方案一并移除。

## ADR-040：附件二进制留在状态根，Run 只记录引用并按 Provider 水合

**状态：Accepted**，扩展 ADR-038 的附件与模型选择边界。

**背景：** 文本附件内联可以直接进入消息，但图片和 PDF 的 base64 通常远大于字符上下文预算；若把原始数据复制进 `message.user`，SQLite、JSONL 审计和每次 ContextBuilder 投影都会重复持有同一二进制。另一方面，模型列表端点已经存在，但 Host 默认模型仍是唯一可选项，用户无法针对单次工作选择另一个同 Provider 模型。

**决策：** 附件原始内容只存状态根的 Session 目录。文本继续按 512KB 上限内联；图片/PDF 的 canonical 用户消息仅保存不可变 attachment 引用，Host 在每次真实 Provider 请求前按 Session 身份重新读取并水合。图片映射到三种 Provider adapter，PDF 映射到 Anthropic Messages 与 OpenAI Responses；普通二进制可上传、列出和下载，但当前 Provider 没有协议映射时在 202 admission 前拒绝。附件一旦进入 Run 即标记 used，默认清单只恢复未使用 chips，DELETE 不允许破坏已被事件引用的内容。RunBody 另增可选 `model`，只影响本次 Run 的 ModelRequest、事件和 checkpoint identity，不改变 Host 配置或凭据。

**影响：** Event Log 和导出仍是完整的运行事实，但不再充当二进制对象存储；恢复和重放多模态上下文依赖状态根附件文件与事件引用同时存在，因此状态根必须整体备份。字符预算衡量引用和文本，而不是 base64 传输体；这避免把图片字节误报为文本 token，但不估算各 Provider 的视觉 token。已使用附件不能通过附件 API 单独删除，只能随状态根生命周期清理。切换单次模型不重建 Provider 客户端，也不允许跨 Provider。

## ADR-041：检索能力以只读工具渐进引入

当前修订：CLI部分由ADR-085退役；检索与治理扩展见ADR-069至075。下文的终端命令及后置范围仅为历史背景。

**状态：Accepted**。

**背景：** Agent 面对较大的 Workspace 时，完整文件读取会快速消耗上下文，而仅靠文件名和精确文本搜索又无法覆盖换一种说法的查询。直接内嵌通用 RAG 框架会引入重依赖、外部服务和第二套工具执行路径，模糊 Eventide 作为 Workspace Agent Runtime 的边界。

**决策：** 检索使用 Workspace 级、可重建的 `.eventide/index.sqlite`，并通过只读工具 `search_knowledge` 暴露给 Agent。索引只处理 Git 可见的 UTF-8 文本：结构感知切块保存文件、准确行号和标题路径；稠密路使用本地 HashEmbedder 或显式配置的 OpenAI-compatible embedding，稀疏路使用 SQLite FTS5 trigram，两路通过 RRF 按排名融合。向量保存为 SQLite float32 BLOB，Workspace 规模下查询时暴力余弦，暂不引入向量数据库或 ANN。embedding provider、model、base URL、维度和算法版本组成 fingerprint，变化时必须显式 `--force` 重建。默认 HashEmbedder 完全离线、确定性、零新增依赖，只保证机制和评测可复现，不宣称具有真实语义能力。Host 在每次 Run 按当前 Workspace 动态检查索引；索引不存在或不兼容时工具不进入目录。

**影响：** 检索调用继续经过 `ToolExecutor` 和 `PolicyEngine`，使用既有 `tool.prepared` / `tool.completed`，因此不新增事件类型、Runtime schema 或旁路审计。Plan 模式可以使用该工具，且只读调用不产生副作用 checkpoint。`eventide rag build|search|eval` 分别负责显式建索引、人工试检索和标注式 recall@5/MRR 评测；离线评测强制 HashEmbedder，不依赖网络或 API Key。索引构建耗时和文件变化不会被自动隐藏，调用方负责需要时重建。rerank、自动索引、PDF/Office ETL、Web 检索台和多租户留待后续决策。

## ADR-042：检索融合以单一只读快照为一致性边界

**状态：** Accepted

**背景：** 原子提交只保证单次 SQL 读取完整，不能保证多个独立连接看到同一代索引。重建复用 chunk ID 时，跨代 dense/sparse 候选可能被错误融合；读取损坏数据库也不应阻断无关 Run。

**决策：** 查询只使用 `mode=ro` 连接。embedding 请求在事务外完成，然后在一个 WAL 读事务内校验 fingerprint 并执行两路召回。构建持有写锁后重验 generation 和语料 manifest，再提交索引替换。读取不初始化 schema；Host 对损坏或不兼容的索引隐藏工具，不静默修复。离线评测覆盖 embedding 配置为 Hash，并将“调用发生”与“工具成功”分开判分。

**影响：** 不新增依赖、数据库 schema 或事件类型。查询期间旧 WAL 页面需要保留，但网络调用不延长读事务；文件系统不受 SQLite 锁保护，因此该机制不是文件系统原子快照。并发重建、损坏索引、扫描漂移和环境配置污染均由离线回归测试覆盖。

**备选方案：** LangChain/LlamaIndex 会带来与当前规模不相称的依赖和控制反转；外置向量数据库增加服务与运维成本；只做 dense 会削弱函数名、错误码和中文子串等精确匹配，只做 FTS 又无法扩展到语义 embedding；文件变更自动全量索引会把不可控耗时塞进 Runtime 主链。上述方案均不用于 v0.4。

## ADR-043：统一生产入口并退役教学兼容层

**状态：Accepted**，替代 ADR-011；修订 ADR-013/014 的 Python 门面保留、ADR-029 的旧 Skill loader 保留和 ADR-031 的无依赖边界。既有 HTTP/SSE 协议与 ADR-024 的只读历史库导入保持有效。

**背景：** 早期教学 Agent loop、context/hooks、全局任务/邮箱/定时器、同步 LLM、模拟 MCP 和工具注册表与生产 Host 并存。它们既不共享 Workspace/Session 身份，也不使用唯一 Event Log，部分 import 会创建目录；长期保留还要求覆盖率/type checker 排除大量模块。继续给旧接口套门面不能消除两套执行与状态。

**决策：** 生产执行和存储统一为 `RuntimeHost` / `RuntimeStore`，删除 `AgentRuntime` / `TraceStore` 别名及旧教学模块、专属测试，同时移除没有生产消费者的 fallback model 配置。已经迁移的 Skills、摘要、审批、恢复沿用当前实现；有价值的任务前置依赖迁入 `todo_write` 原子快照，以稳定 id 校验且不新增状态源；worktree 迁入注入式 Workspace 控制面，通过现有 Host 锁和 Event Log 管理；Shell 只保留注入式异步执行器，使用结构化退出状态与有界头尾采集。旧 task owner/mailbox/cron/无限自主协作不复制，未来受控 worker 必须有独立上下文、预算、取消、产物采纳和写入隔离。LangGraph 只能作为可选外层 adapter；沙箱走执行器边界，观测只派生已有事实。

**影响：** 旧 Python import 是明确的破坏性移除，Git 历史仍保存源码，不维护空壳或第二套状态。旧本地任务/worktree 文件不会自动迁移或删除；CLI worktree 必须从已有 Git ref 创建，状态在源项目外，拒绝丢弃修改或新增提交，遇到隐藏变动的索引标记也拒绝删除，并保留分支。计划依赖只约束模型的计划状态，不证明完成，也不自动调度。Shell 的省略部分会被明确丢弃，不能通过分页找回。覆盖率和类型检查不再排除 legacy/tools。后续功能以实际可靠性、隔离写入、独立验收和可复现评测决定优先级，不通过增加框架数量衡量成熟度。

## ADR-044：先持久接收，再执行；恢复意图与模型用量由同一 Host 管理

**状态：Accepted**，细化 ADR-019、ADR-038、ADR-040。

**背景：** 普通 HTTP Run 返回 202 时尚无运行身份或目标；Continue 在安全检查与后台执行之间也缺少所有权。立即停止、配置切换、重复身份和重启可能使目标或只读限制丢失。摘要直接调用 Provider，绕过 Host timeout/retry，未采用的摘要消费也可能遗漏。

**决策：** 接收在 Workspace 锁内预留精确 Run/Session 身份，线程校验完成后用单个 SQLite 事务提交 run.admitted、用户消息或恢复事实和 checkpoint，再同步登记可取消 Task，最后返回 202。配置切换拒绝接收和执行中的任务，Host 关闭等线程释放所有权后再关闭 Store。Continue 继承 Plan 及同 Provider/endpoint 的模型，Agent 自动审批仅原 Run 有效；恢复扫描 Session 未闭合历史，不重放副作用。附件新目录哈希完整 Session ID，内部保存精确归属，canonical 引用补足 used 缓存与数据库无法共同提交的窗口。

摘要走 Host completion callback，与主请求共用超时、有限重试和用量累计；成功响应在校验和采用前计入，summary 事件不成为聊天消息或主步骤。可选 Run Token 软上限在下一次请求前检查已观测总量，达到则 checkpoint 后停驻；默认关闭，Continue 重置。未知失败请求消费不伪造，单响应允许越界，不承诺硬费用上限。

**影响：** 202 是可查询、可停止的持久接收承诺，完成仍由终态与证据决定。进程 reservation 不构成第二事实源，不新增 schema 或依赖。安全旧附件目录只读兼容，不自动改名或清除；状态根备份必须包含附件。子 Agent 的共享预算和取消必须复用此所有者，不能另建无审计的执行链。

## ADR-045：只读 Worker 借用父所有者，执行复用主链

**状态：Accepted**，落实 ADR-043 的协作边界。

**决策：** 首版仅 Researcher/Reviewer，通过 opt-in delegate_readonly 工具进入既有 Executor/Policy。独立子 Session/Run 与源码快照隔离消息和文件根；父持有源 Workspace 锁，内部子执行复用 `_execute` 并登记 Host Task 所有权，不再次获取同一锁。不注册消失的快照 Workspace，不提供第二循环或邮箱。子身份持久化，禁止普通 Run/Continue/附件，默认归档以便审计而不挤占日常工作列表。

固定读取工具、禁止 MCP/custom/delegate、深度 1、并发 2、每父 Run 4 个任务，子步骤、Token 与时间均有界。成功结果先验证严格 JSON 和真实快照路径/行号才回传；合法引用不是独立验收。源码快照包含当前未提交源码，排除秘密、links和非文本，前后 Git checkpoint 检测漂移，不宣称文件系统原子性。父 usage 包含子消费，取消必须等子结束，未知消费不补造。

**影响：** 可在没有 Key/网络时用 scripted 回归验证隔离、预算、取消和报告拒绝；真实模型质量需另跑 live。写入 Worker 使用 worktree 与独立验收，不扩大此只读 profile。

## ADR-046：结构解析与外部观测采用显式可选扩展

**状态：Accepted**，扩展 ADR-041/042。

**决策：** tree-sitter作为code extra并由rag build/eval的code-structure参数开启，默认窗口与零parser依赖不变。结构块保留准确范围、符号路径与有界大小，解析失败明确fallback。切块配置和parser版本单独进入索引fingerprint，避免复用不同算法产生的旧块；写事务内复查环境，变化要求force。质量用相同语料与指标比较，现有套件成绩相同，不宣称召回改进。

观测只从唯一Event Log派生，默认离线JSON；显式OTLP HTTP参数才懒加载telemetry extra并发送。只导出固定枚举、哈希身份与数字事实，不导出模型/工具原文。局部provider避免改变应用全局观测；稳定历史身份支持重复导出，Continue和worker关联可追踪。跨Run用exclusive模型事件计算已观测消费，未知用量标明不完整。

**影响：** 不强制引入LangGraph、向量服务或外部Tracing平台；import/help/offline仍无Key和网络。增加parser版本维护和可选SDK回归成本，由lockfile、无extra路径与真实parser/内存exporter测试守住。

ADR-046补充（2026-10-16）：HTTP与历史页复用相同脱敏投影，新增显式JSON下载。关系收集按Run而非整Session遍历，最多64个相关Run，祖先/Continue只用于关系定位；不会为一次读取启动网络exporter。验证包含无关会话历史隔离、子任务/Continue关系、脱敏下载、迟到响应与真实API浏览器旅程。

## ADR-047：交付以不可变验收证据和显式采纳为边界

**状态：Accepted**，扩展 ADR-037、ADR-043/045/046。

**决策：** Writer运行于受管worktree，由Host的durable admission、同一Agent loop和Event Log执行，MCP关闭，backend选择随Workspace事实保留。交付控制面没有第二执行或存储系统：干净源基线先预检，固定测试/配置保护，临时Git index生成有界binary patch，独立检查绑定patch hash与源码checkpoint，显式apply前重读最新验收并重验双方源码。不自动commit、merge或覆盖用户已有修改；停止发生于apply生效后，也保存已发生事实。

容器是可选CommandExecutor，默认无新运行依赖、不拉镜像；Shell在本机daemon的有界非root容器内，验收挂载只读。无法确认cleanup不是普通失败，而是未知结果；不写tool.completed，不允许继续写入或采纳该Workspace。Readonly检查失败不会被当作通过。反复Stop不得取消仍在写的同步线程future并释放owner，真实完成结果先落事件。

工程评测采用三个带原版unittest的真实失败fixture；基线非零、测试清单完整和独立进程验收共同决定通过，模型正文不判分。原版测试另存并用隔离stdlib runner执行，消除候选测试修改与派生bytecode对验收的影响。离线脚本证明机制，真实效果必须显式live并报告样本与用量，三题不代表可靠成功率。

**影响：** 首版保护依赖/锁/CI等配置，涉及这些变更需人工处理；容器需要本机Docker、兼容Linux镜像和挂载权限，Git common dir不暴露进容器。未知写入状态保守保留，人工检查后从未受影响源建立新交付，不提供force放行。2026-10-16已在Apple Silicon Docker Desktop以真实容器验证只读边界、Skill脚本及停止/超时；发现自动删除与显式rm竞争时须在原清理期限内等待确认，不将removal in progress视为成功。脚本暂存副本设置跨UID读取权限，用户原文件不变。真实模型任务效果另由B/D评测，脚本化Provider只证明执行机制。

## ADR-048：Web 工作台升级按逐步证据交付

**状态：Accepted**。`AGENTS.md`全部必要编号步骤的实现、工程验证与有限模型实验已完成，验收证据见各步骤和最终汇总；这不代表模型效果成熟或已证实成本下降。

**目标：** Web 是唯一用户操作入口，Vue 3/TypeScript/Vite/Router/Pinia替换现有页面；后端仍为单进程FastAPI/asyncio/SQLite及唯一RuntimeHost/Event Log。同项目只读快照与独立worktree写任务可并发，源checkout及采纳保持独占。Skills支持本地包、用户/项目作用域及固定资源版本。模型参数、流式、准确usage及预算预留使用同一请求主链。

**约束：** 页面/API承接并验证后才能删除用户CLI和旧前端，自动化评测保留Python入口。MySQL/Redis/分布式worker、技能市场、框架重写后置。Token区分已报告、估算和未知；未知消费不冒充零，缓存/推理细项和父子汇总不双计，费用用版本化价格估算而非账单承诺。

**维护制度：** 每个编号步骤记录状态、实现、验证、文档与阻塞；代码、必要验证和Markdown同步齐备才算完成。状态入口为AGENTS.md，原始产物仍忽略；不另建进度/路线/面试文档。现有Accepted ADR在对应替代实现通过前继续约束生产行为。

## ADR-049：用量按请求尝试事实派生，并保留未知消费

**状态：Accepted（账本与适配器基础）**；U1 的生产请求接线和页面交付仍按 AGENTS.md 独立验收。

**决策：** 供应商 usage 经严格计数归一化，缺失保持未知，细项按各 API 语义处理。新数据以 model.attempt_started/finished 的稳定请求及唯一尝试身份为消费事实；失败和重试不冒充免费。纯账本逐尝试去重，Run 终态和父子汇总不能成为第二次消费。历史响应仅在没有新尝试事实时回退，并明确证据不完整。估算与 reported 分桶，不自动推算价格或未知消费。

**影响：** 现有 flat input/output 汇总暂保兼容，新账本可从事件重建并支持预算/费用后续接入；保留缓存与推理明细不会增加重复总量。公开 token 字段仅在严格非负整数时绕过敏感键脱敏，字符串或嵌套对象仍遮蔽。

## ADR-050：生成参数绑定模型能力并在接收时冻结

**状态：Accepted**，落实A4/A5，参数页面仍按F阶段迁移。

**决策：** ModelRequest与RunRequest增加generation；模型默认和Session覆盖通过generation.configured事件保存，模型身份包含Provider/endpoint/model。省略继承、null恢复默认、零是有效值，合并后在持久admission保存有效快照。未知服务不推测支持采样参数，需显式能力声明；已知DeepSeek的思考模式无效温度及top_p限制在接收前拒绝。声明不等于供应商探测验证。

**恢复：** Continue相同接入继承原模型及有效参数并重验，接入改变重新解析当前配置；在途运行不被后续设置改变。摘要与只读Worker采用独立辅助默认，参数不成为权限或Skill授权。每次真实请求尝试记录独立身份以支持未知用量和重试账本，原模型内容事实不替换。

**影响：** 三adapter只映射显式字段，默认请求保持原行为；严格范围校验可能拒绝供应商本会忽略的参数，这避免页面展示无效设置。最大输出、温度、推理强度不是任务预算，也不保证确定性或账单上限。

**2026-10-16 输出格式补充：** JSON输出采用显式response_format和端点能力声明，复用接收冻结及Chat SDK参数，不根据prompt正则猜测。最终响应的JSON语法校验发生在原model.response及usage保存之后，失败不重发、不自动修正、不剥围栏。事实评测按已声明支持的任务协议在规划、接收和实际调用中固定json_object，独立grader保持不变；修复与辅助调用不自动启用。格式正确不代表答案正确，历史失败不会重判，组合支持另做真实验证。

**同日两阶段修订：** 本机当前GLM配置两个JSON工具probe未调用文件工具，同任务text探测实际读取；证据只说明该配置的行为，不宣称供应商全局互斥。原生JSON限制只用于单一主循环中的一次无工具最终化，调查阶段保留工具且不下发response_format。草稿在原响应事实中标记，Continue沿自身链恢复，不建表或另起执行器；无论草稿格式如何都固定一次最终化，并占用原步骤和共享预算。最终化未知/非法不自动重发，幻觉工具仅abandoned配对；根generation保存最终契约，实际请求及attempt以phase和真实generation区分。此步骤不纠正事实或引用，独立grader和原预算不变。

## ADR-051：评测只读解析模型配置并隔离全部用例状态

**状态：Accepted**，修订 ADR-041/042/047 的评测实现细节。

**背景：** live eval 原先只读取环境设置，无法使用 Web 已加密保存的模型连接；无 fixture 的用例还会沿用启动目录与套件状态根，模型工具可能触及真实源码。为了获取有效配置而构造第二个 Host 又会争用状态根所有权，并可能触发中断恢复。

**决策：** 提取 Host/live 共用的 Provider 配置优先级函数，live 通过 `mode=ro` SQLite 事务读取已提交记录，在更换临时状态根前解密。解析不初始化数据库、不创建 Host、不恢复 Run；解密不生成替代密钥，不允许因解密失败使用环境密钥。离线评测不读取保存的凭据，并清除 API 与 embedding 凭据。

全部用例各自使用临时 Git 项目和状态根；无 workspace 时创建空项目，离线/live 均关闭 MCP。报告与逐用例源码、数据库保留在同一临时产物根，失败不删除。显式 output_dir 仅导出报告；凭据不写报告或临时数据库。此处 Git 沙箱表示临时工作副本，不是操作系统或网络隔离；本机 Shell 仍使用本机权限。

**验证：** 离线回归覆盖活跃 WAL 读取、不获取 Host 所有权/不恢复运行、缺失/错误/不可读密钥、旧库/损坏库、离线路径不读取真实配置、失败产物保留、MCP 默认关闭及用户源码/数据库不变。真实模型兼容性属于 0.4，不能由 scripted 测试替代。

## ADR-052：工具契约验证先于权限与执行

**状态：Accepted**。A3已接入生产目录与一次参数修正边界；对应测试证据见AGENTS.md。

**背景：** 手写目录 schema 与 Python handler 可逐渐分歧，依赖 handler 报错难以区分模型输入问题和真实执行失败；MCP 模型参数还不能有机会覆盖 transport 的绑定值。

**决策：** 内置参数统一由严格 Pydantic 模型声明并生成 schema，动态工具通过 jsonschema 验证，引用只在当前文档内解析。执行器冻结目录，在 PolicyEngine 和 handler 前拒绝无效输入，省略字段不自动填充、权限不因通过验证而扩大。坏 schema 逐工具拒绝。MCP transport 使用词法闭包，运行时状态不通过可由模型覆盖的默认关键字参数传递。

**影响：** 自定义生产 handler 必须注册显式工具 schema；参数错误、无效契约、权限、执行失败、超时和未知结果有稳定错误类别。执行后的副作用事实仍走原事件链，不能因校验或修正自动重试写入。新增直接依赖 Pydantic、jsonschema 和 referencing；默认导入与离线测试不访问网络。独立文件路径检查、审批默认拒绝与未知结果隔离继续有效。

## ADR-053：流式适配器只在终态交付完整工具调用

**状态：Accepted**。Provider协议、Host预览/SSE和跨片脱敏已通过A6/A7集成验收；Vue页面按F阶段继续。

**决策：** 保留 complete，三类官方 SDK adapter 增加 stream。文本增量只供临时展示，完整 ModelResponse 是工具调用及 usage 的唯一出口；Chat 工具按 index 收齐 JSON，Anthropic 检查内容块生命周期，Responses 使用官方终态对象。私有推理作为专属 Provider 状态回传，不混入可见文本或其他 Provider。最终对象之前的断流、缺终态或超限不能产生可执行响应。

**资源与重试：** SDK 重试为零，请求创建后出错不回退重发。输出内容、参数与推理组装限制 8 MiB，输出项最多 128 个，原始事件最多 65,536 个。连接在完整响应返回前关闭，取消、异常和消费者关闭同样清理。限制是应用层响应组装约束，不是 SDK 接收帧硬上限、费用上限或安全沙箱。

**截断：** Responses 的 incomplete 终态保留原始原因，不能因存在调用改写为 tool_use。completed 响应含未完成工具项、身份缺失或无效 JSON 时标为 incomplete，最终 usage 保留供先记账后停止；模型生成的完整 JSON 不等于供应商已完成该工具调用。

**验证：** 48 项流式协议测试覆盖部分 JSON、截断状态与工具完成状态组合、私有状态回传、缺失终态、未知 usage、取消、断流与资源边界；三类真实 SDK 使用内存 SSE transport 解码，未访问网络和付费模型。配合既有 adapter、generation 和 usage 测试共 94 项通过，Ruff 和 mypy 通过。

## ADR-054：Skill 内容版本与启用授权分离，运行读取冻结资源

**状态：Accepted（包、Host 主链与 Web 管理）**；真实 Docker 和冻结任务集效果按 S/B/R 单独验收。

**决策：** Skill 包采用严格元数据、明确作用域、目录／ZIP 的先检查后物化和内容寻址版本。用户包版本为派生的不可变内容产物，启用与选择必须来自唯一 Event Log，不通过目录或平行 JSON 推断。导入绝不自动安装依赖或执行脚本。项目包继续由项目和 Git 管理，不替用户重写旧格式或回滚文件。

**资源：** Run 的目录对象冻结正文及全部有界资源，引用按需分页，脚本供执行器读取固定字节。更新只影响后续快照。服务级版本检查重新校验内容摘要，防止磁盘篡改把同一版本变成不同脚本。版本目录的只读权限不等于恶意代码隔离；脚本必须经既有 Executor、Policy 和所选命令后端，Skill 不获得额外授权。

**取舍：** 同名不静默覆盖，错误和缺依赖可诊断；限制包数量和字节范围，避免渐进正文加载却在运行准备阶段无限读取资源。首版仅提供本地包，Git 来源与市场后置；内置三个包也不自动启用。旧畸形 frontmatter 不再降级成可执行的有效 Skill，这是明确的严格化，需要用户根据诊断修复。

## ADR-056：HTTP 领域路由共享单应用上下文与错误契约

**状态：Accepted（现有 API 模块化）**。A1 的新长任务控制面仍按 AGENTS.md 后续验收。

**决策：** 保留 api.create_app 作为组装入口，将现有路由与请求模型移入 http_api 领域模块。router factory 显式接收应用独立的 ApiContext，不使用全局任务、审批或 reservation，不创建第二个 Host、Store 或调度器。接收仍经过现有 durable admission、幂等身份和来源守卫；SSE 与导出只展示既有运行事实。

**错误：** 增加统一 code/message，同时保留原状态码、detail 类型和 HTTP 协议响应头。校验 detail 不回显原始 input/ctx，防止候选凭据或上传正文泄漏；内部异常返回通用错误。模型探测的 HTTP 200 业务失败不改变，已开始的 SSE 不尝试转换成错误 JSON。OpenAPI 描述共同 ErrorResponse，后续 Vue 类型生成使用同一契约。

**验证与边界：** 38 条现有路由清单无缺失或增加；94 项 API/HTTP/接收/流式/生成参数/用量/工作区回归通过，含凭据不回显、405头保留、来源拒绝和两个应用运行/配置相互独立。Ruff 与 Mypy 67 模块通过。Skills、索引、评测和交付的持久任务接口另行实施，不用路由拆分替代功能交付。


## ADR-055：Vue 工作台使用后端权威状态和可丢失预览

**状态：Accepted（前端基础与现有核心交互）**。F2/F4/F5 完整迁移仍按 AGENTS.md 验收；旧前端的既有 ADR 在退役前继续有效。

**决策：** Vue 3/TypeScript/Vite/Router/Pinia 提供页面主入口，FastAPI/OpenAPI 是 HTTP 契约。类型生成只创建临时 Host，不接触真实状态。Pinia 和标签页存储仅保存 UI 选择、草稿、待确认请求身份和可重建缓存；执行、审批、恢复和请求账本以服务端为准。流式预览独立于持久 SSE 序号，可丢失、可清除、不进入历史消息。旧前端到 X 才删除。

**请求边界：** 运行提交及 Continue 保留稳定幂等身份；网络结果不明不自动换键重发。项目/会话切换只关闭观察连接，不终止运行。模型密钥不进入浏览器存储，模型目录联网探测由用户显式触发。原始 HTML 与任意协议链接不执行。开发代理保留 Host 配合同源校验，生产不要求用户安装 Node。

**影响：** 新增固定 npm lockfile、组件和真实 API 浏览器测试；管理控制面未实现时明确显示边界，不用假进度充当功能。服务端字典响应在生成更明确模型前由小型领域接口描述；调用账本未知值不能转成零费用。静态产物挂载、完整 Skills/检索/评测/交付操作及 CLI 退役另行验收。


## ADR-057：Skills 管理采用内容复核、配置冲突检测与持久写回执

**状态：Accepted**。S1/S2/S3 的管理闭环已实现；脚本Docker及真实任务效果不由页面测试替代。

**决策：** 导入界面先展示包诊断与资源清单，再提交预检内容摘要；服务重新读取后比对并只使用该次固定字节。安装、项目启用、主动选择与实际加载分别记录，用户版本回退只变更后续Run配置。配置写入可绑定当前事实摘要，过时页面409，不静默覆盖。损坏启用版本在管理页面可诊断和禁用，执行仍拒绝。

本机有界导入/配置使用Event Log的started/completed/failed回执；结果未知不自动重放。客户端只保留请求指纹与身份，成功重试返回原结果。内容文件与SQLite没有共同事务，崩溃遗留不被宣称为原子成功；后续长任务仍需持久任务控制面，不能用同步回执代替队列。

**验证：** 本地目录/ZIP、幂等重启、内容变化、配置竞争、损坏恢复、作用域与加载事实均有离线测试；Vue真实API浏览器旅程覆盖导入、显式启用选择、资源读取与刷新后的加载记录。默认不联网、不执行安装脚本、不启用额外依赖。


## ADR-058：Vue 构建资源随 Python 包分发，源码与产物共同验收

**状态：Accepted（本地服务、wheel及Docker路径）**。

**决策：** 工作台暂挂载/workbench/，Hash路由和相对静态路径兼容开发及同源部署。生成资源保存在专用eventide/workbench目录，随package-data分发；最终使用者无需Node。Node只用于维护构建和Docker前端阶段。旧根页面继续到X阶段完整迁移后删除。

**取舍与验证：** 提交编译产物增加版本库体积及重建维护成本，换取源码安装和wheel的完整资源；SHA-256清单与源码指纹、CI重建差异检查防止维护者只改源码漏构建。浏览器旅程直接访问FastAPI静态产物，wheel另在临时目录隔离导入，不能仅凭Vite开发页可用声称分发成功。缺资源返回明确503/404，API保持可用。2026-10-16已在Apple Silicon Docker Desktop实测构建及Compose启动；临时卷覆盖静态资源、非root、工作目录、同源控制面和重建恢复，运行阶段无需Node。CI复用tests/container_smoke.py，不依赖真实密钥。


## ADR-059：预算与费用由追加事实派生，预留在 SQLite 事务中竞争

**状态：Accepted（账本底座）**；生产请求接线见ADR-062，页面及并发调度继续按P/U2/U3验收。

**决策：** 预算变更、预留、结算、人工未知处理及价格快照复用唯一Event Log。BEGIN IMMEDIATE内完成读状态、判额度与追加，跨连接竞争不能分别消费同一份余额。按追加顺序重建，系统时钟只用于日期归属和报告；未知消费保留预留且用户处理留痕。子Run使用真实根关联，各作用域独立限制，但消费尝试只记一次。

费用按供应商规范化后的输入/输出总量划分有单独价格的细项，以Decimal与固定价格版本估算；无价格/无用量不补零，estimated来源单列，币种不换算。原报告不可变，重估是显式新事实。暂不引入Redis、收费价格探测或自动模型路由。

**边界：** 全历史折叠保证当前单机正确性，投影缓存可在测量后优化且必须可重建。本阶段测试组件原子性与账本口径，不代表模型请求已受其限制；接线前仍保留现有软限，并在后续测试覆盖发出请求、取消、重试与重启的全部分支。


## ADR-060：Project 聚合本地执行副本，Session 绑定保持稳定

**状态：Accepted**。落实 P1，调度与隔离并发由 P2/P3 单独验收。

**决策：** Project 表示本地源码项目，Workspace表示具体checkout。RuntimeStore新增schema v4的项目与关联身份表，在同一事务内按Git common directory关联源与worktree；不同clone不因remote相同而合并。关联创建后不可修改，Session继续固定Workspace/cwd，不通过重绑定偷换执行位置。受管worktree创建自动登记，交付Writer自动共享源Project，只读worker继续使用源身份加snapshot_id。

**迁移与事实：** 保留全部既有Workspace/Session/Run身份和事件字节，只补齐项目关系；不可验证的历史目录独立保留，不延后静默重分配。身份表不存运行状态或预算，运行事实仍在唯一Event Log。新接收/开始事件冻结Project与源Workspace；旧事件不虚构历史归属。

**影响：** 新增只读项目聚合API，原Workspace接口保持，源有副本或副本有Session时不能删除。提供后续共享预算与并发的可信归属，不提前放开Workspace锁或修改权限边界。真实Git主目录/先注册worktree、独立clone、跨连接注册、历史迁移、重启Continue、交付/worker及HTTP隔离均有离线回归。


## ADR-061：容器本机管理使用显式连接来源与同源约束

**状态：Accepted（协议及真实Docker部署验证）**。补充ADR-010的本机传输边界，保持ADR-012的单机部署范围。

**背景：** 绑定宿主回环端口的Docker转发，在容器内呈现为非回环socket peer。直接拒绝会使Web项目、模型及Skills管理不可用；另启CLI Host则与服务的状态根锁冲突。

**决策：** 新增仅启动时设置的 `EVENTIDE_LOCAL_PROXY_PEERS`，默认空，只接受显式精确IP，不支持网段、通配符、主机名或自动发现。配置在应用创建时固定。额外peer必须同时提供回环Host和完全匹配的Origin；缺失Origin、cross-site和DNS-rebinding地址仍拒绝。既有回环客户端保留无Origin的本地调用契约。服务入口禁用代理头解析，不从X-Forwarded-For等外部输入获得本机身份。

**边界与验证：** 发布端口必须维持宿主回环；将此服务暴露给外部网络需要另行设计鉴权。精确peer、未配置拒绝、错误Origin/Host、伪造转发头、启动配置格式及冻结均有回归；原同源与幂等测试保留。2026-10-16实际Docker Desktop端口转发和Compose网络分别验证默认拒绝、精确peer同源管理和错误Origin拒绝；两种网络来源不同，印证不能内置固定网关或自动信任整个网段。


## ADR-062：模型派发与预算事实原子关联

**状态：Accepted（Run请求派发接线）**，扩展ADR-059；完整预算页面与调度仍按U2/P验收。

**决策：** 所有Run主调用、摘要和只读worker在RuntimeHost唯一请求入口逐attempt竞争原子预留。输入使用有名称的序列化UTF-8字节估算、输出预留完整上限，不把字符统计包装成供应商Token。预算归属取P1不可变项目、实际父Run和交付源事实；Continue的新Run不能改变交付/项目归属。

**一致性：** 预留与model.attempt_started同事务，结算与model.attempt_finished同事务。完整供应商响应同步结算后才交付工具调用；失败、断流、取消和无usage继续占用预留。重试每次单独预留，预算不足不重试；进程重启不自动释放未知消费。费用快照单独追加，崩溃缺口明确为unpriced，不补零。摘要预算拒绝必须上抛给Host停驻。

**边界：** 输入估算可高估多模态数据，不能保证硬账单上限；不用缓存/父子汇总充当第二消费。配置暂由Host Python接口提供，HTTP/UI、P调度和API embedding仍后续，连接探测后由ADR-067纳入Run与UTC日预算。既有已观测Token软限继续独立生效，不悄悄改变环境变量零值语义。

**验证：** 21项新增生产派发用例覆盖预派发拒绝、原子回滚、失败重试、摘要拒绝/未采用、worker父子、交付Continue、并发额度竞争、取消/断流/超时/缺usage、重启及实际超额；102项定向回归和完整868通过/1跳过（93.61%覆盖）、Ruff/Mypy74模块、smoke12/12通过，无付费模型调用。


## ADR-063：Run 固定执行副本，写入 Session 复用其首个 worktree

**状态：Accepted（P2 执行隔离）**。持久队列和全局容量仍由 P3 实施，交付有界修复属于 D。

**决策：** Session保持原Workspace/cwd绑定，Run显式选择source、snapshot或worktree。snapshot仅Plan，在源Workspace短期独占下复制当前Git可见源码并冻结Skills；持久产物及完整manifest绑定admission。worktree首次从干净HEAD创建，后续新Run和Continue复用该Session首次副本，保留未提交修改，不能切回source/snapshot。需要新副本则建立新Session。实际执行Workspace关联同一Project，模型、工具、Skills、检索和worker都使用实际文件根。

**所有权：** Session始终串行；快照使用精确Run所有权，写副本使用实际Workspace所有权。项目Git锁只覆盖共享元数据操作及Shell/Skill命令，模型等待不持有；后端能力显式委派，包装器保持原本地argv和容器资源权限。Delivery使用每交付owner和独立管理Session；准备/比对/采纳短期获取源和Git，writer及检查阶段不整轮占源。采纳重查源码与当前验收，取消先完成副作用收尾。

**恢复与边界：** Continue沿用绑定，源漂移不重定义已接收任务；副本丢失/篡改/未知结果明确拒绝。重启仅停驻不重放，abandon不能更换副本或绕过未知隔离。运行引用阻止副本删除。固定快照限制2,000候选/10MiB文本/单文件1MiB，排除敏感路径/链接/二进制；快照无Shell/MCP/源索引/嵌套worker，worktree暂不连接MCP。worktree和只读位不是OS沙箱，外部用户与本机Shell仍可改变共享环境。

**取舍与验证：** 显式字段保持直接源Python/CLI兼容；副本增加存储量，当前保留恢复，不自动清理失败准备留下的受管记录。定向覆盖真实并发、同Session串行、稳定脏副本/Continue/重启、精确停止、源与副本漂移、未知结果、worker读取副本和共享预算身份、命令协调、交付验收/采纳与取消；Git项目的本地argv及真实Docker Skill通过生产主链验证。此设计复用同一个Runtime/Store，不新建执行事实源。

Vue补充验收：39项前端回归、5条真实FastAPI静态页面旅程、typecheck/build/格式与wheel检查通过；真实文件确认两个副本互不影响、源未修改、刷新及后续消息复用原副本。执行位置监听不因轮询重置，切换新Session恢复Auto。集成136项后端定向回归和6项真实Docker、Ruff/Mypy79模块通过。

## ADR-064：预算控制面复用唯一账本，历史查询保留未知

**状态：Accepted（HTTP与Vue预算控制面）**，扩展ADR-059/062；完整U3输入组成、交付页面和并发调度继续按U2/U3/P验收。

**决策：** 新预算路由复用RuntimeHost/BudgetLedger/CostLedger，查询helper只折叠既有事件，不另建账本。所有控制写请求要求本机同源和持久幂等键；幂等所有者沿用首个预算控制Session，启动Workspace变化不能重做旧请求。配置可带预期hash防旧标签覆盖，hash只包含最新配置事实，消费变化不影响配置竞争。

**查询口径：** 费用按真实预留的Project/Delivery/Run与UTC日期筛选，父子只统计独立尝试，Session覆盖全部历史。用量先完成attempt与legacy去重，再按身份和日期筛选，防止跨午夜重复回退。历史无预留调用不虚构费用，明确增加未知数量。多个币种及estimated来源分别汇总，价格新增不会自动重估；不可变历史和最新尝试快照一起返回。

**人工处理：** 仅在尝试结束或Run终态后允许处理。用户选择record_known时必须给总量与理由，不能低于已报告部分；选择keep_unknown只追加审计并保持额度。补记总量不改模型输入/输出事实，也不改旧费用快照；保守未知不是默认零消费。结果未知的HTTP mutation不会自动重试执行。

**拒绝后的重试：** 确定未生效的预算拒绝显式记录outcome=rejected，首次/重放响应携带X-Eventide-Mutation-Outcome: rejected，允许页面在条件变化后使用新键。原键仍保持原结果；默认失败、未知started和同键异内容不加标记，不能把所有409或存储错误当作无副作用。

**验证：** 41项新增HTTP专项覆盖严格参数、来源守卫、持久幂等/未知结果、重启改变默认Workspace、旧配置竞争、价格固定版本、父子/Continue、真实Git副本项目聚合、UTC日、105个历史Run、旧新调用去重及未知人工处理；完整909通过/1跳过（93.70%覆盖），Ruff/Mypy76模块和离线smoke12/12通过。未调用付费模型；Vue由后续集成独立验收。

拒绝标记补充验证：74项预算/HTTP/Skills回归通过，包含活跃请求拒绝后终态新键处理、原键及重启失败重放保留标记、默认失败/未知started/内容冲突/OSError无标记；Ruff/Mypy76模块通过。

**Vue接入补充（ADR-064）：** 预算/费用页面复用上述查询，不建立浏览器消费累计器；按配置hash保存，轮询不覆盖未提交编辑。价格版本按内容确定身份，所有写请求共用持久幂等客户端，仅确定拒绝后允许新键。真实价格需要用户显式登记，不内置未经确认的收费表。

Vue集成验证：35项前端回归、typecheck/build/format与4条真实FastAPI静态页面旅程通过；预算零值阻止、刷新恢复、关闭限制后Continue、固定价格计价和快照并发均实际运行。集成后端965通过/5个默认Docker跳过、覆盖93.79%，Ruff/Mypy77模块通过。全部使用临时项目和确定性Provider，无付费调用。


**U3字符组成补充（ADR-064）：** 最终ModelRequest在统一预算begin入口按固定JSON序列化口径分项，只保存数字；Token账本和预留方法不变。Skills/检索依据当前请求唯一且先出现的tool_use与结果配对，摘要和缺失关联不补造来源。统计由attempt_started冻结，投影严格验证新结构、旧数据保持null，不能用费用除字符数冒充精确归因。178项组成/usage/预算/HTTP回归通过，统计模块100%覆盖、Ruff/Mypy82模块通过；Vue和完整集成门禁由后续步骤验收。

**U3页面验收补充：** 七类字符统计已在Vue逐请求展示；高消耗Run只按已报告输入/输出排序，父子分列、缺失保留，摘要频次和多次失败均来自独占尝试。48项前端回归、6条真实API静态旅程、typecheck/build/format通过；168项usage/预算/probe组合回归、Ruff/Mypy87和最新Docker分发验证通过。交付范围随D继续验收。

## ADR-067：连接探测使用真实控制 Run 与服务级预算归属

**状态：Accepted。**

**决策：** 沿用既有control Session/Run，而非引入operation_id及另一套账本。connection_probe是一次真实后台请求，不伪装用户Agent；没有用户消息、admission或工具，不占根Agent槽，仍共享模型额度。取得槽后原子预留，Provider单次请求采用冻结超时，失败及缺失usage保留未知。关闭客户端后才写Run终态，重启不重放、不读取或补造源码证据。

**归属：** 探测全局连接不属于任何源码项目，受真实Run及UTC日预算限制。只有事务内验证的独立probe控制身份可使用None项目；普通Run/worker/Delivery及不完整控制身份不能借此绕过项目桶。复用Run身份使未知核对、费用快照及重建保持原协议，不新建状态事实源。

**保密：** 候选Provider/model与默认连接分离，凭据只在内存，事实只保存脱敏元数据和数值。失败返回稳定分类，分类后的类型和文案也统一清洗（含自定义ProbeError），Python/HTTP均不暴露含候选密钥的SDK异常链。HTTP的30秒期限触发取消，仍等客户端收尾后才终态，因此不承诺严格总墙钟上限。

**幂等：** 连接测试虽不修改源码，仍会真实消费模型额度，因此HTTP必须先持久接收。复用mutation事实及控制Run，不新增operation事实源。HTTP200业务结果可回放，在途/未知409保留原键；Run终态存在时可恢复丢失回执但绝不重发，恢复结果不虚构原耗时。候选密钥仅参与内存摘要计算，页面只保存随机键/表单摘要。未显式密钥的同一请求回放首次结果，不能因默认配置改变而变成第二次调用。

**Vue入口：** 设置页显式测试未保存候选，不改变活动Provider和生成默认值。测试期间锁定候选表单，未知结果保留原身份，刷新重新输入密钥；调用账本与UTC每日预算说明同时展示。使用现有durableMutation，不增加第二套请求身份协议。测试服务将probe的Provider factory显式替换为本地fixture，仍执行真实控制Run、预算及usage主链；浏览器验证不使用真实密钥或外部模型。

**Vue验证：** 8项新增专项、本分支47项组件、typecheck/build/格式、2条真实API静态浏览器旅程和40项探测/分发后端回归通过。覆盖候选配置不保存、失败unknown、丢响应刷新同键、SPA切换在途结果及320px无横向溢出；同标签页只在内存共享探测状态，刷新仍由持久请求身份恢复。

**验证：** 23项核心离线专项覆盖准确归属/候选价格、日与Run预算拒绝、8类None项目伪造、取消/超时/缺usage、等待槽取消、重复停止收尾、Host关闭及重启、Python/HTTP脱敏和未知人工核对；相关既有160项回归通过。HTTP另有14项专项及159项关联回归，探测HTTP模块覆盖100%；旧页34项JS（新增5项）及真实Chrome丢响应/刷新/同键回放旅程通过，账本仅1次attempt，Ruff/Mypy83模块通过。全部使用假Provider，未调用真实API。

## ADR-065：调度从Run事实派生，根所有权与模型额度分开

**状态：Accepted（底座及Host/HTTP接线）。** 前端与分发完整集成仍按P3/F/R验收。

**底座决策：** 从既有admitted/started/终态事实按Store提交顺序构建队列，不按时钟排序、不另写状态源。只统计根Agent，持久parent_run_id子任务与没有admission的控制Run排除。4个根lease覆盖准备和实际清理，20个等待额度包含短期接收ticket；ticket不是202凭据，必须先完成持久接收。明确取消或拒绝不会制造Run，持久接收后不能直接丢弃ticket逃避容量。所有句柄按对象所有权核对，不能用相同ID的伪造句柄释放他人资源。

**模型并发：** 单独2个模型槽，限制每次尝试而非整轮Run。父等待worker或backoff期间不持模型槽；worker不另抢根槽。该组件不产生Provider调用或用量事实；接线必须在获得模型槽后才做U2预留，等待期间取消不算真实尝试。

**生产接线约束：** 同Session最多一个未终态工作，队列只承接可靠冻结输入；源正在被直接修改时仍拒绝准备新副本。认领执行资源保持单进程同步操作，失败先补终态再清理并释放lease。重启先停驻旧任务，不自动重发写入或释放未知Token。Python/API/Delivery入口、主投影queued及队列页面必须一起验收后才能接受P3整体完成。

**验证：** 19项新组件测试及预算相关回归共66项通过，scheduler模块100%覆盖；Ruff和Mypy77模块通过。覆盖4+20边界、无副作用容量拒绝、准确Session/句柄所有权、提交顺序/时钟回拨、重启纯重建、子任务排除、FIFO可运行性、终态后保留清理所有权、模型槽取消/异常释放、四父等待子任务以及重试backoff释放。这些为独立底座阶段证据；Host/HTTP接线及新增验证见下文。


**P3主链接线补充：** Host委派RunDispatch管理临时ticket/root/通知，只从既有事实投影接收队列；不增加第二事实源。Python/HTTP/Delivery writer共用同一个admission和等待/执行Task，Python返回最终RunResult；删除HTTP的执行task/reservation所有者。queued释放实际执行claim但保留Session身份；调度在同步claim后启动，精确取消/异常/实际清理结束后才释放根额度，控制面锁释放也唤醒队列。

为避免把半成品源当基线，忙源直接拒绝；等待的source没有可信Git checkpoint则接收前拒绝。source/worktree在执行锁内重验，未started重启不补写当前checkpoint。非Git即时源执行保留原限制。每次模型attempt在拿到全局2槽后才做U2预留，backoff/工具/子任务等待释放槽；Delivery控制Run和readonly子Run不重复占根。配置守卫同时覆盖未start队列和准备ticket，不只看活跃Task。模型连接探测共享槽，并由后续ADR-067纳入控制Run账本。

**接线验证：** 200项接收/快照/写副本/交付/worker/预算/HTTP回归及最终38项调度专项通过；scheduler与run_dispatch均100%覆盖，Ruff/Mypy81模块、help和smoke12/12通过。专项包含真实进程退出保留接收checkpoint、24接收/25拒绝、精确取消/无消费、Continue漂移、控制锁唤醒、启动失败释放、同步写重复取消、4父+8worker及5交付writer容量。前端与整包全量由集成任务单列，未调用真实模型。

Vue/分发集成补充（ADR-065）：42项前端回归、6条真实API静态旅程、typecheck/build/format和wheel通过；队列读取/取消/刷新/自动补位经实际6对话验证。新Docker镜像的静态资源、非root/无需Node、管理来源及重启后数据与队列额度通过。全量后端集成另发现内部状态文件影响源码checkpoint及旧断言未适配，此为阶段性记录；后续修复和完整门禁已通过，P3已完成（见AGENTS.md）。


**P3全量回归修正：** 显式内置状态时，SQLite追加自身日志不能算源码漂移。采用精确Runtime文件与3个受管产物namespace的untracked排除，保留全部tracked diff、普通文件和项目Skills；state==Workspace布局接收前拒绝。策略随checkpoint版本化，旧基线不能静默改写；crash-gap与worker使用相同口径。直接Python现在先admission再started，测试不再假设只有一个身份事实或一个运行基线。Skill在源更新后继续使用旧内容须固定snapshot，source执行仍严格拒绝漂移。

修正验证：19项新增源码证据测试包含默认/自定义内置状态、真实附件和持久快照、worker、重启Continue、旧口径保守拒绝、普通文件/数据库/项目Skills/已跟踪同名产物/链接保护；相关40项及161项生命周期回归全部通过，Ruff/Mypy81模块通过，独立只读复核未发现阻断。完整集成门禁由主分支继续运行。

**P3最终集成验收：** 修复完成后的统一版本完整1180项通过、7项默认Docker跳过、覆盖93.90%；76项真实Docker及相关生命周期另行全通过。6条真实API静态页面旅程、48项前端回归、typecheck/build/format、Ruff/Mypy87、help及smoke12/12通过，wheel与最新镜像资源/非root/同源/重启/队列额度验证通过。P3/P4/P5据此转已完成；源码证据修复和worker测试替身修正均有本地提交，未调用付费模型。

## ADR-066：冻结评测输入，脚本机制与模型效果分开验收

当前修订：当时冻结live拒绝是阶段限制，现由ADR-082主模型账本和Web规划确认取代；隔离执行事实与冻结判分仍保留。

**状态：Accepted（B1 任务集／离线独立判分和 B2 证据完整性）；B3 真实重复实验、B4 Web 入口继续按步骤验收。**

**决策：** 用严格版本化 manifest 固定20道任务的内容哈希、类别、家族和12开发／8留出划分。历史使用过的三个工程题只进入开发集。留出纯机制验证只检查任务能运行及判分能识别错误，不用于选择模型参数或优化策略；一旦参考留出效果调参，移动该任务并补新留出、提升版本。

事实题必须给出标注事实与文件行号，修复题必须通过源目录之外冻结的测试、保护文件检查及真实 worktree 隔离。跨文件任务要求每个声明故障文件都修改；不存在仅凭最终正文或模型自评通过的路径。恢复交付题通过当前 Host 的重启、verify/apply 和源码漂移拒绝验证生命周期，评测程序的控制动作单独标明，不宣称具备未来有界自动修复能力。

**执行与边界：** 共用 `run_evaluations` 和生产 Host，状态、源项目、固定测试按用例隔离；只生成可追溯报告，事实仍来自各自 RuntimeStore。冻结集 live 在凭据读取之前拒绝；旧显式 live 套件保持兼容，但不能直接复用为主服务 Web live 控制面，否则会绕过全局模型并发及共享预算。Web 接线须先解决额度／预算归属，不复制或导入账本作为补救。

**验证口径：** scripted20/20只证明 fixture 与验收机制；失败正文、测试篡改、额外文件、不完整跨文件修复及错误引用必须拒绝。真实模型质量、成本和留出成绩只由未来独立实验给出，禁止从离线通过率推导。

**B2证据补充（ADR-066）：** 实验报告从每个独立RuntimeStore导出 canonical 事实，复用现有用量和费用投影，固定有效生成配置、Skill/工具指纹、任务版本与运行代码commit/dirty/hash。未报告值不补真实零，失败重试和取消保留未知；显式价格输入保留原版本，不复制用户账本。报告逐题原子保存，取消保存后继续传播，live故障停止扩大批次；默认整例300秒与12步/20,000已观测Token形成实验边界，副作用收尾不承诺硬实时。冻结验收原文长期留在本次临时产物目录，报告只是证据导出，不能成为平行运行事实源。真实重复实验和Web共享调度仍未接受完成。

## ADR-068：Web冻结评测先接离线控制Run

当前修订：离线优先阶段已完成，真实live的主账本/预算归属及显式规划确认由ADR-082补齐；下文live拒绝描述仅适用于当时阶段。

- 状态：Accepted（离线控制面；真实模型评测未实施）
- 日期：2026-10-16

Web首先提供内置工程集的scripted机制自检。主Host的归档Project控制Session、Store.admit_run和RunDispatch拥有批次接收、4根/20等待额度、取消与终态；固定正文和资源指纹写入唯一主Event Log。执行回调直接await既有Python runner，使用独立执行键，不占源checkout锁或进入Agent模型循环。

案例需要独立Host/Store，恢复题还会关闭重开被测Host，因此不能把主服务传给旧grader。case数据库仅作为原始测试证据，不承担Web任务状态、不导入主usage账本。固定offline常量配置禁用真实Provider/MCP和凭据解析；报告标记机制验证且费用不适用。真实live接口明确拒绝，未来需设计可信计费归属与主模型槽/预算集成，不能通过切换案例Provider开放。

每个Project首版仅一个未终态评测批次，案例顺序执行；不新增独立队列/后台无限task。接受时固定cases，分发时不重读任意路径。HTTP接收幂等回放原身份，停止等待真实清理，重启仅停驻不重放，控制Session拒绝普通Continue/abandon。主事件保存逐题结果与不可变报告指纹，JSON仅作审计导出。

唯一任务资源由bundled loader提供，包含wheel/Docker。产物限定state/evaluations，沿精确untracked排除策略；已跟踪文件/链接仍保护，旧checkpoint不偷偷放宽。留出集机制自检不能用于调参，也不能把scripted通过率写成模型能力。

## ADR-069：增量索引复用必须验证内容、配置和实际路径

**状态：Accepted（C1 索引机制）；Web 索引控制面、语义模型和效果对照另按 A1/C2–C5 验收。**

**决策：** 保留现有内容哈希增删改、模型/切块指纹和事务外计算。构建只在 generation、源码清单及配置复验一致后原子发布，读者继续使用固定 WAL 快照。复用旧向量之前检查保存的指纹与元数据一致；不将损坏身份静默改为新指纹。显式 force 才允许全量替换，历史无切块元数据的固定窗口兼容保留。

Git 枚举不是文件边界：已跟踪目录被外部改为链接时，旧路径仍会返回。扫描因此拒绝文件及祖先链接、非规范路径，并使用有上限的文件读取。索引父目录、数据库、WAL/SHM/journal 不允许链接或目录重定向；数据库类文件也拒绝硬链接。固定 Workspace 身份，在连接前后及构建提交前检查目录与数据库身份，避免将工作提交到被替换的派生索引。

**边界：** 这是普通本地文件操作的路径校验与漂移检测，不是操作系统沙箱，也不能锁住任意外部进程在检查间隙修改文件。构建期间应避免编辑语料或移动目录。C1 不添加下载、embedding 供应商调用预算或新的运行状态源；检索质量和成本不由这些机制测试证明。

**验证：** 35项新增定向回归覆盖真实 Git 跟踪祖先链接、索引及附属文件链接、硬链接、打开时/计算时目录替换、有界增长读取、同时间戳同大小内容修改、无变化向量复用、删除、配置与损坏指纹拒绝、并发构建获胜及失败保留快照。全部检索/结构回归102项通过，index覆盖92.41%；全仓Ruff/Mypy88、help、smoke12/12及离线检索16/16通过。使用本地Hash，不调用真实模型或embedding服务。

## ADR-070：配置修改采用版本比较和原子持久回执

**状态：Accepted（配置写入子集；A2整体仍在推进）。**

**决策：** Provider保存/恢复及模型/会话参数修改必须提供读取版本与幂等身份。相同请求返回历史结果，过期版本明确拒绝；配置事实和完成回执原子提交。关闭旧客户端失败不先保存新配置，结果不明时保守阻止运行并核对持久记录。会话配置放在内部控制Session，绑定创建身份。

**理由与边界：** 多标签和网络断开不能造成覆盖或自动重复修改。客户端只存请求键和摘要，重新输入原密钥后确认未知请求；不会自动新建身份。保持单服务进程和现有SQLite事件事实源；不引入Redis或分布式事务。此项不等于全部管理写接口已迁移，交付等仍按后续阶段推进。

**验证：** 完整后端1344通过/8个显式跳过、覆盖92.67%，最终恢复边界97项定向回归、Ruff/Mypy92通过；82项Vue、36项旧页及14条整套浏览器检查通过，另补生成参数0/null真实API旅程。wheel隔离资源、Docker构建/非root/同源/重启/开发12题离线评测和smoke12/12通过；未调用付费模型。

## ADR-071：本地检索模型显式准备，重排保留独立证据

**状态：Accepted（C2本地模型机制；默认质量晋升另按C4/C5验证）。**

**决策：** 可选sentence-transformers固定6.1.0，模型采用本地主机绝对目录、固定revision和内容清单。仅接受标准模块与safetensors，禁用远程自定义代码，运行时不下载。RRF候选最多20交给CrossEncoder，最多返回5条，原融合分数和重排分数分开。失败明确降级，不能虚构语义分数；Hash仍是默认机制验证实现。

**依据：** [SentenceTransformer官方接口](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)及[CrossEncoder官方接口](https://www.sbert.net/docs/package_reference/cross_encoder/model.html)支持本地加载、禁用远程代码及固定推理参数。两套固定实际模型已在网络连接被拒绝的测试中完成本地推理、索引、融合/重排及Host审计调用。

**代价与边界：** 可选依赖和模型较大，内容复验增加I/O，默认镜像与离线CI不隐式安装/下载。首版只验证本机CPU，中文代码检索的准确率、p95和成本取舍由C4/C5决定，不据此启用默认语义配置。索引Web与远程embedding用量接线另行完成。

**验证：** 177项相关回归通过，检索/结构143项覆盖93.28%；默认跳过的真实模型用例另在隔离retrieval环境显式执行通过，包含禁止网络连接与Host工具审计。Ruff/Mypy93、uv锁检查、wheel隔离资源、smoke12/12及原检索16/16通过。模型与原始日志保留于忽略目录，未调用付费模型。

## ADR-072：远程检索复用请求账本与工具所有权

**状态：Accepted（Host查询接线；索引管理长任务另验收）。**

检索只能从ToolExecutor/Policy之后进入异步embedding单次派发。模型槽覆盖请求和客户端清理，CPU线程取消等待实际退出；预算拒绝保存工具结果后停驻。已观测软限按唯一attempt及子Run累加，终态汇总不再次收费。输入型费用沿用固定价格和预留口径，未知输入不结算为零。

验证：73项主链/预算/worker专项与49项摘要/流式/用量/契约回归通过，embedding/retrieval派发覆盖91.77%；83项前端与3项静态分发、构建/类型/格式、Ruff/Mypy94通过。使用假SDK响应、真实Host与Git索引，不调用付费服务。

## ADR-073：查询改写只占根任务一次有界尝试

**状态：** Accepted（机制；默认效果晋升另验收）。

**决策：** 默认关闭。原查询先检索，根Run及worker共用持久认领的一次改写，最多一次补检索；调用原有模型请求链路，禁隐式/显式重试，使用共享预算和模型槽。无效、失败或索引漂移保留原结果，预算拒绝/取消仍执行原控制规则。两组候选按排名融合并去重，保留原查询候选，不相加跨查询重排分数。

**取舍：** 一次失败会用掉额度，牺牲短暂故障后的恢复机会，换取明确开销和无重复调查。查询文本按不可信数据封装，模型没有工具；这不能保证生成语义正确，所以必须保留原候选并通过冻结集决定是否启用。没有质量、延迟和全部尝试费用证据前不宣称收益。

## ADR-074：检索升级由冻结对照决定默认配置

**状态：** Accepted（实验工具及不晋升决定；四路排名、真实改写与窄引用配对完成，费用未知）。

**决策：** 固定40查询、开发/留出各20和语料hash；本地四路对照共享索引，保留重复尝试、失败和延迟，输出字符统计不称为Token。安装包包含数据，离线CI使用Hash只验机制。真实固定本地模型在禁网环境另测，查询改写需复用真实Run账本，不能使用预先编写的理想改写冒充模型效果。

**结果：** 2026-10-16留出词面Recall@5/MRR为0.80/0.75；语义0.85/0.85、混合0.85/0.80、重排0.85/0.7292，但延迟均超过词面两倍。因此不晋升默认；没有下游通过和费用证据也不能宣称整体优化完成。小型教学语料和共享语料划分的局限必须与指标同时报告。

## ADR-075：索引构建复用持久Run与共享预算

**状态：** Accepted。

**决策：** Web构建使用专有归档控制Session、同源幂等接收、原4根/20等待调度和2模型槽；配置冻结、密钥只留内存。CPU线程将每批最多64片段交给原异步embedding账本，不新增Host/Store。停止等待真实请求清理，在发布前及提交前检查取消；失败保留旧索引与已发生费用。发布代次/统计在同一事务取证，事件摘要与独立索引可重建数据各有职责。

**边界：** 不锁住整段源码编辑，提交前漂移拒绝保证旧索引不被错误替换。外部进程仍可能在检查间隙修改文件，不能称为沙箱。重启不自动重发写任务；用户明确新建构建。取消到达前已提交的数据无法回滚，缺发布事件时不把结果臆断为未发生。索引控制记录只能经专页操作，不套用普通聊天Continue。

## ADR-076：修复轮次有界，验收固定，限额绑定交付身份

**状态：** Accepted（后端机制；Web闭环另按D6验收）。

**决策：** 复用既有DeliveryController/独立Writer和Verifier，不引入图编排主链。首次加最多两轮修正，每轮实际验收绑定候选patch；无进展和相同失败提前停止。验收条件、后端和保护范围在修复开始时固定，旧run/verify不能追加第四轮或放宽条件。

跨轮模型派发在原ModelBudgetController统一检查：36次真实模型尝试、60k已报告Token、15分钟绝对deadline。包含摘要、worker、改写和失败重试；embedding只进Token。根Run/Continue不重置交付，关闭或失去控制Run所有者后拒绝继续派发。未知账本与原子预留继续保留。修复控制Run不重复占Writer根槽，取消等待实际清理。

**边界：** 模型预算约束不能保证供应商账单硬上限，清理可能超过15分钟。重复失败签名只归一化耗时，不宣称语义理解；可能保守提前停止。验收通过后仍由用户明确采纳，源码或候选变化使证据失效。没有自动提交/合并/推送，也不自动重启未完成交付。

## ADR-077：Web交付控制与Writer共享根名额

**状态：** Accepted。

prepare/repair/apply以同源幂等HTTP接收持久Run，复用原Root队列；Web修复Writer是可信控制Run子任务，借用父根lease而继续独立持有Session/checkout所有权。否则四个控制任务占满四个根槽，再等待Writer会死锁。只允许operation=repair且父Run实际持有活跃lease的专属入口借用，普通用户Run/完成Run不能借用。预算context允许此受信控制Run绑定源项目，但实际Writer checkout仍须匹配交付凭据。

用户查看补丁并明确采纳对应SHA；服务端在接收、队列执行和最终apply复核证据。操作事实与领域事实保存在同一Event Log，跨Session按提交顺序读取。Vue只展示实际验收结果，刷新/未知接收不自动换键或再次写入。macOS规范路径别名不视为越界，产物内部链接及hash篡改继续拒绝。

ADR-077验证：完整1460项后端通过、10项显式可选跳过，覆盖93.54%；55项交付/快照回归与9项控制专项、92项前端、17条浏览器检查通过。wheel资源与真实Docker准备/重启/幂等/有界修复采纳通过。全量浏览器暴露的源项目配置继承串线已修复，补充采纳后普通对话和重启回归；Mac路径别名、test_tokens.py快照清单均有回归。无付费模型请求，不能代替真实模型效果评测。

## ADR-078：会话元数据采用版本检查和原子回执

- 状态：Accepted
- 日期：2026-10-16
- 决定：会话改名、归档和空会话删除要求读取版本与持久请求身份；修改、审计、
  成功回执在一个 SQLite 事务中提交。审计属于服务管理所有者，保留空会话可删语义。
- 理由：多标签页不能静默覆盖；丢失响应后的重试不能修改同身份重建的会话。
  运行进展不属于元数据版本，归档不影响后台工作。
- 验证：事务失败回滚、重启回放、旧版本拒绝、删除后同名重建保护、Vue迟到响应
  隔离，以及真实 API 浏览器丢响应/刷新/同键重试、多标签冲突和归档恢复验证。

## ADR-079：工作副本维护接入持久控制Run

- 状态：Accepted
- 日期：2026-10-16
- 决定：Web工作副本创建/保留/清理沿用Host管理边界，以持久Run接收并进入共享
  根任务队列，冻结创建提交与目录版本，完成结果写回同一Run。
- 原因：Git操作可能跨越HTTP请求或页面生命周期，不能靠浏览器保有执行所有权。
  删除前需复核引用与副本现状，重启和未知响应不能触发自动重放。
- 限制：Worktree是Git副本，不是OS沙箱；不支持强制删除、分支删除、自动合并。
  停止不撤销已完成操作。标记保留是审阅状态，不代替删除时的明确确认。
- 验证：真实Git创建/保留/清理、历史保护、冻结提交、版本漂移、队列取消、
  停止等待真实结果和重启停驻；浏览器丢响应后刷新同键重试与320px布局通过。

## ADR-080：旧版导入内容隔离为只读历史

- 状态：Accepted
- 日期：2026-10-16
- 替代：ADR-024
- 决定：有界一致只读读取并验证、脱敏；不读取或恢复Provider配置。旧事件封装为
  legacy.event，旧总量独立保留，不能冒充新执行、审批、预算或模型调用事实。
  导入Session归档并标为只读历史，Agent/Continue拒绝；保留消息、输出和JSONL证据。
- 原因：旧库是外部数据，事件名称不足以授予新Runtime执行权。旧聚合用量没有
  逐尝试证据，不能补造精度。连接配置也不应随历史内容恢复。
- 维护：同一migration模块服务开发入口与Web；目标写入和完成回执可在同一
  事务提交，回执失败整批回滚。Web预检命名空间可稳定映射旧身份，避免占用内部身份。
- 验证：33项导入/工作区/CLI回归，导入模块92.37%覆盖；WAL一致读取、源未修改、
  恶意事件隔离、只读Session、资源上限、引用/类型损坏、重复导入及回执失败回滚。


ADR-080 Web补验：HistoryImportController复用根任务调度；预检固定脱敏档案，确认
绑定项目与hash，导入数据/成功回执/终态同事务提交，取消等待真实线程结束。
Vue提供确认与未知请求同键重试，历史页按session_seq分页显示只读消息。
106项关联后端、108项前端、20条浏览器检查及真实容器预检/导入/重启通过；
原库未改，无模型调用。旧入口继续保留到X统一退役。

## ADR-081：项目登记变化与成功回执原子提交

- 状态：Accepted
- 日期：2026-10-16
- 决定：项目登记/移除经现有配置控制Session持久幂等，SQLite身份关系与成功回执同事务；移除只删空登记，保留源文件。启动Workspace不能移除。
- 原因：请求丢响应不能造成删除/重建循环，也不能使默认Workspace悬空；重复注册使用原回执，路径再次登记使用新身份。
- 维护：原子回调只追加同一连接事实，不嵌套提交。浏览器待确认移除保存精确身份，刷新后不依赖列表存在。Python内部生命周期继续复用同一Store检查。
- 验证：72项工作区/身份/HTTP/会话关联回归、112项前端测试以及真实API注册/移除丢响应刷新旅程通过；全仓与容器集成结果另记AGENTS。

## ADR-082：隔离评测引用主服务的唯一模型账本

- 状态：Accepted（主模型账本及Web规划确认已接入；真实效果另验收）
- 日期：2026-10-16
- 决定：隔离执行Store保存工具/验收事实；模型尝试只写主服务Store，通过精确case及本地Run身份关联。模型槽、根/项目/日预算沿用主调用链，本地观测上限读取同一权威。
- 原因：独立case数据库不能变成绕过共享预算的独立付费入口，也不能把尝试事件复制到两边后再次累计。
- 维护：response/summary的usage_authority禁用旧账本回退；证据必须明确两种事实来源、导出hash及实际价格版本。离线入口不读密钥、不启用桥接；断流/取消/重试的未知消费仍归主账本。暂拒绝嵌套桥接和未接完整累计限额的本地bounded repair批次。
- 验证：89项离线关联回归，相关模块93.59%；共享2槽、取消等待零尝试、派发取消保留未知、项目预算、子任务/摘要/重试去重、case观测上限及权威报告验证通过。没有真实模型调用。

ADR-082 执行器补验：受控live Python批次需确认及当前generation_hash，冻结默认值和能力，case禁止自行创建Provider，使用主模型槽和账本。12题开发集经脚本Provider机制验证全部通过；预算拒绝中止、输入漂移拒绝及重启不重发通过。62项关联回归、113项前端通过。ADR-066关于每case模型账本独立的描述由本决策替代；case工具/验收状态仍在独立Store，只有模型尝试归主服务权威。Web live规划确认及接收已完成；真实模型效果继续独立验收。

ADR-082 Web接收补验：已开放默认offline、显式live规划确认。原generation_hash校验升级为quote_hash，绑定任务、模型连接/参数和有效价格版本；每题20k Token的90/10场景仅用于规划，未知价格不视作免费。确认后首次接收固定规划，未知响应重试回放原身份，不重新确认或派发。实际结算仍由主Host唯一调用账本决定，案例执行证据继续隔离。ADR-068的“live明确拒绝”由此取代；真实效果仍须独立实验。

ADR-050 GLM接入补验（2026-10-16）：能力绑定精确协议/官方端点/模型。兼容Chat顶层reasoning_effort与Responses reasoning.effort分别映射；GLM只开放enabled，精度和输出范围同时校验服务默认及用户覆盖。97项定向后端、116项前端和3条真实API设置页旅程通过；GLM真实普通/文件工具3次请求完成，完整效果仍按B3/U5。

ADR-025 接收边界补充（2026-10-16）：HTTP接收是否成功由持久Run身份决定。提交后的缓存/派发错误不撤销已接收身份，客户端查询真实终态；未知409及5xx保留原请求。Skill文件发布与SQLite回执不能原子提交，故只读预检之后的发布/回执错误一律保持未知，不以异常类型推断零副作用。验证包含三个执行位置、Continue、发布后安装事实失败、成功回执失败及刷新同键回放。

## ADR-084：摘要认领持久化，后续请求需新增内容与冷却

- 状态：Accepted（摘要节流机制；真实收益另按U5验证）
- 日期：2026-10-16
- 决定：摘要 callback 之前在唯一事件日志原子认领 Session/provider/model 的完成前缀内容。
  相同完成前缀不再次自动请求；失败、未采用、取消、重启均保留认领。
  后续完成内容新增至少4,000个序列化字符且间隔至少60秒，才允许新的摘要；首次无门槛。
- 原因：未采用的摘要没有checkpoint，单纯按上下文超限触发会在后续步骤重复请求同一历史。
  认领与SQLite检查在同一事务，两个Store竞争也不能同时付费；冷却只判断资格，不等待。
- 边界：认领不是消费，不补造attempt、usage或checkpoint；预算拒绝和槽等待取消也可能消耗
  本次摘要资格。有效checkpoint优先复用，节流不绕过硬上下文上限；折叠仍不够时明确失败。
  不自动重置认领，不提供模型或Skill绕过入口；字符不是Token，尚不宣称真实成本收益。
- 验证：15项新增专项及110项摘要/Context/Host/预算/用量/评测桥回归通过，
  context_builder覆盖91.59%；全仓Ruff、Mypy113模块、help和离线smoke12/12通过。
  验证使用离线Provider，未调用付费模型；完整版本收口仍由R阶段集成执行。


## ADR-083：实验统计只读派生，优化收益必须通过可比检查

- 状态：Accepted（统计及实际对照完成；无已证实降本，未触发机制收益未证实）
- 日期：2026-10-16
- 决定：从 schema v2 已有独占尝试和费用快照产生可丢弃统计，不写账本、不重新定价。
  所有失败和重试进入总消费，未知/估算保留，币种不相加，cache/reasoning 不重复收费。
- 比较：冻结任务、输入、模型和实际生成配置；运行代码、工具/Skill 和上下文等优化
  差异必须在全部候选请求之前逐项声明精确前后值与时间。允许先收集开发基线再
  选择优化并声明；不允许使用留出结果选优化或调参。重复次数相同且各 variant 身份稳定，逐题
  通过次数不能下降；完整 Token 或相同价格版本及费率依据、同币种完整费用下降才可宣称改善，
  延迟变化一并输出。scripted 机制通过、样本缺失和未知消费不能充当真实节省证据。
- 边界：本机声明和导出报告不是远程预注册或供应商账单。统计不补造缺失字段；缺失
  费用或零成功时 cost/success 不可计算。没有改变留出判分，也没有发起付费模型请求。

## ADR-085：Web唯一入口与用户终端退役

日期：2026-10-16，落实ADR-048/055。

- 状态：Accepted。
- 决定：用户操作统一Vue/HTTP，删除cli.py、原生前端和UI专属测试，不保留转发空壳。部署只提供eventide-server及python -m eventide，两者启动同一FastAPI/RuntimeHost；根地址重定向/workbench/。
- 保留：任意suite评测、检索、手动交付验收、历史导入、JSONL/Trace/OTLP等Python开发接口。原终端测试中的底层验收先迁移到这些接口及HTTP，再删除终端展示/解析检查。
- 维护：CI使用真实服务启动冒烟和Python离线评测；Vue组件及真实API浏览器检查继续覆盖用户流程。wheel拒绝旧CLI/原生资源并检查新console entry point，Docker运行阶段不依赖Node。
- 证据：详细检查与集成分发证据见AGENTS的X1–X3；质量效果与最终R验收继续单列，不由入口退役推定完成。

## ADR-086：重复失败停驻只采用已发生结果和源码证据

- 状态：Accepted（结构化失败窗口；真实收益另按U5验证）
- 日期：2026-10-16
- 决定：普通Run最后三次实际工具结果在工具名、原始参数、错误和可验证源码checkpoint
  上一致时停驻。指纹和调用引用留在canonical事件中，计数由当前Run/显式Continue链重建。
- 原因：单靠模型步数上限会重复花费在同样的失败上；按调用缓存或跳过写工具会掩盖真实副作用。
- 边界：成功、不同参数／错误和源码进展重置窗口；未知结果、预算拒绝和参数纠错走原规则。
  仅接受异常类型、超时或非零退出码等结构化依据，旧Error:字符串判错不纳入新窗口。
  写工具复用既有checkpoint，只读工具只在阈值候选时复核。非Git或证据不可得退回原步数上限。
  尚未派发的同批工具只写abandoned，不伪造prepared。Continue保留失败链，用户仍可明确再决策；
  取消优先，不自动重放副作用，不宣称真实Token或费用收益，也不把源码无变化当验收失败原因。
- 验证：17项新增真实Host专项及183项关联回归通过、1项真实Docker专项默认跳过，
  tool_loops覆盖96.55%；全仓Ruff、Mypy114模块、help和离线smoke12/12通过。
  实测本机Shell失败、源码修改、外部编辑、Error:文件正文、并发、重启Continue及取消竞态；
  模型使用离线Provider，无付费调用，容器路径仍由R阶段集成验收。


## ADR-087：SDK传输超时服从已配置模型截止

- 状态：Accepted。
- 日期：2026-10-16。
- 决定：三个官方SDK adapter将Settings.model_timeout显式传给客户端，统一连接、读取、写入及连接池阶段；Host继续限制完整请求并等待真实清理。
- 原因：真实项目重放一次请求约5秒失败，检查发现SDK仍使用其5秒连接默认值；模型配置90秒没有传到SDK。现有证据不能确定该次失败的底层网络原因。
- 边界：不增加重试，不重新发送流式半成品，不消除unknown用量或原预留，不修改交付36次/60k/900秒限制。
- 验证：直接实例化三个真实SDK客户端检查各阶段配置、重试关闭与无网络关闭，连同流式/探测/线协议78项回归通过。两项历史修复均失败并保留，不以配置修正代替效果验收。

ADR-074补充：检索评测共享生产的结构化改写结果，不从格式化字符串重建排名。对照支持协作停止及部分结果，未尝试与失败分开，缺样本不生成默认晋升结论。33项离线改写/对照回归通过，无付费请求；完整受控模型对照另验收。


ADR-085分发补验：主分支119项前端/24条浏览器检查通过，干净wheel在脱离源码环境启动成功。一次旧setuptools构建缓存污染被退役入口门禁发现；新增精确workbench文件集合及重复路径检查，6项分发回归通过。发布必须以实际包内容为证据，不能绕过失败检查继续发布。


ADR-083身份补验：交付随机writer_delivery_id属于证据定位，不属于可重复执行配置，从稳定对照身份排除，原报告不改；local/docker后端差异仍识别。33项统计专项通过。

ADR-074受控实验补验：Python检索改写对照以专属归档控制Session和 `python_experiment` Run进入原Host调度，每条查询/重复各有根所有权和一次生产改写认领。只复用原模型槽、预算和唯一尝试账本，不创建新HTTP/CLI/控制器或Scripted Agent工具链。独立原检索排名、补检索融合排名及耗时写主事件日志，JSON可重建。无效/不变/截断输出计degraded并继续，不把回退排名通过冒充有效改写；模型请求故障、索引漂移、预算及取消停止扩批并保留部分证据。重复取消等待真实清理，重启不重发，普通Agent/Continue拒绝控制身份。此为接线验收时的阶段记录；随后40查询真实改写排名已完成（见后续ADR-074补验），下游/费用效果独立验收，默认保持关闭。

ADR-074本轮验证：21项新增专项、108项改写/检索/控制/调度/预算关联回归，以及全仓Ruff/Mypy114通过；没有发起付费请求。

## ADR-088：重复只读委派以子接收事实和可信快照判定

- 状态：Accepted（字面去重机制；真实收益仍由U5测量）。
- 日期：2026-10-16。
- 决定：同Run/显式Continue链内，角色、精确目标及可信源码快照完全一致时，整批在任何子身份创建前拒绝；不缓存、重新计费或伪造worker回答。返回已有父工具及child身份供查询原结果。
- 事实：在原子子run.admitted中保存脱敏前计算的指纹和父call引用，重启及worker.started缺口不丢失已接收证据；从唯一Event Log重建，不添加去重表。旧无指纹事实不从脱敏消息补猜。只沿同Session Continue链，不共享其他用户意图或会话历史。
- 边界：源码进展或不同角色/精确目标、普通新Run允许委派。批内有重复时整批拒绝，不先启动有效前缀；拒绝不消耗四worker额度。原预算、模型槽、只读和取消链不变。不同表述可能重复仍交由模型判断，不引入语义缓存。已知凭据沿原语义脱敏入口进入子消息，新增指纹不复制原目标。

- 验证：14项新增真实Host场景，覆盖同批全拒绝、四worker额度、Continue/重启、源码进展、秘密目标hash、跨Session边界及admission后缺worker.started；累计125项关联回归通过，worker定向41项覆盖96.83%。Ruff/Mypy115、部署help及离线smoke12/12/workers1/1通过。模型采用离线Provider，无付费请求，整体版本门禁仍由R阶段集成。


## ADR-089：评测 Skills 是显式固定输入，选择与加载分别取证

- 状态：Accepted（机制；真实效果继续按S5/B3/U5验收）
- 日期：2026-10-16
- 决定：评测默认none，可显式选择engineering三个内置包。只读quote绑定内容hash，
  接收物化原Skill包产物并固定版本/资源，派发复验；各隔离case使用原导入和配置接口，
  Delivery Writer显式固定相同用户版本，再由已有Run admission冻结。
- 理由：在不改冻结任务、提示和判分的前提下，才能比较真实按需加载的收益；
  不能依赖当前内置目录或把已选中伪装为已加载。内置包更新不影响排队批次，
  重启继续停驻，不隐式重放或换用新版。
- 边界：仅复用已有SkillPackageService、RuntimeHost/Store和唯一模型权威，不新增
  实验配置库、执行主链或CLI。默认none幂等正文与历史请求兼容；实际消费、
  权限和取消继续遵循原控制规则。没有付费请求，不宣称成功率或成本收益。
- 验证：57项后端关联回归、profile覆盖95%，包含20题scripted机制、按需加载、资源篡改、
  旧请求兼容、排队固定与重启停驻；121项Vue、3条真实API浏览器旅程、typecheck/Vite
  构建、Ruff/Mypy115以及help/service/smoke12通过。没有付费模型调用。


ADR-074真实改写补验：40查询/22有效/18降级，全部尝试reported且未知0；原/融合Recall .925/.95、MRR .9/.9083，但端到端p95 9.16秒。输出256未调参，不以留出结果改配置；输入2853/输出7877及所有降级保留。当时下游未完成、成本未知；随后C5配对已完成且仍无默认晋升，因此保持显式实验，不宣称整体节省或默认晋升。


ADR-074配对引用提取扩展：保留默认关闭的answer_comparison，以相同冻结40查询/标签和实际返回片段测下游文件选择与逐字引用。固定JSON、512输出/空采样覆盖、稳定hash顺序及独立源码行判分，不新增模型judge。局限是标签仅支持相关文件与少量关键词，不能宣称任意问答或工程修复正确；所有配置继续显式实验。原/候选回答按真实attempt归属，候选同时承担改写消费，失败也收费、未知价格不填零。权威事实或回执失败保留缺失状态，不伪造未尝试与完整0；此规则优先于可丢弃JSON导出的完整性。

ADR-074本轮补验：165项关联回归通过，最终96项回归覆盖96.81%，追加6项软限/未知投影/Session价格读取/结构化密钥脱敏专项通过；全仓Ruff/Mypy115及diff检查通过。使用确定性Provider，只证明机制与边界，未发付费请求。C4真实40查询排名对照已有独立摘要；本配对引用的真实效果继续由后续冻结模型实验决定。

ADR-074真实下游补验：固定40查询、120权威尝试完整完成，original15/40、rewrite20/40；候选包含改写总Token43916高于原34386，pipeline p95 17.58秒高于13.89秒。两路本轮Recall/MRR均.925/.900、27/40有序源片段相同，单轮随机答案不作因果推断。没有套餐价格，每成功费用未知；不升级默认配置。原网络中断保留，严格标签/输出上限不因结果调整。

ADR-007/085依赖契约补验：代码采用SDK2的Client/MCPServer，不维持旧SDK执行分支；声明mcp>=2.1.1,<3和httpx2>=2.12,<3，并将生产直接使用的httpx列为核心依赖。旧SDK1.10的[官方导出](https://github.com/modelcontextprotocol/python-sdk/blob/v1.10.0/src/mcp/__init__.py)没有Client，原范围会允许已满足依赖但不可执行的安装。收紧元数据不改变本轮锁定版本；锁环境与全新无dev安装均通过真实stdio/HTTP及服务验收，Docker重新验证。

ADR-083最终实验验收：3个完整固定候选轮10/20、12/20、12/20，34/60；另外原中断0/6、14未尝试不删除，所有实际尝试34/66与计划槽34/80分别报告。完整353请求727591 Token，含中断379请求有1unknown，不补完整总量；全部保留GLM855请求unknown9，价格未知。未按质量挑完整轮，补轮在调用前声明且未改模型、任务、Skill或判分。Skills与改写单项均增加Token；整版guard对照没有保持逐题质量或减少Token，未触发机制不估收益。接受统计、实验和“不宣称降本/不晋升”的决定，不把负面效果改成通过。两个实际历史问题4次尝试均失败并保留。

ADR-048最终收口：Web唯一入口、单进程并发、固定Skills、模型参数/流式、唯一用量预算、检索与有界交付、旧入口退役及分发均有代码和验证。后端1742通过/覆盖93.63%、前端121/浏览器24、真实Docker54及最终镜像/wheel通过；3完整模型轮34/60，含中断实际34/66，真实历史任务四次均失败。全部负面结论和未证实收益保留，不以Accepted宣称复杂任务稳定落地。

ADR-076执行额度补充（2026-10-16）：用户暂缓成本优化并允许本轮提高付费测试额度，交付新增显式`observed_token_limit`，默认仍60,000，严格整数范围1–1,000,000，无0禁用；36次模型尝试、900秒及原独立验收不变。非默认值持久接收并固定到原`limits.observed_tokens`，继续使用唯一交付归属与账本，不新增预算事实源；项目/日预算和未知消费处理仍有效。默认新增字段在HTTP指纹与accepted中省略，旧已接受请求保持原身份和60k行为。更高额度结果单独报告，不覆盖原60k失败，也不称为原预算下的成功率或成本改善。验证证据见AGENTS Q2；真实效果另行验收。


## ADR-090：系统目录选择是本机用户控制面

**状态：Accepted**

**决策：** 添加项目优先使用服务宿主的系统目录选择窗口，固定 macOS AppleScript 或 Linux 系统 Python/GTK 3；不接受任意脚本、运行参数或可执行路径，不安装新依赖。不引入通用文件浏览 API 或读取秘密文件的后备路径。

**边界：** 本机连接、严格同源、单窗口和有界等待；取消必须等待实际子进程回收。选中路径只填表，不替代注册确认，不改变 Agent 工具目录、执行模式或审批策略。无原生桌面时由用户显式手动输入路径。
## ADR-091：导航展示与后台操作身份分离

**状态：Accepted**

**决策：** 返回仅沿已成功访问的内部历史；直接入口回到对话。关闭详情只改变视图选择和读取代次，不调用取消 API、不清除未知写入身份。轮询不能覆盖显式关闭。未保存草稿在路由离开、页面卸载和明确上下文切换前确认，秘密草稿仅在内存中保留。

**影响：** 用户可安全关闭长任务面板；真正停止仍需明确停止动作。浏览器原生 beforeunload 提示内容由浏览器决定；关闭窗口不保证未保存内容可恢复。
## ADR-092：长期偏好是来源绑定的数据，不是执行授权

**状态：Accepted**

**背景：** 跨对话的用户偏好不能复用会话摘要作为事实源，也不能因模型观察到工具文本而自动形成持久授权。普通编辑、撤销、请求重试与 Continue 具有不同生效边界。

**决策：** 唯一 Event Log 保存版本化 global-installation/project 偏好、独立建议和设置。项目用稳定 project_id；工作副本共享，独立 clone 隔离。同作用域同主题冲突须人工处理。明确单行带作用域的用户记忆指令可保存；有界偏好语句重复形成建议，确认后才激活。来源必须是 canonical 根用户事件，拒绝模型/工具/委派/导入/摘要内容作为保存授权；所有历史绑定来源在拒绝、编辑、撤销后仍受抑制。

按 Run 起始快照冻结普通编辑，每次实际请求/重试前排除暂停/撤销/总禁用，Continue 重新快照当前版本。精确记录实际派发的 ID/revision/attempt，不标记“模型因它产生答案”。最多 12 条、每条 500 字符，无额外模型调用；当前明确请求及项目偏好优先于全局偏好。偏好始终是下位数据，权限、审批、模式、恢复、采纳和预算仍由既有 Runtime 控制。

**影响：** HTTP 写入沿原同源/本机/幂等和原子回执契约，版本过期明确 409。schema v5 只增加事实索引，历史不可变。总关闭不隐式禁止主动保存，也不因主动保存自动打开。Worker/交付 Writer/控制会话和冻结评测排除自动记忆，评测条件可比。撤销不等于擦除历史，已派发请求不可撤回，原对话/摘要仍可能包含原文；公开说明检测和自然语言理解边界，不作完整 DLP 或语义记忆宣称。
### ADR-055 补充：对话优先展示不改变执行授权（2026-10-17）

采用中性主题、可折叠项目/会话侧栏、居中对话和按需上下文面板，管理页面保持原路由。工具、错误、审批和停驻以真实后端状态呈现，不模拟执行进度或默认自动继续。Skills 菜单读取项目已配置目录并链接管理页面，不引入未经后端支持的单条消息技能开关。输入草稿与任务队列明确分离；Enter 换行、Cmd/Ctrl + Enter 显式发送，统一前置条件阻止键盘绕过按钮禁用状态。主题与侧栏偏好仅影响浏览器展示，不更改模型、权限或服务设置。

## ADR-093：可选离线容器执行配置与显式候选导出

**状态：Accepted**

将 OS 进程边界作为独立、可选的 Run 执行配置，不替换 Auto/Plan/Agent 权限语义。默认 local 保持兼容；sandbox 固定本机 Docker 镜像 ID、无网络及资源限额，接收时持久化并在整个 Session/Continue 中保持，任何不可用状态都不静默回退。工作副本隔离与容器边界不同：容器不直接挂载源 worktree 或其共享 Git/秘密文件，而使用有界过滤文本候选。

文件工具也在容器内执行，只包装 Shell 不构成此能力。V1 采用正向工具目录与 dispatch 白名单，明确禁用尚未迁入同一边界的 MCP、Skills、worker、retrieval、自定义工具。Provider 和控制面留在 Host，不宣称整个 Agent 离线或完整 OS 沙箱。容器清理不确定和进程崩溃未结算工具均保留 unknown 并隔离候选；只读工具也可能留下容器，不能用只读豁免恢复。候选 unknown 不应阻塞其他源会话。

V1 选择完整、有界、只读补丁导出而非实现第二套自动采纳：保存原过滤 baseline，用户审阅后手工应用；不把过滤文件当删除、不复制任意容器产物回宿主项目。代价是暂不支持二进制/特殊引用文件名补丁、应用内采纳、网络依赖安装、硬磁盘配额和扩展工具。受信任 Docker daemon/镜像仍是前提，不以离线 argv 测试替代真实容器证据。

2026-10-21 补验 ADR-090/093：真实 macOS 原生选择经生产 HTTP 返回正确目录，实际关闭返回 cancelled 并释放选择器。Apple Silicon Docker Desktop 的候选修复、容器内 unittest 与外部断言、可应用文本补丁及超时/停止清理三项通过；另核实实际容器无网络、非 root、只读根、capabilities/资源限制和唯一候选挂载。完整 Linux CI 已在 a9ba371 全绿；0.5.0 仅同步发行元数据和文档，复用该完整证据并定向验证新产物，不重复全量或付费模型实验。schema v5 升级前需备份状态根，旧程序不能直接打开新库。
