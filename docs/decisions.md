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

2026-10-22 限定扩展：交付Controller可从已完成的上一失败轮次，向精确Writer Run冻结该交付的验收call白名单。read_tool_result仅对该Run开放指定控制Session/Run/call的日志页，附原轮次与patch来源；其他会话及后续Run继续拒绝。不是通用跨会话访问，原Event Log仍是唯一事实源。

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

**资源与重试：** 重试部分已由 [ADR-101](#adr-101) 替代；SDK 重试仍为零，Host 按失败类型决定是否在同一步输入上重试。输出内容、参数与推理组装限制 8 MiB，输出项最多 128 个，原始事件最多 65,536 个。连接在完整响应返回前关闭，取消、异常和消费者关闭同样清理。限制是应用层响应组装约束，不是 SDK 接收帧硬上限、费用上限或安全沙箱。

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

2026-10-23补充：本地embedding从固定包读取 `prompts.query` 与 `prompts.document`，用于查询和索引正文；缺少对应提示时维持空提示。包内容指纹与角色策略v2共同绑定向量语义，旧本地索引需显式重建。普通、混合、生产查询及改写补查复用同一查询入口；Hash和远程接口行为不变，不新增模型管理服务或按模型名硬编码提示。

同日CPU修正：实际Qwen全项目构建暴露BF16慢注意力路径，仅将模型实例权重固定float32，使用cpu-fp32-role-prompts-v3指纹并要求旧索引显式重建；不改全局注意力开关或增加MPS后端。代价是实际492文件构建峰值RSS5.838GB、约61分钟，具体证据见architecture及failure-cases RG-INDEX。原BF16排名与Agent实验保留原条件，不能据局部数值接近宣称新精度效果等价。

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

2026-10-21 失败反馈补充：在原有失败后请求内改进证据交接，保留所有验收项的状态及调用来源，失败日志按项提供有界头尾节选和省略量，先裁剪内容再序列化为不超过 12,000 字符的合法 JSON。用一基检查序号关联已固定的命令，避免重复注入命令全文；成功日志省略，完整程度以原执行器元数据和记录为准。提示要求原 Writer 根据证据核查源码并简述待验证假设和修正点，无独立 Reflection Agent 或新增模型调用。验收原文、签名和停止规则保持原口径；接受该有限投影的取舍，不将提示或机制验证等同真实修复效果提升。

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

## ADR-095：独立验收输出使用有界脱敏证据产物

**状态：Accepted（2026-10-22）。**

独立Verifier在原25,000字节/流首尾预览之外，将已接收输出增量解码、跨块脱敏后保存为UTF-8证据；不保存原始秘密副本或把大正文写进Event Log。Session级新增事实通过source_run_id/call_id绑定原运行及两路引用（不修改终态Run规则），记录收到字节、保存字节/字符、摘要、采集状态与编码损失；日志文件是受保留期限约束的证据产物，删除后不可从预览重建。保存故障不改变命令退出码，不停止排空管道，不伪造工具完成或容器清理成功。

read_tool_result省略stream沿用原content字符分页；显式stdout/stderr读取新证据，旧运行不猜分流。授权先于文件访问，沿用同Session或精确Writer Run/验收call白名单。每页默认4,000、最大12,000字符，归档坐标与原始字节数分开；新增引用计入原12,000字符反馈。采集complete/超额/写入失败/中断与available/expired/missing/corrupt分开，进程退出结果独立。

默认正文逻辑配额为每流1MiB、control Run跨轮24MiB、当前状态根256MiB，终态/停驻起保留7天；并发写入共用额度，在写/删除失败/残留文件也计数。现存少量真实验收非空输出仅98/654/655字节，不能估计实战分布；初值依据已有约1MB压力样本及3轮×4命令×2流容量，不是统计最优或文件系统硬配额。活跃交付保护，额度不足保留有界前缀并继续排空。启动、验收前及收尾清理，不新增worker/服务。

日志位于state根的独立命名空间，不开放任意路径或下载平台；Docker不挂载该目录。本地Shell和Host同用户，路径/权限/摘要不能构成强防篡改隔离，继续要求可信本地命令。未扩展任意二进制/编码归档或秘密识别范围，真实模型效果另行实验。

## ADR-094：基础代码调查使用有界本地字面搜索

**状态：Accepted（2026-10-22）。**

**决策：** 增加无需索引的 search_text，支持本地 Plan、源码快照和只读 worker；参数验证、权限、实际 cwd 与工具结果沿用同一主链。路径 glob 过滤、真实行号及匹配行分页，目录枚举、文件读取和输出均有限额，链接目录不递归。达到上限明确显示搜索不完整，支持用精确路径缩小范围。

**取舍：** 新工具固定目录增加637字符；测试目录与压缩基线显式反映开销。单页和单次扫描有限，活动源码跨页不承诺一致快照；非UTF-8、二进制和过大文件不检索。可选Docker沙箱暂不暴露该工具，不允许本地回退。未增加索引、模型调用或检索服务，不宣称真实定位质量改善。


## ADR-096：MCP传输未知与明确工具错误分开处理

**状态：Accepted（2026-10-22，TC-1定向验证）。**

MCP handler返回明确的ToolOutput（正文、is_error、metadata），ToolExecutor在参数及Policy校验后赋予调用身份。MCP的isError作为失败事实，成功正文即使以Error:开头也不误判；明确失败纳入原三次重复失败窗口，不增加独立计数器或自动重试。

SDK的REQUEST_TIMEOUT/CONNECTION_CLOSED及调用中的TimeoutError、连接/流关闭、HTTP传输异常和取消，统一标记outcome_unknown并复用Host停驻、工作区限制和Continue拒绝。SDK无法提供可靠派发确认，可能在请求未到远端时也保守停止；这是避免盲目重复写入的取舍，不是恰好一次执行或远端事务。明确的其他协议错误仍保留原错误通道，发现失败及参数/权限拒绝不升级为未知。

未知不是已失败，也不表示远端已停止；没有自动补偿、重发或人工核销新平台。现有事件、脱敏、导出及恢复边界继续有效；进程崩溃前已有prepared而无结果的MCP仍沿既有保守恢复路径。

TC-2补充：tool.outcome_unknown必须进入逐调用投影；保留prepared的参数和unknown错误，前端优先显示结果待确认。取消不能把未知显示成普通失败，刷新不依赖内存缓存；不新增核销或重发入口。


## ADR-097：副本索引与可核对的检索证据

**状态：Accepted（2026-10-22机制验收；2026-10-23本地真实实验完成，未达下游晋升门槛）。**

保留现有Agent循环和FTS5/向量/RRF，仅按实际Workspace将派生索引移到服务状态根。用户显式选择任务内增量准备，普通Run与每轮Writer复用原所有权、模型槽和预算；构建按Workspace协调，不在任务执行期间自动重建。源码内旧索引只读识别，不自动迁移；状态根位于源码内时拒绝准备，不改用户忽略和交付保护规则。

同一SQLite读快照绑定命中正文、文件哈希、索引代次与模型指纹，再对最终至多5个文件做有界核对；失效正文不供模型使用。模型依据任务选择文件/文本/语义搜索，程序提供明确来源和诊断，不另建意图识别或检索Agent。代价为按副本保存向量及每次最终命中的文件读取；换取可审计的来源与过期提示，仍不保证片段足够回答问题。

生产状态及metadata沿用唯一Event Log，旧历史不补精度。正确性验收与embedding真实效果分开，未达冻结门槛时保留为可选，文本搜索仍是推荐基础。不提高自动重试次数，不默认启用改写或重排。

验收覆盖实际副本/Writer、同快照取证、过期提示后重读源码、冻结配置、准备取消/重启及预算；新增手动构建的幂等身份固定到项目，目标变化不能绕过去重。唯一全量2087过/1旧请求测试待更新/12跳过、覆盖93.69%；旧测试修正并定向通过，Vue199项、35条浏览器分别有通过证据，保留首次1条超时及复验。类型、静态、服务、离线smoke和干净wheel通过；embedding-3两次接入失败，新密钥明确1113账户欠费，因此40查询与24次Agent真实对照未执行。Accepted不代表付费接入、质量推荐门槛或工程修复率提升已达成。

2026-10-23后续：用户授权改用本地Qwen后，40查询及24次Coding Plan聊天任务均按冻结批次完成。BF16候选留出召回19/20，高于FTS5的16/20；下游严格通过0/8→1/8，但引用有效率15/19→12/17且两臂实际语义调用均0，无法归因检索，故不晋升默认。embedding-3历史失败仍保留，FP32另验计算与实际构建，不重跑留出制造提升结论。

## ADR-098：内置文本修改以真实前内容为基准，人工按文件撤销

**状态：Accepted（2026-10-23，最终验证结果见AGENTS的FU-1）。**

**决策：** 在既有 ToolExecutor/Policy 与 RuntimeHost 执行所有权内接管原本地 write/edit，保存实际 before/after，绝不使用 Git HEAD 作为用户修改的恢复基线。schema v6 在原 SQLite 内增加加密不可变恢复产物，准备事实与完整备份同事务；操作状态仍来自唯一 Event Log。复用稳定安装密钥，创建需 fsync，首次并发按路径协调。不把旧正文塞入事件或模型上下文，不创建源码中的明文恢复文件。

**恢复与取舍：** 原地写入保留 owner/inode/扩展属性，完整备份成功后才允许修改。文件写入与完成事实不是跨资源事务，崩溃可能部分写；prepared/undo_prepared或记录故障保守待核验，禁止自动重放/修补。首版只承诺POSIX、单链接UTF-8文本、修改前后各≤1MiB，其他内容明确拒绝；恢复正文不截断，不自动清理。数据库与原密钥是完整恢复所需状态，旧版不能打开v6。

**控制面：** 人工点击本次文件撤销，服务复验会话、实际执行副本、路径、策略、操作状态和当前after指纹；冲突拒绝，重复操作幂等，新文件只删除自身。同路径写入/撤销共享临界区，撤销复用Session/Workspace锁与取消收尾。锁不覆盖外部编辑器/进程；可信同UID Shell和本机API不构成模型身份隔离。无模型撤销工具、自动重试/采纳/合并、任意Shell或整批交付撤销。

**展示：** 共用安全文本红绿组件，但普通操作来源为单次真实前后，交付仍用候选patch。checkpoint仍只保存指纹。长差异按需/上限展示，恢复内容与显示限制分别处理。


## ADR-099：可解释的代码语料范围与严格有界重叠

**状态：Accepted（2026-10-23核心及任务/页面定向验收；效果单列）。**

默认code仅排除明确依赖锁、常见产物目录、压缩资源与有标准生成声明的TS/Go文件；all显式保留这些文本，原Git/路径/大小/编码边界不变。不把项目特定OpenAPI路径当通用规则，不排除全部JSON/YAML/d.ts或宽泛build目录；因此可能保留未标注生成物，不能宣称消除了此前统计的全部37%片段。规则内容和scope固定到索引元数据，有界原因计数解释过滤。改变规则或旧索引无身份时要求force，承担一次重建成本以避免旧新语料混用。

windows-v2保留600/800字符窗口、既有Markdown与可选tree-sitter段，将重叠改为严格最多100字符，并修复纯空白窗口使后续内容被遗漏的问题。行边界只在上限内尽量对齐；长行可能从行中开始，但行号和原文仍准确。无需新框架、tokenizer或自动模型调用。旧索引可查询，新准备必须显式重建；不自动重建用户生产索引。

87项核心定向及静态检查证明边界，历史模型成绩不改判；片段减少、构建成本或下游调查收益须另做同条件测量，不以机制验证替代效果。

ADR-099补充：旧位置到新位置也必须检查已存在索引的完整构建策略，不能以“新数据库为空”绕过策略变更的force确认。只读预检与原build共用切块/语料校验，并在同一读快照核对embedding；不引入迁移器或第二套兼容规则。

准备策略由手动构建、普通Run、Continue和每轮Writer统一冻结；等待构建锁后再次复核，不在等待期间默默接受变更。旧位置只读预检完整策略，写新位置也不能绕过force确认。默认结构切分仍需显式勾选；选项和指标页面不等于真实召回提升。

2026-10-23 RQ测量收口：24条新代码查询的结构候选留出7/12低于历史窗口8/12，Recall下降，故结构保持显式关闭默认。过滤减少明确产物，结构切分减少重复但增加片段与建库量；保留模型完整性校验，不追加猜测性的重排/前缀/默认策略。新Agent v2实际13/16槽后遇Coding Plan限额；12正常完成的9次通过且语义调用0，不能据此认定RAG收益或改写v1历史。机制验收Accepted与效果未达门槛/外部未完成分别记录。


<a id="adr-100"></a>
## ADR-100：Maka 借鉴升级采用统一 Runtime 上的大幅重构

**状态：Proposed（2026-10-26；用户已确认范围与取舍，具体实现尚未开始）。**

**背景：** 当前模型流式失败不自动重试、Continue 仅接受 interrupted、非只读未知结果缺少解除阻塞的核查路径；现有 worker 主要只读，工具/上下文/交互和前端诊断尚未形成完整协作体验。用户明确选择大幅重构和积极恢复，原 14 点及全部观察项均进入正式计划。

**决策草案：** 保留 Python/FastAPI、Vue、SQLite 唯一事件事实和 ToolExecutor/PolicyEngine，重做恢复决策、调度和上下文组织。模型步骤按失败与副作用证据分类重试；服务重启对有效任务主动恢复；未知工具自动受限只读核查，机器可验证证据充分才继续，不明才问用户。Graph 是既有 Runtime 的持久调度层，自动分工/独立写入/整合，最终经独立验收和用户采纳。自然语言明确长期记忆直接保存并提示，推测偏好待确认；旁枝独立只读、用户选择回传。macOS/Linux 原生边界与 Docker 并存，文件工具和命令共同受限。

**兼容与交付：** 可以迁移数据库、调整并废弃旧开发 API，前后端同版本迁移；旧数据、身份、用量、撤销备份和证据保留。先副本迁移与恢复演练，不改历史事实为新授权；升级不自动启动旧版遗留任务。新增能力必须有 API/投影/UI/刷新取消和错误状态验收，不能只交后端。真实完整任务及平台验收放在最后，阶段内做必要离线/定向检查；本机缺 Linux 环境时明确待外部验证，不能缩减已确认平台范围。

**替代关系：** 本 ADR 实施并验收后，按对应步骤分别替代 ADR-015 的仅人工恢复、ADR-053 的流式统一不重试、ADR-096 的未知结果无核查路径，以及扩展 ADR-045/065 的只读 worker 与调度语义、ADR-092 的记忆输入、ADR-093 的隔离选择和 ADR-098 的核查衔接。本轮不把这些当前有效 ADR 标为 Superseded；MK-00/MK-08 等实际切换时再补替代项和验证证据。唯一事实源、工具权限、未知用量、不可变验收与人工采纳继续有效。

**取舍：** 自动恢复及 Graph 增加状态组合、额外请求和资源竞争；保留尝试/时间/预算上限、停止优先、可追溯来源及无进展终止。文件后置状态不等于历史操作恰好一次；通用 Shell/远端未知可能无法自动核实。原生平台能力不同，不静默无隔离执行。Maka 尚未实现的 workspace continuity 和通用核销不能当现成成果照搬；具体增强在 Eventide 中独立设计验证。

完整范围、21项追踪、依赖、接口/UI和验收见 [升级方案](architecture.md#maka-upgrade-plan)、[接口计划](api-contract.md#maka-upgrade-api) 与 [MK状态](../AGENTS.md#maka-upgrade-status)。方案阶段（5acba20）未改运行代码；用户随后已授权实施，当前切片与验收以MK状态为准。应用真实模型与平台完整验证放在最终阶段，不公开发布。

<a id="adr-106"></a>
## ADR-106：浏览器进度连接显式续接（2026-10-26，Accepted）

页面断线后由应用控制连接生命周期和可见等待状态；每次使用最后已处理的持久游标创建 EventSource，后端兼容 Last-Event-ID。离线时暂停，联网和重新可见时唤醒；短时间反复失败指数退避，稳定在线后重新计数。旧连接、已切换会话和已卸载页面不得继续更新状态。

此设计让用户知道页面正在重新观察任务，避免重复提交运行意图。它不改变模型重试、服务重启恢复或未知工具核查策略；这些仍按 ADR-100 分阶段替换。代价是维护显式连接状态和浏览器生命周期测试，终态需要主动关闭以免无意义重连。

<a id="adr-101"></a>
## ADR-101：模型失败分类与步骤内积极重试（2026-10-26，Accepted）

替代ADR-053的流式开始后不重试规则，保留完整终态才派发工具、临时预览和未知用量。主任务可恢复传输失败最多尝试10次，固定输入，SDK重试仍关闭；认证/账单、协议异常及服务商侧活动不重放。结构化账单代码优先于HTTP429，错误文本里的数字不能授权重试。

等待释放模型槽，下一尝试重新预留。默认累计退避最多300秒，模型执行耗时不消耗等待额度，避免一次超时就失去恢复机会。原预算及固定评测单次尝试边界保留。失败请求可能有未知消费并继续占预留，积极重试不保证免费或成功。服务重启与工具核查仍需后续持久恢复实施。

<a id="adr-102"></a>
## ADR-102：前端按精确契约版本门控启动，能力表只声明已实现功能

**状态：Accepted（2026-10-26；MK-00 契约切片已实现并验证，完整迁移底座仍按 [MK-00](../AGENTS.md#maka-upgrade-status) 进行中）。**

**背景：** [ADR-100](#adr-100) 允许数据库与 API 随前后端同版本迁移，[接口草案](api-contract.md#maka-upgrade-api) 要求新增 `contract_version` 与能力发现，"未知版本提示升级，不静默套旧语义"。需要落成可执行的具体策略：前端如何判断兼容、版本信息从哪来、能力表如何避免提前宣传未实现功能。

**决策：** `GET /api/runtime/contract` 返回 `contract_version`（显式常量，破坏性 API 变更前后端同提交递增）、`schema_version`（只读自当前运行库 `schema_migrations` 的实际版本，不做第二份声明，库不可读明确 503）、`runtime_version` 与 `capabilities`（仅列已实现并验收的能力，条目随实现同提交增删，计划中的 MK 项不得出现）。前端启动时先读取该接口：`contract_version` 与构建内 `SUPPORTED_CONTRACT_VERSION` **精确相等**才挂载工作台；不匹配渲染版本不兼容屏障（显示服务端契约/数据/程序版本与升级指引），期间不渲染任何写入口；fetch 失败与 5xx 单独呈现为"暂时无法确认服务兼容性"，不与版本不匹配混同，两者都提供显式"重新检查"。

**替代与理由：** 范围式兼容（后端声明支持的旧版本区间）可减少同步升级摩擦，但当前交付形态是前后端同批发行（MK-22），精确匹配可防止"字段级猜测兼容"回归；能力探针（运行时逐项探测）成本高且无法区分"未实现"与"未启用"，静态声明 + 实现同提交约束更可审计。schema 版本不复制常量，避免与 `store.py` 迁移实现形成双事实源。

**取舍与边界：** 每次契约版本递增都会让旧前端停在屏障页，这是有意的安全默认，不是降级策略；屏障只是控制面阻断，不替代后端权限校验。能力表是声明性清单，其真实性由评审与测试（含"计划项不得出现"断言）约束，不提供运行时证明。`migration_status` 与迁移维护页属 MK-00 后续工作，本切片不虚构迁移状态。

**验证：** 后端契约与状态副本迁移测试、前端组件/门控测试、3 条真实浏览器契约旅程与真实 HTTP 读取见 [AGENTS MK-00 行](../AGENTS.md#maka-upgrade-status) 与 `.task_outputs/maka-upgrade/contract/`。

<a id="adr-103"></a>
## ADR-103：精确唯一编辑与版本绑定文件分页（2026-10-26，Accepted）

替换“首处命中即写”的行为：精确目标必须唯一，重叠位置也算歧义；保留真实修改前后版本和撤销。可用读到的字节哈希防止检查后换版本。分页凭据绑定文件、执行副本和内容哈希，每页仍做路径校验；版本变化必须重读，不能静默拼接。

页预算包括渲染开销，超长行拆为带行内偏移的连续片段。容器/workspace相同不代表同一工作副本，因此宿主注入可信副本身份。代价是每页重新读取并计算版本；凭据不授予路径权限、不保证文件永远不变，也不是文件系统事务。原生隔离、专门的前端证据浏览和真实平台验收后续接入。

<a id="adr-107"></a>
## ADR-107：故障报告是绑定事件高水位的事实投影（2026-10-26，Accepted）

MK-19 的报告不是新的任务状态，也不是可变产物：它是 `(run_id, high_water)` 上的纯投影函数。`high_water` 为纳入事件的最大 `session_seq`，`report_version` 固定叙述格式；同一高水位重放得到逐字节一致的事实，其后提交的事件不会混入旧版本，重新生成才产生新事实。JSON 与 Markdown 渲染同一份投影，不各自取样。生成过程只读已保存事件：不调用模型、不执行工具、不追加事件、不自动上传或发 issue，缺事件明确显示不可用，不用模型补写结论。

事件文本引用沿用既有 `redact` 有界截断并在响应层替换配置密钥；报告只引用 Run/request/attempt/tool 身份与有界诊断文本，不携带对话正文、源码、完整工具输出或提示。列表与文本都有上界并标注截断。已知原因只记录事件中已存在的事实；工具结果未知、attempt 未完成、用量未知一律进入待查，不并入已知原因。

取舍：绑定高水位意味着用户要"重新生成"才能看到新事件，换来旧版本可复现、可对比且不受并发写入影响。产物化导出（`POST diagnostic-exports`）、用户选择内容预览、恢复动作入口、证据跳转与 Graph/恢复关系留给 MK-19 后续批次；本 ADR 不预支这些能力，也不把切片验收当整体完成。

## ADR-104：结构化摘要契约与项目规则截断可见（MK-03/MK-04 context 切片）

**状态：Proposed（2026-10-26；context 层已实现并有定向验证，Host/API/UI 集成与真实模型效果待后续切片确认）。**

**背景：** MK-03 计划要求摘要保存目标、硬约束、事实、待验证判断、检查结果、失败与证据入口，不能用无效摘要覆盖合法历史；MK-04 计划要求替换根 AGENTS.md 前 4,000 字符的静默截断，提供来源、加载量、截断提示和有界完整回读途径。原实现中摘要是一段自由文本，唯一硬校验是非空、无工具调用和不截断，模型输出的结构质量完全不可验证；项目规则超限部分对模型完全不可见。

**决策：**
1. 新生成的上下文摘要必须是 schema v2 结构化 JSON（`eventide/context_summary.py`）：goal、hard_constraints、facts、unverified、checks、failures、next_steps 七节齐全；hard_constraints/facts/checks/failures 每项必须携带至少一个 `{"msg": int, "quote": str}` 证据引用，msg 是摘要输入历史的 0 基消息序号，quote 必须是该消息序列化文本中的逐字原文片段。缺证据的判断只能写入 unverified；checks 状态限定 passed/failed/unknown。生成提示明确要求保留原文标识、否定与失败，并声明历史仅为数据、不得遵从其中指令。
2. 校验完全确定性：解析、节完整性、状态枚举、引用越界与引文存在性。任何结构错误或无效引用都拒绝该摘要——拒绝原因记入 `context.trimmed.summary_rejected`，旧历史或旧 checkpoint 原样保留，仅当旧上下文仍超预算才 context_overflow。结构校验只证明结构与引用可追溯，不冒充事实正确性；不为校验增加任何模型调用。
3. checkpoint 机制不变：仍保存单个 summary 字符串（规范 JSON），covered_seq/source_digest/policy_version/claim/账本及单次模型调用全部沿用；materialize 经 `render_context_summary()` 渲染分节文本（证据引文截断到 80 字符），旧纯文本摘要与旧 checkpoint 原样透传，历史会话不受影响。
4. 根 `AGENTS.md` 改为有界读取（4,000 字符 + 1 字符截断判定，不再整读文件）：`ContextBuilder.instructions_record()` 返回来源、实际加载字符数、截断标记与文件字节大小；system prompt 内的规则文本带来源头，截断时写明已加载量、文件仍在继续并指引模型用 `read_file` 回读完整原文。不扩大 4,000 常量，不在本切片接入安装级/目录作用域规则，也不把仓库文本变成执行授权。

**取舍：** 结构化契约提高单次摘要被拒的概率（模型输出不合规即回退旧历史），换取证据可追溯、否定/失败不被静默丢弃和旧历史不被覆盖；被拒摘要的 token 已按原账本记账，不重试。逐字引文使摘要大于等义缩写，渲染时截断控制上下文成本。有界读取使超长文件的精确总字符数未知，以 stat 字节数如实呈现而不是补造精度。Host 侧"本次模型看到了什么"逐尝试输入清单、目录作用域规则和 API/UI 摘要比较属后续切片，不在本 ADR 声称完成。

**验证：** 142 项受影响定向测试通过（新增 test_context_summary.py 27 项含无效摘要不覆盖、引用越界拒绝、否定/失败保留、工具配对与旧 checkpoint 透传；既有 7 处纯文本摘要响应按新契约更新为合法结构化摘要），Ruff 全部通过，Mypy（py310 目标）2 模块通过。未运行全量、前端、容器或真实模型；首轮 12 项预期失败及根因见 failure-cases MKCTX-ZCODE。

## ADR-105：原生隔离平台后端库与显式失败语义（MK-21 切片 1）

**状态：Proposed（2026-10-26；实现已落地并通过离线定向验证，真实内核边界验证未做，暂不作为可信隔离依据）。**

**背景：** MK-21 要求 macOS（Seatbelt/sandbox-exec）与 Linux（bubblewrap/namespace）原生命令和文件 worker 共处同一 OS 限制，文件工具不能继续在宿主进程执行；平台不能表达所选规则时必须显式拒绝，不静默无隔离重试（继承 ADR-093 的"明确失败不回退"取舍）。本切片只交付集中后端库 `eventide/native_sandbox/`，不接线 `ToolExecutor`/`PolicyEngine`/HTTP/前端。

**决策：**

- 策略输入唯一且严格类型化：`NativeSandboxPolicy` 冻结数据类（profile、workspace/tmp/只读/受保护根，全部 realpath 规范化），支持 `read-only` 与 `workspace-write` 两种 profile。`allow_network` 与 `allow_subprocess` 本切片对两个后端一律 `UnsupportedPolicyError`：loopback 守护（保护本机管理 API）没有离线可验证的表达，宁缺毋滥。
- 能力矩阵逐项声明 `full/partial/unsupported`：Seatbelt 的 `subprocess_deny` 为 full（`deny process-exec*` + 解释器 literal 放行），bubblewrap 为 partial（不挂载 shell 但解释器可执行）；受保护根位于 workspace 内时 Seatbelt 用更具体 deny 切块、bubblewrap 用空 tmpfs 遮蔽（内容不可见而非报错）；bubblewrap 不能表达"workspace 内的只读依赖根"（遮蔽会藏掉内容），显式拒绝。
- 探测只做只读存在性检查（平台 + PATH + 可执行位），返回结构化 `BackendDetection`，并明确"工具存在不是隔离有效"；profile/argv 构造是纯函数，离线可测。
- 文件 worker 是固定 Eventide 代码（纯标准库），经 `<python> -I -c <bootstrap> <包父目录>` 启动，授权以 base64 JSON grant 放环境变量；协议为有界 JSON 行（16 MiB/行），操作限于 ping/stat/list_dir/read_file/open_read/read_page/close_handle/write_file/remove/shutdown；资源句柄是不透明 token；路径先 realpath 再授权，符号链接逃逸、受保护根、越界一律 `path_denied`；read-only profile 拒写。worker 不是工具执行器，无模型、策略或宿主状态访问。
- `NativeSandboxRunner` 是本切片唯一进程生成点：后端缺失、策略不支持、spawn 失败、握手失败均抛显式异常，不存在无沙箱回退；`launcher` 构造参数是测试专用注入（离线协议验证用真 worker 子进程但不经 OS 包装），生产调用方必须保持默认沙箱启动。

**取舍：** Seatbelt profile 采用 targeted-deny（写/网络/进程白名单 + 受保护根切块、读默认放行）而非 deny-default：deny-default 对 Python 运行时的完整 allow 集无法离线验证，宁可如实声明 targeted-deny 的边界并把真实内核验证列为 MK-21 后续门禁。bubblewrap 挂载表即白名单，未验证内核禁用非特权 user namespace 时的行为。句柄/分页读写为 MK-02 续页留接口，本切片不含写入句柄。

**接线契约（后续主任务实施）：** 文件工具执行点从宿主实现切换为：`ToolExecutor` 按会话冻结的 `NativeSandboxPolicy`（含 `default_protected_paths` 派生的状态根/共享 Git 保护）构造 `NativeSandboxRunner`，`start()` 后以 `call(op, **params)` 执行文件操作，`aclose()` 收尾；`NativeSandboxUnavailableError`/`UnsupportedPolicyError` 必须作为明确的工具/会话失败呈现，不得降级到宿主执行；Profile/argv/profile 文本与 worker 版本进入工具指纹与审计证据。API/OpenAPI/页面与实机验证清单见 [架构](architecture.md#native-sandbox-slice)。

**验证：** 44 项离线定向测试（策略边界、能力矩阵、profile/argv 文本、协议与输入边界、真子进程握手与显式失败）、Ruff、Mypy 7 模块（py310 目标）通过；无网络、无模型调用。真实 OS 越界验证（文件/网络/子进程/符号链接/受保护目录/本机管理 API/取消清理）按 MK-21 在本机统一执行。

<a id="adr-109"></a>
## ADR-109：确定性工具目录发现与版本绑定按需加载（2026-10-26，Accepted）

**状态：Accepted（2026-10-26；MK-16 纵向最小切片已实现并定向验证，API/UI 面板与持久加载回执仍按 [MK-16](../AGENTS.md#maka-upgrade-status) 进行中）。**

**背景：** [MK-16](architecture.md#mk-16工具发现与按需加载) 要求大工具目录先检索/浏览、选中后加载精确 schema，目录快照固定版本，MCP 重连后同名不能换实现，发现不等于授权。

**决策：** 新模块 `eventide/tool_discovery.py` 提供两个原语。`ToolCatalog` 是一次运行的冻结目录快照：目录项含名称、来源（`builtin`/`mcp:<server>`）、schema SHA256 版本指纹、描述、效果类别（readonly/effect）、执行环境、状态与禁用原因，目录版本指纹是对全部条目身份的规范 JSON 哈希；确定性搜索按名称精确/前缀/子串/描述四级排序稳定分页，空查询即浏览。`ToolDiscovery` 是运行内绑定：`tool_discover` 返回分页摘要，`tool_load` 校验同名工具与可选 `expected_catalog_version` 后返回绑定目录版本的 schema 回执，并把该 schema 注册进本运行唯一的 `ToolExecutor`（`register_discovered_tool`，schema 来自同一冻结快照，坏 schema 仍编译失败并拒绝调用）。

**接线与边界：** Host 在工具投影处（`host.py` 工具组装块）把 MCP 工具从默认广告改为按需投影：仅当存在 MCP 工具且非沙箱/只读档案时构建 `ToolDiscovery`，基础工具照旧直接提供，加载后的工具才进入模型目录与 handler；未加载的 MCP 工具既不在目录里也没有 handler，幻觉调用被执行器以 Unknown tool 拒绝。加载不触碰 PolicyEngine：readonly 按 MCP 声明进入受信只读集，effect 工具加载后仍走既有 ASK/审批，默认拒绝语义不变。快照按运行冻结，跨运行重建产生新版本，回执无法复活旧实现。

**取舍与边界：** 快照与加载回执当前在运行内存中，不持久化到事件日志，Workspace 级 HTTP 目录只列真实常备基础目录并明确标注 run-scoped 加载未持久化，不伪造已加载列表；沙箱/只读/plan 模式不开放发现（plan 目录本就过滤掉发现工具）。目录预算测试与已加载面板、加载事实持久化、同名来源竞争 UI 属后续切片。

**验证：** 新增 5 项定向测试覆盖 121 工具分页/空反馈、指纹稳定与漂移、回执与版本校验、未加载拒绝/加载后执行/加载≠授权、按加载状态投影；executor/contracts/tools/worker/mcp 关联 76 项及 host 26 项（3 项基线既有失败见 failure-cases MK16-BASELINE）通过，Ruff/Mypy(4模块) 通过。证据 `.task_outputs/maka-upgrade/tool-discovery/`。

## ADR-110：自然语言记忆输入采用确定性有限语法（2026-10-26，Accepted）

MK-18 的记忆抽取不引入付费模型或语义解析：`memory_intake.py` 只识别声明过的有限句式，识别不了的原文一律不保存。直接保存要求意图开头 + 长期标记词或既有结构化 `key=` 语法；裸“记住X”走观察→同会话重复→pending 建议的既有链路；“这次”等临时表述、否定、引文/工具内容、非开头意图全部拒绝或忽略。

取舍：宁可漏识别也不误存。用户可用结构化语法或记忆管理页补齐有限语法覆盖不到的表达；把“记住这个函数名”这类任务性表述误存为长期偏好的代价高于漏存。全局作用域必须原话出现明确词，未指定范围默认当前项目并在回执标注，用户可一键撤销。

来源绑定：记录 source 携带 `span:{start,end}`，与原用户消息逐字对齐；授权只来自 observe_run 读取的 user 作者消息事实，模型/工具输出无法自授 trusted。主题 key 用内容摘要稳定派生（`nl_` 前缀），同内容重发命中冲突而非静默覆盖。局限：中文分词式变体、复合句、意图不在开头的表达不识别；英文标记词有限；临时词检测只覆盖声明的短语集合。

## ADR-108：search_text 的有界 regex 后端与逐行核对证据（2026-10-26，Accepted）

regex 模式只依赖可探测的本机 ripgrep，缺失时返回明确能力错误而不是降级为字面匹配或自动安装；这是有意的能力边界，不是缺陷。Python `re` 不用于用户提供的 regex：其回溯没有可用的时间上界，而 Rust regex 引擎保证线性时间，配合整批 wall-clock 截止时间和输出字节上限、超时即 kill 并 reap，构成"实际执行时间边界"。rg 只是匹配预言机：它报告的每一行都必须与工具自己在工作区边界内读取并哈希的字节逐行一致才采信，任何不一致把整个来源按内容已变化排除——这同时封住了枚举与匹配之间的 TOCTOU 窗口，后端输出无法把边界外文本带进结果。

literal 模式保持纯 Python 且语义不变（行级命中、offset 计数、扫描/跳过上限、隐藏与链接规则）；大小写不敏感用 `re.escape` 的字面模式实现，因为 casefold 会改变字符串长度、破坏匹配位置。上下文行在文件内合并去重，命中与上下文用不同记号区分；每个展示来源携带实际搜索字节的 sha256，直接作为 edit_file 的 expected_sha256。空结果必须带扫描/跳过/超时/核对计数，不允许被读成全仓无匹配。沙箱 Docker 任务副本本轮仍不暴露 search_text，不做容器内正则的虚假覆盖声明。代价是 regex 模式引入外部进程依赖和一次额外文件读取；换来的是有界的执行时间和不出边界的证据链。源码定位 UI 与整体效果验收由后续切片承接。

## ADR-112：运行中补充的持久接收与单次模型边界消费（MK-06 切片 1）

**状态：Proposed（2026-10-26；切片 1 已实现并通过定向验证，结构化提问等待/回答属切片 2 未实现）。**

**背景：** [Maka 借鉴升级](architecture.md#maka-upgrade-plan) 要求真实运行中的任务能持久接收用户补充，在下一安全模型边界消费一次；已执行的工具不倒带，终态后的迟到补充不得自动续跑。所有状态只放唯一 Event Log；工具问题不等于权限审批，不建第二执行路径。

**决策：**

1. 补充只经可信本机 HTTP 路径接收：`POST /api/runs/{run_id}/supplements`（要求本机来源与 Idempotency-Key，复用既有 `mutate_run_action` write receipts/origin 语义；同键同正文重放返回原收据，同键不同正文 409）。收据事实 `interaction.supplement` 落唯一 Event Log，含 supplement_id、run_id、session_id、正文、body_version；正文 1–8,000 字符，空正文 422。
2. 接收时按投影 Run 状态定级：queued/running/waiting_for_user 记 `pending`；终态（completed/failed/interrupted）记 `late`。Store 的终态 Run 拒绝任何 run 标签新事实，因此迟到收据以 session 事实落账、payload 内仍固定 run_id 供作用域判定。
3. 消费点唯一：Host 主循环每步在 `context_builder.build` 之前读取本 Run 的 pending 补充，经新窄方法 `consume_supplement` 在同一事务内原子追加 `message.user`（带 `[User supplement for the running task]` 前缀）与 `interaction.supplement.consumed`（含 step）。消息序列天然位于已执行工具结果之后，不倒带任何工具；同 Session 串行保证单一消费者，崩溃时两事实同事务原子提交，不出现消费标记与消息事实半提交或重复注入；请求发出前崩溃时模型仍可能尚未收到消息。Host 不在其他任何位置读取补充。
4. 作用域隔离：`interaction_supplements(run_id)` 只匹配本 Run 的收据/消费事实；其他 Run（含同 Session 后续 Run）不消费、不投影。迟到补充保持 `late`，不自动创建、不排队、不恢复任何 Run；是否带入下一轮由用户显式决定。
5. 查询端点 `GET /api/runs/{run_id}/supplements` 返回收据/状态投影（pending/consumed/late 及消费 step）；刷新与迟到读取只读同一 Event Log，不重放写入。Vue `RunSupplementPanel` 独立组件挂 ChatView：发送走 `durableMutation` 幂等，late 明确提示不自动续跑，pending/consumed 按步数展示。

**取舍：** 复用 `message.user` 作为模型消费载体使上下文装配无需改动 context_builder，代价是补充在会话转录中呈现为普通用户消息（消费事实与收据提供追溯）。结构化提问的持久等待/回答需要审批式的等待接线，属切片 2，本 ADR 不声称完成。

**验证：** 后端 4 项定向（test_interactions.py：边界单次消费与 step 投影、同键重放/冲突/终态重放、迟到不续跑且不泄漏到后续 Run、空/超长/未知 Run 4xx）通过；新增前端 4 项组件测试通过；`vue-tsc --noEmit`、Ruff 全仓、Mypy（py310 目标）5 模块通过；chat-experience/memory/sandbox 既有 39 项回归通过。未运行全量、浏览器、容器或真实模型；测试迭代首轮失败已登记 failure-cases MK-06。

## ADR-111：持久自动恢复的领取式续跑（MK-08C 切片）

**状态：Proposed（2026-10-26；离线定向验证通过，真实模型/平台验收未做）。**

**背景：** 重启前 Host 把全部未完成 Run 一律关成 interrupted，用户必须手动 Continue；与"积极自动恢复"的目标不符。但盲目续跑会重放副作用、绕过审批/预算或用新配置扩大旧授权。

**决策：**
- 授权按接收冻结：每次 admission 在 `run.admitted` 持久化 `recovery_policy={"auto_resume": true, "version": 1}`；旧历史无此字段，永不自动升级，重启后仍停驻（手动 Continue 不变）。
- 判定是事实投影（`eventide/run_recovery.py`，纯函数）：控制类执行（沙箱/交付/评测/索引/worktree 控制/历史导入/Python 实验/连接探测）、未知工具结果、未闭合工具协议对、待审批、副作用缺 post-tool checkpoint、无冻结策略，一律不自动续跑。
- 文件系统证据仍须成立：当前 checkpoint 必须等于该 Run 最后一个已提交 `workspace.checkpoint`，否则停驻（`workspace_evidence_changed`），不把漂移文件当原任务继续。
- 恢复领取与新后继身份同一事务（`store.claim_run_recovery`）：先写 `recovery.claimed`（冻结 `resumed_as` 身份）再写 `run.interrupted`；`turns.continuation_of` 的 UNIQUE 约束与 claim 存在性检查共同保证重复 initialize/连续重启只能重建同一后继，不能产生两个续跑。
- 实际重派发延后到 `initialize()`（持久 Provider 配置已加载）：冻结的 provider/endpoint 与当前配置不一致、原显式模型参数不可复用、冻结 Skill 快照无法复现、或 admission 失败时，写 `recovery.blocked` 会话事实并保持停驻——后来的配置变化不扩大原授权。通过后走既有 Continue admission 与调度队列（原顺序、既有模型槽/根租约上限），不新建调度路径。
- 预算跨链不重置：续跑 intent 持久化 `recovery_of`，`ModelBudgetController.context` 与 `BudgetLedger._validate_context` 按 recovery 谱系上溯到原根 Run，run 作用域限额与项目/日预算跨重启延续。
- 后继 Run 经正常 `_recovery_facts` 配对旧协议史；已执行工具引用原结果，不重放；Web启动显式传入既有ApprovalBroker处理新ASK；没有审批处理器的开发接口仍默认拒绝。

**后果：** 停驻理由更精确（如源码漂移记 `workspace_evidence_changed`）；沙箱/交付等控制类恢复、未知工具只读核查（08D）、恢复状态 API/页面由后续切片承接。

## ADR-113：可读审批与待处理中心是纯事实投影（MK-07/MK-09 切片 1，2026-10-26）

**状态：Proposed（离线定向验证通过；真实多会话/多标签旅程与完整门禁未做，不作为最终验收）。**

**决策：** 注意力数据一律从唯一事件日志与既有绑定事实投影，不新增状态存储、不新增授权通道。`eventide/attention.py` 仅做只读派生：待审批取同 Run 内无 resolution 的 `approval.required` 事实（取消/中断不补发 resolution，因此过期审批保留在列表中并标记不可操作），失败/停驻运行取最新终态为 `run.failed/run.interrupted` 的 Run，待采纳候选取交付 `repair_status=passed` 且未 applied 的记录。去重键为 `approval:{approval_id}`、`run:{run_id}:{status}`、`delivery:{delivery_id}`。

可读审批从冻结参数与 `run_execution` 绑定派生命令/目标路径/cwd/执行位置/影响范围；网络访问与权限提升没有事实来源，永远显示"未声明"，不从命令文本推断。审批 `approval_id` 与原始 `arguments` 原样透传，展示转换不改变授权身份；批准/拒绝复用既有 `POST /api/runs/{run_id}/approvals/{approval_id}`，过期审批继续由该端点拒绝（404/409）。新增 `GET /api/attention` 与 `GET /api/projects/{project_id}/attention`（GET-only，纯读）；"待回答"类别当前无事件事实，明确声明不支持，不伪造。前端 `/attention` 独立页面 + 导航入口，Chat 主链路不改动。

**取舍：** 不按结果隐藏过期审批——保留事实并标注，比静默消失更可核对；同工作区源执行同时只能有一个写入任务，跨会话并行审批列表受既有串行约束约束，不做并行化扩展。MK-07 的 operation/revision/参数摘要绑定与批准前参数变化失效、MK-09 的待回答/核查不明类别与项目筛选 UI 属后续切片。

**验证：** 后端 5 项新定向（可读派生、过期审批保留且拒绝、失败运行列表、跨会话去重键、未知字段不猜测）+ `tests/test_api.py` 25 项回归通过；前端 3 项组件测试（可读事实渲染/失效按钮禁用、复用原审批端点并显示拒绝、空类别不伪造）通过；Ruff、Mypy（py310 目标 4 模块）、vue-tsc、Prettier 通过。日志 `.task_outputs/maka-upgrade/attention/`。无模型调用、无真实平台验证。

ADR-111补充：恢复领取后、initialize前再次退出时，下一进程从持久claim重建同一后继；普通worktree和snapshot在证据一致时允许恢复。当前仅恢复进程退出时缺终态的Run；正常close已记为取消的运行不会自动重启，后续需区分服务关闭与用户停止后再扩大范围。不得据此宣称所有服务重启均已自动恢复。

## ADR-120：以实际工具输出的结构化证据显示文件范围与搜索来源

- 状态：Accepted（源码证据展示切片；2026-10-26）
- 背景：从文本标题解析行号或重新读取路径，会混淆特殊文件名、历史输出和当前源码。
- 决策：在本次读取/搜索生成结果时一并记录版本与坐标，沿用 ToolOutput/Event Log/Run 投影；保留文本开发接口与模型有界正文，不增加另一状态表或自动文件访问。Vue 仅显示历史证据与明确不完整信息，缺元数据时说明缺失。
- 取舍：元数据增加有界事件体积；搜索显示行与原有分页结果相同，不保存全文件副本。它证明读取来源和范围，不能保证模型理解正确或源码持续不变。原生与Docker元数据接线及最终真实平台验收继续单列。
- 证据：83项后端、36项前端、真实API构建页面1条（原文件变化后刷新历史、read范围、search命中/上下文、320px）、vue-tsc、定向Ruff/Mypy通过；原始日志 `.task_outputs/maka-upgrade/source-evidence/`。

## ADR-121：在模型适配器入口记录无正文的输入清单

- 状态：Accepted（MK-04 切片，2026-10-26）
- 背景：model.request 是逻辑步骤记录，重试前长期记忆可能被撤销；不能用第一次构造的输入代表每次真实尝试。
- 决策：在最后一次记忆资格检查之后、调用 provider adapter 之前，同步追加 model.input_manifest；期间没有 await，不打破“撤销后不再注入”的保证。绑定 request_id/attempt_id，记录系统/消息/目录指纹、工具名称、结果调用引用、字符组成；规则加载来源及截断摘要随 context.configured 记录。复用 Event Log，不复制原始提示词/源码/附件/工具参数。
- 取舍：这是交给适配器的 provider-neutral 输入，不能宣称供应商已接收、适配器最终HTTP字节完全一致或模型已理解；字符组成不等于Token归因。辅助请求没有对应根规则配置时显示未记录。目录规则继承、摘要正文前后对照仍待后续。
- 验证：实际 Provider 捕获与每次清单的 hash/组成一致；重试身份分离、记忆撤销导致系统指纹变化；分页无重叠、未知Run和边界参数；页面按需加载/错误重试/迟到响应隔离与built真实API旅程。见 AGENTS.md 和 `.task_outputs/maka-upgrade/input-manifests/`。
## ADR-114：Run 工具目录以冻结事件为唯一投影事实（MK-16 切片 2，2026-10-26）

**状态：Proposed（离线定向与端到端替身验证通过；真实 MCP 旅程、浏览器与完整门禁未做，不作为整项验收）。**

**决策：** 一个 Run 的工具目录在该 Run 的 执行目录组装时由 Host 冻结为同一 EventLog 中的 `tool.catalog.frozen` 事件：来源/名称/描述/目录版本/每项 schema 版本/只读或副作用/环境/可用状态与精确 input_schema（超 32k 字符的 schema 只留摘要标记与字符数，不静默截断成看似完整）。`tool_load` 的成功与失败由 ToolDiscovery 自己的加载路径经 Host 回调写入 `tool.loaded`/`tool.load_failed` 事实；MCP 汇报的任何同名 metadata 不是可信来源，永远不进入这两个事件。

`GET /api/runs/{run_id}/tools` 与 `/tools/{name}` 是纯只读投影：仅折叠上述持久事实，高水位取最后一条冻结事件；回执必须同时绑定目录版本与条目 schema 摘要才算已加载，不匹配的回执保留为事实但不套用，并显式计数提示。没有冻结事实的 Run（旧历史或无按需目录）返回 `catalog_frozen=false` 加说明，绝不重新枚举当前 MCP 伪造历史；重开 Store 或 MCP 已变化时旧 Run 的投影逐字节不变。分页/过滤在投影层完成，列表不内联 schema，精确契约由详情端点按需提供。Vue 聊天流内 `RunToolsPanel` 提供搜索/来源/状态筛选、精确参数契约、版本与失败记录展示，并在面板级常驻"展示与发现不等于授权"；不提供任何执行入口，加载/执行语义与权限全部仍由 ToolExecutor/PolicyEngine 既有边界决定。

**取舍：** 冻结进事件而非独立表，保持唯一事实源且重启/迁移天然携带；代价是快照占事件体积（已按条目有界）。不把"已加载"提升为权限——它是模型可见性状态，授权仍按逐次调用判定。旧 Run 不补冻结事实，宁可显示缺失也不伪造。

**验证：** 新增 `tests/test_run_tools_directory.py` 4 项端到端（真实 Host + 假 MCP 管理器）：首轮请求不含未加载 schema、`tool_load` 后下一请求携带冻结 schema、冻结事件与加载事实绑定同一目录版本、重开 Store 且 MCP 换catalog 后旧 Run 投影与详情不变而新 Run 冻结新目录、未加载调用被拒且 auto 模式下加载后的写工具仍经审批拒绝不执行、无目录 Run 明确缺失。既有 discovery/tools 共 10 项通过；前端 6 项组件测试（状态徽标/契约展开/筛选/缺失与失败明示/切 Run 晚到响应隔离/有界分页）通过；Ruff 全过、Mypy py3.10 三模块、vue-tsc、Prettier 通过；OpenAPI/TS 已再生成。开发期失败与复验见 failure-cases MK16-DIR-ZCODE。日志 `.task_outputs/maka-upgrade/coding-resume/`。无付费模型调用。
## ADR-117：恢复详情是原子切面投影，动作只复用既有受控端点（MK-08F/MK-19 切片，2026-10-26）

**状态：Proposed（离线定向验证通过；未做真实模型/浏览器全量旅程，不作为最终验收）。**

**决策：** 恢复详情一律从已提交事实投影：`store.recovery_snapshot` 在同一把 Store 锁内取 Run 身份、Run 事件和以该 Run 为 source 的 `recovery.blocked`/`recovery.claimed` session 事实，保证 claim、终态与 blocked 不会跨版本混读；`recovery_center.build_recovery_view` 是纯函数，停止分类（模型重试等待/自动续跑/用户停止/预算/未知结果/连续失败/服务重启等）、已记录执行进度摘要、未知与待审批、原/后继关系全部由事件推导，不新增状态存储。admission 缺冻结 `recovery_policy` 的旧历史显式返回 `history_available=false`，不猜测可恢复性。后继当前状态用 `get_run_summary` 实时读取并在页面与历史切面（`high_water`）分开呈现。

`available_actions` 由服务端按当前调度事实计算并给出原因；页面只渲染服务端判可用的按钮，动作仅复用既有 `POST /api/runs/{id}/cancel` 与 `POST /api/sessions/{id}/continue`（端点自身复核）。本切片不实现 `POST recovery/actions`，不提供自动重试或"忽略风险重跑"；未知工具结果与失败运行不宣称为可恢复。

**取舍：** 唯一后继由既有 `recovery.claimed` 事务不变式保证，投影只如实展示（出现多条 claim 时标注异常注记）而不修复；blocked 事实按 `source_run_id` 绑定，同 session 其他运行不可见，避免恢复状态串线。不在诊断报告或恢复页触发模型、文件或审批。

**验证：** `tests/test_recovery_center.py` 4 项真实 EventLog/HTTP：重启 claim 后唯一后继且重复读取不改事件数、blocked 为按 source 绑定的 session 事实、用户停止/预算/未知结果的分类与"无自动重试"断言、旧历史缺策略显式标注；既有恢复套件 7 项回归通过。前端 5 项组件测试（事实与后继链接纯查询、预算/用户停止无自动重试按钮、未知结果不可继续、服务端可用时才出现停止并在动作后重读、切 Run 丢弃迟到响应）；受影响 chat/navigation/diagnostics/model-retry 套件 33 项通过。Ruff、Mypy（py310 目标 3 模块）、vue-tsc、Prettier、git diff --check 通过。日志 `.task_outputs/maka-upgrade/recovery-center/`。无模型调用；MK-08 整项仍进行中。

ADR-117集成补充：恢复页Continue必须携带expected_run_id并在实际接收锁内复核；未知网络结果复用sessionStorage中的原幂等键，不按每次点击新建意图。高水位包含按源绑定的Session恢复事实，服务正常关闭的claim也可展示。动作可用性不是源码/配置/预算的预先验收；这些仍在接收路径核查。built实际API恢复旅程已通过1条，真实模型/全平台和整仓门禁仍待。

## ADR-115：完整命令日志是绑定身份的独立脱敏备份（MK-05 切片 1，2026-10-26）

**状态：Proposed（定向验证通过；真实浏览器旅程与完整门禁未做，不作为最终验收）。**

**决策：** 超出模型预览的本机 Shell/Skill 输出在 `ToolExecutor` 内经既有命令执行器观察口捕获两流，先跨块脱敏（复用 `SafeTextBuffer` lookahead 与 Host 语义清洗）再写入状态根 `command-logs/<run哈希>/<artifact>/`，路径只接受事件事实派生的 32 位十六进制 artifact 身份，不接受任何客户端路径。唯一 Event Log 仍是权威：`command.log_started/recorded/expired/cleanup_failed` 记录身份、received/stored 字节、SHA-256、字符数与诚实 capture_state；脱敏正文不能从这些元数据重建，磁盘文件是独立有界备份，读取时必须校验摘要与字符数，不一致返回 `corrupt` 而非部分内容。

配额（单流 1 MiB/Run 24 MiB/全局 256 MiB）与保留期（终态后 7 天）只影响日志，不影响命令：超额继续排空进程输出、标记 `quota_exceeded` 及省略量；写盘失败、fsync 失败、跨块密钥过长（`redaction_unavailable`）都不改变真实退出码，也不让命令看似失败。取消等待真实进程回收后以 `interrupted` 收尾；超时以命令结果的timed_out/退出码为准，capture_state=complete只代表已采集全部实际排出的字节，不代表命令成功；服务重启把未收尾捕获标为 `process_restart`，rename+目录 fsync 保证 sealed 文件原子可见。分页按 Unicode 字符（单页上限 12,000）经 `GET /api/runs/{run_id}/calls/{call_id}/command-log` 提供；未知 Run/call/流 404，非法分页 422，读取失败保留 200+显式 availability——"日志曾存在"本身是运行历史。Vue `CommandLogPanel` 挂 `ToolActivity` 的 `command_log` metadata，按需分页并静态声明覆盖范围：仅本机普通 Shell 与 Skill 脚本；容器/沙箱执行不接入此采集，也不降级为宿主采集。

**取舍：** 不复用验收归档（AcceptanceOutputs）——授权模型不同（验收调用显式开启 vs 普通 Shell 自动采集），混用会扩大任意写日志的面；不为 MCP/文件工具建通用输出存盘（MK-05 明确排除）；日志压缩、按流采样和 MK-10 受管后台命令身份属后续切片。

**验证：** 后端 11 项定向（中段标记分页回读、跨块密钥不落盘、取消中断收尾、超时诚实事实、流配额显式截断、重启恢复、Skill argv 路径、拒绝调用 not_started、过期显式不可用、HTTP 全栈分页/404/422）通过；前端 4 项组件测试（按需分页、诚实事实、显式不可用、请求错误与迟到响应隔离）通过；Ruff、Mypy（py310 目标 6 模块）、vue-tsc、Prettier 通过。日志 `.task_outputs/maka-upgrade/command-logs/` 及 `coding-resume*/`。无模型调用、无真实浏览器或完整门禁。

ADR-115集成补充：普通命令流回读进入既有read_tool_result，同Session权限不变；返回真实availability/capture_state，不因日志缺失重放命令。每Run执行器组装时取当前冻结Provider密钥，跨块脱敏包含从持久配置加载的密钥；错误处理只捕获磁盘/SQLite边界异常，未知程序错误保留。已补真实Host长输出→模型中段回读、持久密钥分片、跨Session拒绝与磁盘故障不改退出码，built页面1条通过。

## ADR-118：区分正常关服与用户停止，恢复领取不改写旧终态（2026-10-26）

**状态：Accepted（本切片定向验证通过；整体验收与真实模型仍待MK22）。**

Host.close在取消执行前为每个未结束Run写入绑定确切run_id/shutdown_id的host.shutdown_started事实；正常关服记录service_shutdown终态。用户cancel_run先持久化run.cancel_requested，再取消实际所有者；持久化失败明确返回错误，不能假装停止成功。停止事实优先于关服，未知工具结果仍保持未知，不因关服被当成可恢复。

下一Host扫描关服终态，复用已有恢复判定与源码checkpoint、配置/Skill/预算边界；安全时在单事务写Session级recovery.claimed，保留原Run终态。唯一后继与连续重启沿用原claim；不重置父根预算。领取后、接收前及接收等待期间的用户停止都在首模型派发前复核；已创建后继的旧Run取消请求拒绝并要求使用后继身份，不把停止旧记录宣称为停止新执行。恢复接收携带expected_run_id，避免继续到被用户替换的新任务。

关服记录写入异常仍完成任务取消与资源清理，随后向调用者报告异常；不会吞掉磁盘失败。恢复中心原子切面包含精确绑定的用户停止事实，仍保留已领取记录及其高水位，展示用户停止而不是继续显示自动恢复。

验证：32项恢复/接收/投影组合和101项Host/队列/副本/Worker关联通过；补充停止投影后的22项复验、Ruff/Mypy4模块通过。覆盖真实Host关闭/重开、连续恢复、领取后再次退出、用户停止竞态、不同Session、新Run不继承旧关服标记、持久化失败、原预算归属和Web审批处理器。日志.task_outputs/maka-upgrade/graceful-review；外部ZCode实现初稿，主代理修正历史断言、接收竞态与页面事实范围。未知工具核查与控制任务恢复不在本切片冒充完成。

## ADR-119：版本化模型能力目录与三种验证状态分离（MK-14，2026-10-26）

**状态：Proposed（离线定向验证通过；真实供应商端点与完整门禁未做）。**

**背景：** 保存 Provider 配置、连接探测通过、真实工具调用验证是三件不同的事；此前设置页只有能力 source（documented/declared/unknown），没有目录身份、来源版本与验证状态，恢复链路复用冻结 generation 但能力校验始终取当前配置。

**决策：**
- 身份是 Provider + 协议 + 规范端点 + 模型（`eventide/model_capabilities.py:endpoint_identity`）。规范端点折叠尾斜杠与显式默认端口，不同路径/主机/模型互不继承；连接验证按原始 `endpoint_hash` 字节精确匹配，同样不跨端点继承。
- 能力值只来自两处：`eventide/generation.py` 内代码预设映射（`capability_preset`，附代码来源引用；逐项真实验证日期保持未知，不臆造供应商文档 URL 或记忆规格），或用户显式声明（`source="declared"`，`declaration_ref` 绑定 model_identity，覆盖字段相对预设列出差异）。未知能力保持 `null`（context_window/tools_supported/streaming_supported 仅信息性，不参与请求校验），不从 Provider 名称猜测，不新增自动供应商路由。
- 三状态分离：`config_saved`（持久 Provider 记录解析到同一端点身份）、`connection`（探测 Run 终态投影：passed/failed/unknown/never_probed，附 run_id 与原因；扫描只发生在设置 API 读取投影，不进 admission 热路径）、`tool_verification`（本切片恒为 `not_verified`，连接通过不冒充工具验证）。
- 接收冻结：`run.admitted` 持久化 `capability_ref`（身份、来源、预设键、目录版本、有效能力）。Continue 复用旧请求时按冻结能力校验，之后默认/目录变化不能改写历史请求或复活被新上限拒绝的参数；冻结引用无效时 Continue 明确拒绝，不静默回退当前配置。旧历史无 `capability_ref` 保持原行为。冻结能力中 `None` 字段省略，避免与脱敏规则冲突（见 MK14-ZCODE-01）。
- API/UI：`GET /api/generation/defaults` 与 session generation 响应附 `capability` 记录与连接验证（验证证据在 `config_hash` 计算之后附加，不改变可编辑配置的并发语义）；GenerationEditor 增加能力卡，迟到响应由既有 revision 守卫隔离。

**取舍：** 目录版本是代码常量（当前 `1`），预设集合变化时整体升级，不做每条目独立版本迁移；连接验证投影全表扫描 `run.started`（探测 Run 稀少，设置页读取频率低），量大后再加索引。真实供应商工具/流式验证属后续切片，当前不宣称任何端点已通过工具验证。

**验证：** `tests/test_model_capabilities.py` 8 项（异端点不继承、规范端点、未知不猜、预设来源版本、显式覆盖可追溯、冻结引用不被新配置改写、探测三状态与工具验证分离、API 卡片与 config_hash 隔离）通过；受影响既有回归 84 项通过；前端组件 14 项（含能力卡渲染与模型切换迟到响应不串）与 vue-tsc 通过；Ruff/Mypy 见提交说明。日志 `.task_outputs/maka-upgrade/model-capabilities/`。

ADR-119集成复核：历史成功请求不能验证所有能力，preset.verified_at保持null。身份hash保留query/auth路由区别，公共地址去除凭据/查询参数；IPv6保留括号。12项目录专项、53项恢复/生成组合、18项前端、11项分发、built真实API页面1条及类型/构建/Ruff/Mypy通过。用户声明能力编辑页、目录过期与真实工具/流式验证仍待，不把本能力卡当完整目录交付。
## ADR-116：旁枝提问是只读快照上的独立 Run，回传是唯一写回通道（MK-17 切片 1，2026-10-26）

**状态：Proposed（离线脚本模型定向验证通过；真实模型与完整门禁未做，不作为最终验收）。**

**决策：** 旁枝提问复用只读 worker 执行边界，不新造执行路径：创建时在共享 Git 协调下冻结源 Workspace 快照并绑定主 Run 事件高水位，独立 Session/Run 以 `ReadonlyProfile(role="side_question")` 执行；写类工具不在其目录，全部工具调用仍经 ToolExecutor/PolicyEngine，只读不是 prompt 承诺而是能力边界。预算与模型槽挂 `parent_run_id` 共享父 Run，`continuation_of/recovery_of` 不复制——旁枝不是主任务的恢复或续跑。证据只来自高水位内的已保存事件（目标/最近输出/工具活动，脱敏有界）并明示排除计数，不把创建后的文件冒充当时事实。主 Run 生命周期完全不受旁枝影响：不排队、不取消、不自动注入。回传必须经既有 supplement 接收路径显式触发，绑定用户所见答案的 SHA-256 指纹；同一旁枝最多注入一次，主任务终态记 late 不自动续跑，超长正文显式标注截断而非静默丢弃。创建事实为 Session 级事件——终态 Run 不能追加 Run 级事实是商店完整性规则，不绕过。快照临时目录由旁枝 Run 任务回调在终态（含 Host 关闭取消）删除，不依赖 GC。

**取舍：** source/worktree旁枝各复制一份短期快照；Plan复用实际已校验的持久只读快照，所有者负责保留；旁枝答案先落独立 Run 再由用户决定回传，不做自动证据流；UI 先轮询完成态，不做旁枝独立 SSE，主链路 SSE/重连不改。刷新到新快照、选择文本提问、来源跳转属后续切片。

**验证：** `tests/test_side_questions.py` 7 项：主/旁枝脚本 Provider 真实并发（旁枝写被拒、只读目录恰好5工具、快照身份入 started 事实）、共享账本含旁枝 attempt、显式回传一次且主边界消费一次、取消旁枝主任务完成、HTTP 迟到回传 409/幂等/late 不续跑、校验与畸形 id 404、超长答案显式截断标注；快照目录删除有 glob 文件证据。前端 `tests/side-questions.test.ts` 3 项（提问不触主任务、回传绑定所见文本/迟到/去重、停止按钮仅活动态）+ ChatView 回归 5 项，vue-tsc 通过；Ruff、Mypy（py310 目标 2 模块）通过。日志 `.task_outputs/maka-upgrade/coding-resume4/`（2026-10-26 续做会话全量复验：后端 7 项、前端旁枝 3 项 + ChatView/执行回归 24 项、vue-tsc、Ruff、Mypy py310 目标 2 新模块 + host/store/api 3 个改动模块均退出 0；首次验证日志随上一会话丢失，resume2 只剩崩溃 runner 会话记录）。无付费模型调用，未重跑全仓/构建产物；OpenAPI/TS 类型再生成留主集成统一收口。

ADR-116集成补充：主代理修正真实执行副本选择、工具错误正文、继续任务的原目标继承、响应迟到隔离和取消前未启动的清理。临时副本由任务回调所有，去除全局注册表，清理OSError写入Session事实并报告；接收失败收尾已创建Run。并发接收用Host短期锁，全服务最多4活动旁枝，共享原模型槽/预算；控制类和旁枝子任务不允许再次创建旁枝。显式回传必须completed且HTTP答案指纹必填；文件/事件切面分别说明。58项后端关联已逐项通过（1旧时序测试修复后所在5项复验）、35项前端、11项分发、1条built真实API浏览器与Ruff/Mypy5模块/vue-tsc通过。整项MK17仍有追问/更新背景/来源跳转未完成，真实模型按MK22。


## ADR-126：摘要对照绑定已保存版本与历史输入，不从当前源码补证据（2026-10-26）

**状态：Accepted（本切片定向验证通过，整体验收与真实模型效果仍按MK22）。**

版本列表与详情复用原context_checkpoints。按Session+checkpoint身份查找，原事件只读截至covered_seq，验证完整终态与source_digest、policy_version，再用ContextBuilder共用summary_history投影重建。原context.summary_claimed.prefix_hash可证明重建输入与原摘要请求一致；缺失claim或规则变化只显示重建内容，明确不保证历史输入一致。不会从当前源码拼接看似原始证据。

结构化摘要引用重新校验；匹配不代表结论正确。旧纯文本、无效引用、缺历史分别显示，不悄悄换成新摘要。摘要显示上限24k字符，消息原文按身份/索引/字符分页，全部纯文本渲染；用户可并排比较两个版本并回读引用。既有摘要生成/采用/预算机制不变，只将私有_summary_history命名为可复用summary_history。这个页面不能证明某次后续模型请求使用了某摘要，逐attempt关联需另加真实派发事实，不作推断。

验证：真实Host历史经ContextBuilder生成并保存摘要，API重建hash与实际捕获的摘要请求逐字节匹配；后续会话变化不改旧版本。42项后端、6项组件、11项分发及built真实API页面1条（原文回读、刷新、320px）通过，Ruff/Mypy4模块/vue-tsc通过。legacy/缺claim/投影变化/源摘要不匹配、分页/跨Session身份及HTML字面显示覆盖；无付费模型。


<a id="adr-123"></a>
## ADR-123：结构化提问先持久接收，再唤醒精确工具调用（2026-10-26）

状态：Accepted（当前进程等待与持久历史切片；跨进程等待恢复仍 Proposed）。

问题与答案复用runtime_events，不新建任务状态库。ToolExecutor限定作用域的ContextVar提供真实call_id，避免按工具名称猜测或给任意handler注入保留参数。Host等待Event只用于唤醒，权威答案必须来自已提交的同question/Run/revision事件。模型槽在等待前已释放，源/副本所有权不释放。回答事务复核明确停止、终态、关闭和重复内容；答复不授予工具权限，也不创建后继Run。

范围：本机源/工作副本与Plan主任务，子Agent/交付Writer/Docker暂不提供。正常关闭保留关闭记录，进程中断后的孤立问题过期；不在本切片改写恢复主链以伪造未完成工具结果。跨进程恢复将来需要冻结调用及原输入、预算、工作区状态的完整续接设计；现阶段不以默认答案绕过。

外部ZCode提供草稿，主代理发现并修复自由文本校验、事务外状态检查、缺调用身份、取消清理和缓存回执唤醒问题。验证覆盖真实Host+HTTP、停止、同键/内容冲突、Plan、秘密脱敏、关服后原身份保留与独立审批；网页组件和built真实API旅程见AGENTS及questions-review日志。未调用真实供应商做产品效果验收。

<a id="adr-122"></a>
## ADR-122：任务图使用唯一事实日志及覆盖准备阶段的节点所有权（2026-10-26）

状态：Accepted（只读图运行切片；完整MK20尚未验收）。

图定义、启用、领取、完成、取消均追加到既有RuntimeStore的Session事件；单事务检查依赖和全图容量，不加schema或graph数据库。进程任务仅持有准备/等待所有权，普通advance不回收准备中的claim。每节点沿既有Host只读执行，实际副本/冻结模型和端点/父预算不另造路径。图与旁枝共享4个活动名额，依赖结果是有界、标明来源的观察资料，不授予权限。

取消冻结图后等Provider和快照真实收尾，重复取消不中断清理；关服保留系统事实，不冒充用户停止。重启按已提交的子Run终态收尾孤立claim，未完成节点停驻且不重派发，写协作和跨重启节点续接仍待后续。本切片没有模型自主拆图工具，不将保存DAG当作自主写入协作已完成。

外部ZCode草稿未集成，当前窗口模型重写运行切片；主代理负责后端与集成，当前模型协作代理负责页面。定向验证与失败修复记录见AGENTS及failure-cases MK20-CURRENT，真实供应商效果仍按MK22。

## ADR-125：同批可信读取共享调度槽，取消等待实际执行与事实收尾（2026-10-26）

状态：Proposed（定向机制切片，最终集成/浏览器/模型验收仍待MK22）。

只有精确内置文件读取、枚举和搜索函数可进入连续只读组；同名自定义handler和MCP声明不能授权并行。单项读取同样取得全服务4槽之一。写入和依赖Runtime状态的工具是屏障，保留原顺序；调用仍经过ToolExecutor/PolicyEngine。结果按模型原请求顺序保存，先完成的线程不能抢先改后续输入。

批次协调任务拥有准备、执行和落库；停止时先无await地取消全部尚未获得槽位的调用，再等已派发同步工作结束和结果保存。重复停止不打断收尾，不靠取消to_thread协程声称线程已经退出。单项失败/预算/unknown只阻止后续未执行组；同组其他真实结果照常落库。所有执行事实仍进唯一runtime_events，临时Task/Semaphore仅表示进程所有权，不成为第二状态源。

增加tool.started区分已获槽与prepared等待，批次身份随原工具事件及Run/API/SSE流转，页面显示每项真实状态；无独立批次启动端点、用户可调并发或额外线程池。Docker和原生不同实现保持串行；跨文件读取不承诺原子快照，同组读取来自各工具实际读取时刻。

ZCode提供未验收草稿；当前窗口模型重做取消与落库收尾、补页面和确定性测试。原6ab4029已有5项失败循环/读取头回归在独立完整导出复现，未夹带修复；本切片证据和待集成检查见AGENTS及failure-cases MK11-CURRENT。

