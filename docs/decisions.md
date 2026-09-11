# Eventide 架构决策

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

**状态：Accepted**

**背景：** 当前产品面向单机调试和演示，不需要独立前端构建链或复杂状态管理。

**决策：** 使用 FastAPI 托管原生 HTML、CSS 和 JavaScript，通过 REST 与 SSE 访问 Runtime。

**影响：** 安装和运行不依赖 npm；UI 保持轻量；账号、租户、复杂路由和前端框架不进入当前版本。

## ADR-010：模型密钥使用独立主密钥加密

**状态：Accepted**

**背景：** Web 保存配置需要跨重启恢复，但 API Key 不能以明文进入 SQLite、HTTP 响应或日志。

**决策：** 使用 Fernet 加密数据库中的 API Key。主密钥来自 `EVENTIDE_SECRET_KEY`，未配置时生成 `.eventide/secret.key`；配置变更和连接检查只接受回环请求。

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

## ADR-013：Event Log 是唯一运行事实源

**状态：Accepted**，替代 ADR-006。

**决策：** runtime_events 追加保存消息、工具、审批、usage 和终态。Messages、Runtime State 和 Context Builder 只从该日志投影；身份表不保存第二份运行状态。上下文 checkpoint 是带来源 digest 的有损投影，不改变日志。

**影响：** 增加版本化 schema、不可变触发器和唯一终态约束。应用不会把旧 schema 直接当作当前数据库打开或自动删除；v0.2 数据通过 ADR-024 的显式只读导入迁移。TraceStore 名称和常用入口保留兼容，JSONL 输出 canonical 事件，旧 SSE 名称通过适配提供。

## ADR-014：RuntimeHost 拥有 Workspace 执行权

**状态：Accepted**，替代 ADR-002。

**决策：** 一个状态根由一个进程内 Host 持有 OS 文件锁，Workspace 绑定规范项目目录，Session 永久绑定 Workspace。同 Workspace 全 run 互斥，不同 Workspace 可并发。Provider 配置属于 Host；MCP 和指令按 Workspace 解析。

**影响：** 默认状态移到用户级目录；serve 与临时 CLI Host 不能同时打开同一状态根。AgentRuntime 为兼容门面。后续同项目并行写作须经独立 Git worktree；常驻 Host 协议和 Agent Graph 延后。

## ADR-015：中断检测与用户主动 Continue

**状态：Accepted**。

**决策：** 重启补写 interrupted，Session parked。仅用户主动请求、Git 可见源码 checkpoint 一致且无未知副作用时创建关联旧 run 的新 turn/run。未知 Bash/MCP/写工具不自动重试；只读未完成调用明确 abandoned。

**影响：** 不承诺指令级恢复、ignored 文件或宿主机全状态恢复。无 HEAD、非 Git、submodule 或无法采集证据时拒绝 Continue。旧兼容调度和协作工具不进入生产 Host 默认工具目录。

## ADR-016：Web 使用语义化工作投影

**状态：Accepted**。

**背景：** Runtime Event Log 是运行事实源，但 canonical event 粒度不适合直接作为用户界面。逐项展示模型请求和工具事件会把工作台变成日志查看器，掩盖 Workspace、Run 和恢复生命周期。

**决策：** Web 保留 RuntimeStateProjection 作为状态事实来源，在其上增加只读的 Semantic Work Projection。该投影按工具证据和事件边界将多个事件聚合为 Preparing context、Exploring workspace、Modifying workspace、Running validation、Checkpoint、Approval、Parked 和 Result 等 Execution Blocks。Model request/response 默认只进入 Inspector；retry、error、approval 和 interruption 等影响用户理解的事实才提升到主 Narrative。

**影响：** Execution Blocks 是可重建的展示状态，不写入 SQLite，也不改变 RuntimeHost、RuntimeStore、ToolExecutor 或 PolicyEngine 边界。无法由事件可靠证明的文件数量、验证结果或修改行为不得由 UI 猜测。Web 保持原生 HTML/CSS/JavaScript，不引入大型前端框架。具体工作记录组织和证据展示由 ADR-017 细化，普通 checkpoint 不再作为独立主视图片段。

## ADR-017：Session 作为持续的工作记录

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

**背景：** 仓库已有 `skills/*/SKILL.md` 和进程全局 legacy loader，但 v0.3 Runtime 只读取根 AGENTS.md。把所有 Skill 全文提前塞入 system prompt 会浪费上下文，也无法稳定表达每次 Run 实际看到的 Skill 版本。

**决策：** 每次 Run 从 Workspace 根目录发现并快照直接子目录中的 `SKILL.md`。system prompt 只暴露 Skill 名称和简介；存在有效 Skill 时动态注册只读 `load_skill`，由模型按需取得完整快照。目录和 manifest 解析不得逃出 Workspace。`context.configured` 记录 Skill 数量与包含内容摘要的目录 hash，加载操作继续使用 canonical tool 事件。

**影响：** 不新增数据库 schema 或进程全局 Skill 状态；不同 Workspace 的目录彼此隔离，Session working directory 不改变查找根。运行中对 Skill 的修改只影响下一次 Run。旧 `eventide.memory.skills` 保留兼容，但生产 Host 不导入它。

## ADR-030：大型工具结果使用无损日志和分页请求投影

**状态：Accepted**，扩展 ADR-018。

**背景：** ADR-018 只在整个请求超预算后折叠较早结果，并固定保护最近三条。单次 Shell、MCP 或自定义工具可以返回接近完整 context limit 的正文；当它位于当前 turn 的受保护尾部时，摘要和旧结果折叠都无法避免 `context_overflow`。把完整正文反复发送给模型也会让大型任务快速消耗上下文。

**决策：** canonical `tool.completed` 继续保存执行器返回的完整正文。ContextBuilder 对超过 12,000 字符的结果始终生成有界头尾预览，预览携带 run_id/call_id；Runtime 提供只读、Session 隔离的 `read_tool_result`，按字符 offset/limit 从 Event Log 读取任意页，单页上限 12,000 字符。若请求仍超预算，再沿用 ADR-018 的较早结果折叠。预览与折叠都记录 `context.trimmed`，不写回事件正文。

**影响：** 最新的大结果不再单独占满请求，模型仍能按证据需要无损回读具体区间；审计导出、工作记录和默认 MessagesProjection 保持原文。分页身份必须同时匹配当前 Session、Run 和 call，不能跨 Session 探测结果。不引入外部 artifact 文件、数据库迁移或自动任务拆分；TodoWrite/Task System 属于后续调度层工作。

## ADR-031：Session 计划以事件快照驱动执行

**状态：Accepted**。

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
