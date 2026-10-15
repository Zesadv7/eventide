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

RuntimeHost 是进程内唯一执行 Owner，持有状态根的 OS 文件锁、RuntimeStore、Provider 配置、SessionManager 和 Workspace 锁。CLI、FastAPI 和 Eval 直接复用 `RuntimeHost`；不再保留旧 Python 门面。首版不含后台 daemon 或 IPC client：同状态根已有 Host 时第二个进程失败。进程终止由 OS 释放文件锁。

Workspace 保存稳定 ID、规范路径、Git 根、名称和创建时间；Git 子目录统一绑定仓库根。Session 的 Workspace 绑定不可改变；工具 cwd 只由该绑定解析。同 Workspace 全 run 串行，不同 Workspace 可并发。Host 以 run_id 索引活跃执行，服务关闭或用户取消时先停止异步 Provider、MCP 与 Shell 子进程；同步线程工具结束后才释放 Workspace 所有权。

主要实现：`host.py` 管理生命周期与 Agent loop；`workspace.py` 管理路径、锁和 Git evidence；`store.py` 保存身份与事实；`projections.py`、`context_builder.py` 负责投影；`skills.py` 负责 Workspace Skill 的发现、快照和按需加载；`task_plan.py` 定义有界计划、依赖校验和紧凑提示；`worktrees.py` 提供注入 Workspace/状态根的 Git checkout 生命周期；`attachments.py` 管理 Session 附件生命周期、文本内联与多模态引用水合；`run_policy.py` 以纯函数定义每次 Run 的模式策略（plan/agent）；`model_catalog.py` 提供探测式模型列表辅助；`rag/` 负责 Workspace 文本切块、SQLite 混合索引、检索工具与离线检索评测。生产基础工具位于 `tools/runtime_catalog.py`，动态 Runtime 工具由 Host 注入，不导入 legacy 注册表。

## 存储与事件契约

默认用户状态根中包含 `runtime.sqlite`、`host.lock` 和按需生成的 `secret.key`。EVENTIDE_STATE_DIR 优先；SQLite 使用 WAL、外键和 schema_migrations。当前 schema 版本为 3，版本 1/2 在同一 v0.3 数据模型内自动升级；未知版本和直接作为 RuntimeStore 打开的未版本化旧数据库明确拒绝，不自动删除。`migrate-v02` 通过独立只读连接校验旧 TraceStore schema，再把数据以单个目标事务导入当前库。

| 表 | 职责 |
|---|---|
| workspaces | Workspace 身份、规范路径、Git 根 |
| sessions | 身份、固定 Workspace/working directory 绑定、显式标题、归档状态、创建时间 |
| turns | 用户回合身份、session、continuation_of |
| runs | 执行身份、session、turn、开始时间 |
| runtime_events | 唯一运行事实源 |
| context_checkpoints | 可失效的摘要投影及覆盖证明 |
| provider_config | Host 级模型配置和加密凭据 |
| schema_migrations | 已应用 schema 版本 |

RuntimeEvent 包含 event_id、session_id、严格递增 session_seq、turn_id、run_id、ts、type、role、author、payload、partial、schema_version。SQLite 事务分配 session_seq；唯一索引防止重复序号，触发器禁止事件 UPDATE/DELETE。一次 run 的第一个终态胜出；终态后的普通事件拒绝写入。partial 不进入消息、状态投影，且不能作为终态。

身份表不保存独立运行状态。消息与状态查询每次折叠事件，不依赖不可重建的缓存或 messages 表。`RuntimeStore` 是唯一存储入口；`append_message()` 仅作为导入/测试辅助追加 `message.imported`，生产 Agent loop 不调用它。

全局控制面使用 `facts_by_type(types)` 在 Store 锁内读取指定类型的全部非 partial 事实，按时间及 rowid 排序，不受列表导航的 100 条分页影响；控制面不直接访问私有 SQLite 连接。

主要事件：

- run.admitted、run.started、message.user、model.request、model.response、model.retry；
- tool.prepared、tool.completed、tool.abandoned；
- approval.required、approval.resolved；
- workspace.checkpoint、context.configured、context.compacted、context.trimmed、context.summary_requested、context.summary_generated；
- task.plan_updated（payload 为 `{todos, next_task_seq, step}`，每个 todo 含 `id`、`content`、`status`）；
- run.completed、run.failed、run.interrupted（终态 payload 另带 `reason`：`step_budget`、`task_step_budget`、`token_budget`、`unknown_tool_outcome`、`cancelled` 或 `null`）。

模型响应保存结构化 content blocks，包括 provider_state 与带稳定 ID 的 tool_use。工具执行前提交 prepared，结束后提交 completed；工具组收齐结果后才形成 provider-neutral tool_result 消息。副作用结果之后另存 workspace checkpoint；若两者之间崩溃，Continue 拒绝缺失 checkpoint 的历史。

SSE/get_events 为旧客户端把 prepared/completed 映射成 tool.request/tool.result，并使用 session_seq 作为兼容 seq 游标；JSONL 导出使用 canonical 事件名称。事件先提交，再通知观察者；观察者失败不改变运行事实。

## 三类投影与上下文

MessagesProjection 只消费已提交消息和工具事实，保留工具配对、次序及 Responses 私有状态。未配对历史拒绝发送给模型；其他 Provider 按 adapter 契约忽略不适用的私有状态。

RuntimeStateProjection 从事件得到 running、waiting_for_user、completed、failed、interrupted、usage、步骤、工具、停驻原因和待审批状态。SessionManager 将最新 interrupted run 展示为 parked；pending approval 保留历史证据，审批提交还必须匹配该 run 与当前进程的 pending future。

TaskPlanProjection 折叠 `task.plan_updated` 快照，得到 Session 的任务列表、每项的首次出现序号、结算序号、完成声明，以及当前 `active_task_id`（= 最新计划里 `in_progress` 那一项）。被移出计划的项保留最后已知状态但失去位置。投影只读、不落库，`store.task_events()` 只读取计划与工具结果三类事件，不重放整个 Session 日志。计划事件始终是任务的唯一写入者：不存在独立的激活或完成事件，因此也不存在"计划说 A、生命周期事件说 B"的分歧。

任务证据由 Runtime 从该任务**首次出现到结算之间**的 `tool.completed` / `tool.abandoned` 事件派生，条目引用真实存在的 `run_id`、`call_id`、事件 id，因此不可能凭空出现；同一 Session 内的同名 `call_id` 不会跨 Session 泄漏。上限 8 条并报告省略数量。`todo_write` 自身的调用是调度记账，不计入证据。**证据只记录"发生过什么"**：失败的调用照样在列，`summary` 是模型对"做了什么"的声明，两者都不构成"验证已通过"。

ContextBuilder 每次读取日志，构造模型输入；根 AGENTS.md 最多读取 4,000 字符，拒绝指向 Workspace 外的链接。每次 Run 还在 Workspace 根目录快照 `skills/*/SKILL.md`：system prompt 只加入名称和简介，完整内容由动态只读工具 `load_skill` 按需返回。Skill 路径必须留在 Workspace 内，运行中修改只影响下一次 Run。每轮 run 记录指令 hash、Skill 目录 hash 和工具目录 hash；实际 Skill 加载沿用 canonical tool prepared/completed 事件。消息与工具结果进入语义记录时会脱敏，但正文不静默截断；下一模型请求读取同一记录。Provider 返回的原始工具参数用于实际执行，持久事件中的敏感字段使用脱敏副本，避免审计规则改变工具行为。已知模型 API Key 在文本中也会替换。Host 每个 step 从最新 `task.plan_updated` 投影当前计划：system 只包含未完成项、它们的稳定 `id` 和完成数量；历史 `todo_write` 的完整参数在 ContextBuilder 请求投影中替换为合法空清单，默认 MessagesProjection 和 Event Log 不变。计划项身份由 Runtime 在写入时分配（`t1`、`t2`……），序号单调且不回收；模型回传已知 `id` 即保留身份，省略时按内容匹配复用，引用未知 `id` 或重复使用同一 `id` 则拒绝该次更新。旧事件缺少 `id` 与 `next_task_seq` 时按已见序号兜底，只读投影，不回写日志。单条工具结果超过 12,000 字符时，ContextBuilder 在所有模型请求（包括摘要请求）中只投影头尾预览，并写明稳定的 run_id/call_id；模型通过 session-scoped 的只读 `read_tool_result` 按字符 offset/limit 回读原结果。预算仍不足时，当前 turn 内较早的结果进一步折叠为占位串，保留最近三条预览或原文及 tool_use/tool_result 配对。请求侧省略与两层结果裁剪都记录 `context.trimmed`，Event Log、MessagesProjection 默认输出和审计导出保持完整。read_file 自行分页并报告文件总行数与下一页 offset。UI 可以独立裁剪展示。

持久 checkpoint 保存 covered_seq、source_digest、summary、policy_version、provider、model。摘要只覆盖结束 turn 的完整前缀；模型调用使用 summary + 未覆盖 raw tail。checkpoint 来源 digest、策略和模型身份不符时忽略；空摘要、工具调用或达到 Provider 输出上限的残缺摘要都视为生成失败。摘要生成失败且旧投影仍在预算内时使用旧投影；摘要之后仍超预算时先折叠当前 turn 的旧工具结果，仍不够才返回 context_overflow。压缩与折叠都不改变原始事件。context_limit 计算 system、messages 和完整工具目录的 provider-neutral 序列化请求，仍以字符数近似，不宣称 token 精确计量。ContextBuilder 抽出模块级 `request_size()` 与 `tool_catalog_size()` 作为唯一序列化度量；Host 发出的 `model.request` payload 复用同一函数记录 `request_chars` 与 `tool_catalog_chars`，composer 的上下文指示器和用量分析因此与预算使用同一口径。

Host 的两阶段接收由 `admit_run` / `admit_continue` 和 `start_admitted_run` 组成：先在 Workspace 锁内预留精确 Run/Session 身份，通过可取消但会等待线程结束的恢复和 checkpoint 检查，再由 `RuntimeStore.admit_run()` 在一个 SQLite 事务中写入 turn/run、`run.admitted`、用户消息或恢复事实和 checkpoint。附件验证、事务提交与 used 缓存写入之间没有 await；跨文件和 SQLite 仍不是共同事务，因此附件 API 同时查询 canonical 引用来保护生命周期。202 前同步登记执行 Task，使立即取消、关闭和配置切换都能找到所有者。Python 直接调用按 Workspace 排队，HTTP 重复请求拒绝；临时 reservation 只管理进程内所有权，重启事实仍以 Event Log 为准。

ContextBuilder 的可选 completion callback 把生产摘要接入 Host 的模型 timeout/retry 和统一用量账本；不提供 callback 的离线调用者保持 Provider.complete 行为。`context.summary_requested/generated` 不进入 MessagesProjection，也不增加主 Agent 步数；成功生成的 usage 在是否采用判断前记账，新 `context.compacted` 不重复携带 usage。旧 compacted usage 仍可投影兼容。`run_token_limit`（默认 0）检查成功响应返回的 input+output tokens，包括摘要，在下一次主模型或摘要请求前停驻；它是按 Run 计数的软上限，不估计重试未返回的消费或视觉 token，不保证账单硬上限。

## 中断与 Continue

启动时扫描缺少终态的 run，不调用模型。崩溃可能正好落在「副作用工具已提交 tool.completed、它的 checkpoint 还没写」之间，此时 Host 用文件系统本身当证据：`changed_since()` 比较 Git 可见路径（已跟踪加未忽略的未跟踪）的 mtime 与已跟踪文件是否缺失，基准是该 run 最后一条持久事实的时间戳。全部早于该时刻且没有缺失时，当前工作区就是 Host 死亡时的状态，Host 先补记 workspace.checkpoint 再写 run.interrupted，Session 停驻但仍可 Continue；查到改动则不补 checkpoint，并把改动路径写进终态 payload 的 `gap_files`（最多 20 条），让用户知道该核对什么。无法作答（非 Git、不可读、文件数超过扫描上限）时按没有证据处理，行为与本次修改前一致。单次 run 用尽步数预算（`max_steps`，默认 30）时所有工具结果已提交，Host 先记录工作区 checkpoint，再写入 run.interrupted，Session 停驻，用户可 Continue 到新 turn 并获得新的步数预算。存在计划时还有一条更小的软边界：同一条 `in_progress` 任务连续占用超过 `task_max_steps`（默认 12，`0` 关闭）个 step 时同样停驻，终态 `reason` 记为 `task_step_budget` 并在错误信息中点名任务 id。用户停止运行（`reason=cancelled`）时也先补记工作区 checkpoint 再写 `run.interrupted`：取消可能正好落在"副作用工具已提交、它的 checkpoint 还没写"之间，补记这一步保证停驻后仍可 Continue。该计数只在本次 run 内存在、不落盘，因此 Continue 后重新计数；`max_steps` 始终是硬上限，Host 不因为单任务超时而改写计划状态。

用户主动 Continue 必须通过 Workspace 锁内检查：

1. 最新 run 为 interrupted，且 Workspace 为可验证的 Git 仓库。
2. 当前 HEAD、暂存与未暂存 binary diff digest、未跟踪文件路径和内容 digest 与最后可信 checkpoint 相同。
3. 不存在结果未知的非只读 prepared 调用；MCP 即使声明只读，也采用保守的恢复策略。
4. 已完成副作用工具后存在 checkpoint；无法采集证据时拒绝继续。

Continue 从来源 `run.admitted` / `run.started` 继承 Plan 的只读限制与相同 Provider/endpoint hash 下的模型；原 Agent 自动审批不延续，其他模式归为 Auto。Provider 或 Base URL 改变时使用当前默认模型，旧记录缺 mode 时按 Auto。恢复事实扫描整个 Session 未闭合的工具与审批，覆盖执行前已被停止的 Continue 链。Continue 创建新 turn/run，并以唯一 continuation_of 关联来源 run；不会重放旧工具或重复用户消息。未完成 read_file/glob/compact 和确定尚未派发的调用写入 abandoned，补齐下一次模型请求的 tool-result 结构。只读结果也不伪装成成功。续跑的 `run.started` 记录 `resumed_task_id`（来源 run 停驻时的 `active_task_id`），system 中写明正在接续哪一条任务。

没有 HEAD、非 Git、存在 submodule、证据采集失败、未知副作用或源码变化时维持 parked 并解释原因。用户可以显式 abandon：Runtime 创建一个不调用模型的关联 run，把未配对调用记录为结果未知且未重放，保留全部历史并解除 Session 停驻。范围仅为 Git 可见源码；ignored 文件、外部 MCP 服务和 OS 全局状态不在快照内。不是指令级恢复，也不是操作系统沙箱。

## 入口、模型与 MCP

`api.py` 是 FastAPI 组装入口，保留 `create_app(runtime=None)`；现有 HTTP 路由按工作区、Session、Run、模型生成参数、Provider 配置、附件及 SSE/导出拆入 `http_api/`。请求模型集中声明；每个 router factory 接收同一应用的 `ApiContext`，其中只保存 Host 引用、审批 Future、暂存 reservation 与 Task 集合，不是新的运行状态库。每个应用独立创建上下文，禁止模块级共享可变状态。Host/Store 仍管理接收、执行和全部事实；应用 lifespan 在退出时取消并等待自己跟踪的 Task 后再关闭 Host。

同源写入守卫仍在应用级，领域路由继续执行原本的回环、身份和权限检查。统一 HTTP 错误包含 code/message/detail，保留状态码、HTTP 异常响应头以及字符串或列表 detail；RequestValidationError 不返回原始 input/ctx，内部异常仅返回通用错误。OpenAPI 使用共同 ErrorResponse 描述该契约；模型连接探测的 HTTP 200 失败结果不改为协议错误。38 条现有路由全部保留，用量查询仍覆盖完整历史，SSE 持久游标及 opt-in 临时预览语义不变。Skills 管理已由专用路由承接；索引、评测及交付长任务控制面仍待后续步骤，A1 尚未整体完成。

HTTP写请求先校验浏览器同源与本机Origin；无Origin的可信非浏览器路径沿用现有权限边界。Run/Continue可用Idempotency-Key生成操作/Session隔离的确定性Run身份，在admission原子保存内容指纹；重复提交先查持久接收事实，再检查busy/parked，不重新启动已接收任务。未落库接收竞争仍拒绝为busy，不通过内存缓存伪造持久成功。

生成参数使用 `generation.py` 严格校验和能力描述。模型默认/会话覆盖保存在 `generation.configured` 追加事实中，通过 Provider、endpoint、model 的哈希隔离；模型默认管理使用归档 Session，不新增独立可变配置表。省略继承、null清除、0有效，`run.admitted/started.generation` 保存有效请求快照。Continue在原接入下继承并重验，切换接入则重新解析；摘要/只读子Run不隐式继承用户采样覆盖。`ModelRequest.generation` 由adapter映射到SDK参数，最大输出仍受现有请求字段约束。

每次模型调用记录 `model.attempt_started/finished`，同重试组共享request_id，每次真实尝试有独立attempt_id。成功保存报告usage，失败/取消无usage记unknown；事件不记录请求正文。原model.response和summary事件继续提供上下文事实，后续用量账本只以attempt事实计算，避免重复计数。

SDK内部重试显式关闭，Host统一重试，因此每条attempt对应一次SDK请求。默认最大输出也纳入有效参数快照，重启改变启动默认不会改变Continue。Session覆盖查询同时绑定模型身份，切换模型再切回仍可恢复各自设置。Chat兼容adapter保留并回传其reasoning_content私有状态，不泄漏到其他Provider格式。

三类 Provider adapter 同时提供 `complete(request)` 与可选 `stream(request)`。`streaming.ModelStreamEvent` 只有 `text_delta` 和 `response_completed`；只有后者含完整 ModelResponse 和工具调用，增量中没有可执行 ToolCall。Responses 读取 SDK 的终态对象，Chat 按调用 index 拼接参数，Anthropic 按消息/内容块生命周期拼接 JSON；各自保留必要的私有推理状态，增量只发送可见文本。收到最终对象前不会执行工具。Chat 等待结束原因并读取后续 usage，Anthropic 用累计 message_delta 覆盖临时用量，不把 message_start 的输出零值冒充最终消费。

SDK stream 在终态返回前关闭；异常、取消和消费者 aclose 同样释放连接。SDK 请求创建成功后任何断流均为不可重试错误，不能因预览失败回退为 complete 重发。适配器限制保留内容/参数/推理合计 8 MiB、128 个输出项及 65,536 个事件；超限与缺终态均不返回完整响应。这些是响应组装边界，不是请求费用上限或操作系统隔离。适配器协议已通过内存 SSE 与官方 SDK 验证，Host 的临时预览、SSE 及尝试账本集成仍按 A6/A7 单独验收。

Responses 终态为 incomplete 时，即使带有格式正确的 function_call，也保留 max_output_tokens 等原始截断原因，不能改写成 tool_use。外层 completed 但工具项仍为 in_progress/incomplete、缺少调用身份或 JSON 无效时，同样标记 incomplete；缺身份的项不制造伪调用，带身份的 JSON 错误保留审计占位。完整 ModelResponse 继续携带最终 usage，使 Host 可先记账，再终止并标记未派发的调用，不能因工具不完整而丢失已知消费。

`RuntimeStore.usage_events`在一个递归查询中选取指定Run/Session及可选持久子Run的调用事实；`usage.build_usage_ledger`纯派生去重、已报告/估算/未知及完整性，HTTP用量接口不依赖列表分页。终态/worker汇总不再次收费，历史只做不完整响应回退。价格、共享预算、输入组成估算与Vue视图仍按U2/U3实施。

`RuntimeHost.run(RunRequest)`、create_session、Provider 配置和 RunResult 原字段保持当前契约；RunResult 新增带默认值的 turn_id、continuation_of。RuntimeHost 还提供 resolve_or_register_workspace、continue_session、session_status。

CLI 提供 run/chat/serve 的 --workspace、run/chat 的 --cwd、workspace add/list/show/remove、continue、abandon、export 和 plan show。`plan show <session_id>` 读取 session_status 中已有的计划事实，默认人类可读输出，`--json` 返回与 HTTP 同构的 `task_plan`、`active_task_id` 和 `task_state`；CLI 不重算投影，也不提供任何计划写入口。无指定 Workspace 时使用启动 cwd；Git 子目录可作为 Session cwd，同时 Workspace 身份仍归一到仓库根。HTTP 保留原路由，新增 /api/workspaces、/api/workspaces/{id}/sessions、/api/sessions/{id}、/messages、/runs、/continue、/abandon、按 run_id 取消和 canonical JSONL export。Session POST 可传 working_directory，PATCH 可设置标题或归档；列表默认隐藏归档记录；DELETE 只允许没有运行历史的空 Session。普通 Run 与 Continue 都返回 202；Continue 在响应前完成安全校验并持久化 continuation Run 身份，执行结果通过 Run 查询与 SSE 获取。Export 按 session_seq 分页读取 canonical Event 并以 NDJSON 流响应，不使用 SSE 兼容别名；CLI 写文件默认不覆盖。轻量事实接口包括：`GET /api/workspaces/{id}/capabilities` 用纯文件解析返回 Workspace 的 Skill 名称/简介、MCP 服务名/transport 与配置路径提示，不连接 MCP 服务，也不返回 command/env/密钥，坏配置写入 `notes` 而非 500；`GET /api/runtime/settings` 透出 `context_limit`、`approval_timeout`、`max_steps`、`task_max_steps`、`run_token_limit`；Run 摘要（`list_runs` 与 latest run summary）随状态字段透出 RuntimeStateProjection 已派生的 `usage`（input/output tokens）。这些只是字段透出，不新增事实源。模型列表辅助位于 `model_catalog.py`，`GET /api/models` 探测当前 Provider 的列表端点，失败返回空列表与说明而不抛错。Web 观察 Session/Run Projection 发现执行并消费同一 SSE，运行中可请求停止。HTTP 同 Workspace 已有请求时返回 409；Python Host 的请求按 Workspace 锁排队。

`migrate-v02 [source] --workspace <target>` 显式导入旧 `.nexus/nexus.db`。旧 messages 变为 `message.imported`，工具事件名称映射到 canonical prepared/completed，Run 根据旧终态补成当前终态；无法确认结束的旧 running Run 作为 interrupted 导入。旧源文件不写入，身份冲突时整个导入回滚。Provider 名称、URL 和模型可在目标没有配置时导入，旧 `api_key_ciphertext` 不跨密钥根复制。

模型配置为 Host 级，活跃 run 期间不允许修改。RunRequest 可携带可选 `model`，只覆盖该次 Run 的 ModelRequest 和 checkpoint 身份，不修改 Host 默认配置或重建 Provider 客户端。Provider 客户端惰性创建，连接检查不切换活动 Provider。密钥继续使用 Fernet；主密钥优先 EVENTIDE_SECRET_KEY，否则状态根 secret.key。解密失败不退回明文。凭据管理及 Workspace 注册/移除仅接受本机回环请求。

每个 run 按 Workspace 读取 mcp.json；MCP SDK transport 在同一 owning task 内连接和关闭，避免跨 task 的资源退出。各 MCP Server 独立连接和报告错误，单个 Server 的连接、发现或关闭异常不覆盖其他能力及已完成结果。模型请求、MCP 连接/调用和本地 Shell 分别受 `EVENTIDE_MODEL_TIMEOUT`、`EVENTIDE_MCP_TIMEOUT`、`EVENTIDE_COMMAND_TIMEOUT` 限制；取消 Shell 时终止其进程树。所有工具统一进入 ToolExecutor/PolicyEngine，文件工具内部再次检查路径。生产基础目录包含文件、Shell、compact、`read_tool_result` 和 `todo_write`；前者只能按当前 Session、指定 Run 和 call identity 读取 canonical `tool.completed`，单页最多 12,000 字符；后者替换当前 Session 计划，最多 20 项且只修改 Event Log，作为 recovery-safe 工具不会触发 Workspace checkpoint。Workspace 存在有效 Skill 时动态加入 `load_skill`；存在与当前 embedding fingerprint 匹配的知识索引时动态加入只读 `search_knowledge`。旧教学 task graph/teammate/cron、全局 Skill loader、同步 Agent/LLM、context/hooks、模拟 MCP 与全局注册表已经删除；不再提供 `AgentRuntime` / `TraceStore` 别名。Skills、摘要、恢复与审批均沿用已有生产实现。离线 Eval 使用 scripted Provider；离线与 live 评测均显式关闭真实 MCP，并为每条用例创建独立临时项目和评测数据库。

RunBody 支持每次 Run 的 `mode`（auto/plan/agent，未知值按 auto 处理）、`attachment_ids` 与可选 `model`，策略在 `run_policy.py` 中保持为纯函数，wiring 在 Host。`plan` 把模型可见的工具目录收敛为只读白名单（`read_file`/`glob`/`read_tool_result`/`compact`/`todo_write`/`load_skill`/`search_knowledge`），system 追加只读声明；executor handlers 同步裁剪到可见名字，模型幻觉出的写调用不会命中处理器；MCP 服务仍按 Workspace 配置连接，只是其工具不进入目录。`agent` 在该 Run 内自动允许 ASK 决策，不再等待审批处理器，`approval.resolved` payload 带 `auto: true` 且 `author="runtime"`，审计事实完整。附件上传采用 JSON base64、单文件 ≤10MB，存储在状态根 `attachments/~<sha256(session_id)>/{attachment_id}`，不属于 SQLite schema；内部 v3 记录保存精确 session_id，读取、标记、删除和水合均校验；新目录始终使用完整 ID 的 SHA-256，拒绝链接逃逸与重名覆盖。旧安全目录保守只读兼容，无法确认归属的旧记录拒绝访问。清单默认只返回未使用附件，used 由文件缓存或 canonical 引用共同确定；下载保持原始媒体类型，删除只允许未使用附件。HTTP 在返回 202 前校验归属与当前 Provider 能力。UTF-8 文本以分隔符内联且单附件超过 512KB 时截断；图片和 PDF 在事件中只保存不可变引用，每次 Provider 请求前从状态根水合，避免 canonical Event Log、审计导出和字符预算复制 base64。图片映射到 Anthropic、OpenAI-compatible 与 OpenAI Responses 的各自多模态协议；PDF 只映射到 Anthropic 与 OpenAI Responses。普通二进制可以存取，但没有协议映射时明确拒绝提交。

## 工具参数契约

`tool_contracts.py` 是内置工具参数的类型来源：严格 Pydantic 模型生成目录 schema，`production_tool_catalog()` 将 Host 动态内置目录映射到同一类型，并保留当前 Skill 名称枚举。省略字段通过 `exclude_unset` 保留省略，传入的 JSON 不作类型强制转换。依赖拓扑、Session 归属和文件路径仍由既有处理器及策略验证。

`ToolExecutor` 固定 handler、只读集合及 schema 快照。未知 handler、缺契约、无效 schema 和无效参数均在 PolicyEngine/审批/实际 handler 前返回带稳定 `metadata.code` 的错误。验证成功的原始参数继续通过策略；只有文件与 Shell 工具可在验证后注入运行时 cwd。CommandResult 的 timeout/exit code 和已知未知副作用继续保留，参数修正不得成为副作用重放路径。

动态 schema 使用 jsonschema 和显式空的 referencing Registry。构造时按 dialect 遍历真实子 schema，拒绝网络/文件/相对外部引用、未知 dialect、未支持的引用关键字和无法解析的本地引用；const/default 中的普通数据不会被当作 schema。坏条目不使其他工具不可用。MCP 的 session、工具原名及 timeout 通过词法闭包绑定，模型 `_session` 等同名字段只能作为远端工具参数，无法替换 transport。

本提交交付执行器和目录基础；Host 对实际可用目录传入 `tools=`、请求目录归一化以及每个 Run 最多一次参数修正仍待 A3 的集成步骤。定向测试覆盖严格类型、字段省略/空值、权限边界、冻结目录、局部引用、失败隔离和两个真实本地 MCP transport。

## Workspace 知识检索

RAG 是与 Runtime 状态库分离的 Workspace 派生能力。用户显式运行 `eventide rag build` 后，Git Workspace 内 `.eventide/index.sqlite` 保存 `files`、带文件/行号/标题路径的 `chunks`、float32 embedding BLOB、FTS5 trigram 索引和 embedding fingerprint；数据库可随时删除重建，不进入 canonical Runtime schema 或 Event Log。v0.4 只枚举 `git ls-files --cached --others --exclude-standard` 可见的 UTF-8 文本，跳过 symlink、二进制、超过 1 MiB 的文件以及 `.git/`、`.eventide/`。增量判断以内容 SHA-256 为准；构建在写事务外完成读取、切块和 embedding，提交时重新核对语料与 index generation，并以单个 SQLite 事务替换文件、chunk、FTS 和 meta，因此查询只看到旧或新完整快照。

Markdown 按 fenced code block 外的 H1～H3 切 section，超长 section、源码和普通文本再使用 600～800 字符窗口及约 100 字符 overlap；每块保存准确的 1-based 闭区间行号。默认 HashEmbedder 用 SHA-256 token hashing 生成确定性本地向量，只验证离线机制，不声称具备学习得到的语义能力；可选 OpenAI-compatible provider 只在显式配置后惰性调用。provider、model、base URL、dimension 和算法版本组成 fingerprint，任何变化都要求 `--force`，避免比较来自不同向量空间的 BLOB。

读连接使用 SQLite URI `mode=ro`，不运行 schema 初始化。查询 embedding 在读事务外生成，随后在同一个 WAL 快照内重验 fingerprint、读取 dense 和 sparse 候选，防止重建复用 chunk ID 导致跨代融合。构建的第二次 manifest 检查在 `BEGIN IMMEDIATE` 之后、写入之前执行；它检测两次扫描之间的变化，但不等价于锁住工作区文件系统，用户仍应避免构建期间编辑语料。Host 将 SQLite 错误视为索引不可用。

查询同时执行全量 float32 余弦 Top-2k 与 FTS5 BM25 Top-2k，再用一基排名、常量 60 的 Reciprocal Rank Fusion 合并。BM25 与余弦只贡献各自排名，不直接相加不同量纲的原始分数。Host 在**每次 Run**按当前 Session 的 Workspace 检查索引及 fingerprint；有效时动态加入 `search_knowledge(query, k)`，无效或不存在时不向模型暴露。该工具属于 Plan 可见的只读集合，仍由 `ToolExecutor` 和 `PolicyEngine` 执行，调用事实继续使用既有 `tool.prepared` / `tool.completed`，不新增事件类型或副作用 checkpoint。

`eventide rag eval` 将标注语料复制到临时 Git Workspace，强制使用 HashEmbedder 建索引，按 query→期望文件计算 macro recall@5 与 MRR；普通 scripted smoke 通过显式 case 级 `rag_index: true` 在既有 Git sandbox 中预建索引。两条离线路径都不读取真实 embedding 配置、不访问网络，也不会在真实 checkout 写索引。

检索评测在同一索引、同一组 query/标签上依次执行 sparse、dense 和 hybrid。文件召回以去重的相关文件数计算；MRR 以第一条相关 passage 的排名计算，`expect_contains` 可进一步限制相关 passage。聚合前不舍入。顶层指标和 CLI 退出码保持 hybrid 语义，`comparisons` 保留三路完整结果。JSON 还记录复制后语料 SHA-256、套件 SHA-256、embedding fingerprint、索引规模，以及每题耗时和 nearest-rank p50/p95；耗时是单次本机观测，不是性能基准。复制排除 `.git` / `.eventide`，拒绝 symlink，并拒绝不存在的期望文件标签。

## 评测

评测套件是 YAML 的 `cases` 列表（`evals/smoke.yaml` 覆盖机制，`evals/tasks.yaml` 覆盖任务级完成）。每条用例包含 `id`、`prompt`、离线 `script`（ScriptedProvider 步骤）、`expected`，以及可选的 `workspace`（相对路径 → 文件内容的 fixture 映射）。所有用例各自创建一次性 Git 沙箱：未声明 `workspace` 时使用空 fixture，其他情况仅接受相对路径映射；写入 fixture、`git init` 并提交初始 commit（仓库级 user identity 和 `.eventide/` 排除规则写在沙箱内）。Runtime 使用沙箱 `workdir`、沙箱内 `.eventide` 状态根和独立数据库，离线与 live 都关闭外部 MCP。每次评测统一临时产物根保存所有用例与 JSON 报告，不自动删除，不写启动 checkout 或用户状态根。报告顶层 `artifact_dir` / `report_path` 和逐用例 `artifact_dir` 可定位失败证据；Python `output_dir` 只导出报告，不复制状态数据库。

live 在更换状态根之前调用共享 `resolve_provider_configuration`；其使用 `mode=ro` SQLite 读事务读取已提交 Provider 配置（包括活跃 WAL），不初始化 schema、不获取 HostLease、不恢复历史 Run。Host 与 live 共用 `apply_provider_configuration` 的已保存配置优先级；没有保存配置时保持启动配置，密文不可解密时禁止环境密钥回退。SecretBox 解密不创建密钥，缺失/无效/不可读密钥分别明确报告；凭据只留内存，评测使用注入 Provider，不向临时库保存连接凭据。离线路径不调用 resolver，并移除启动 API Key 与真实 embedding 凭据。

判分（`_judge`）是确定性的 fs/git 证据检查，零模型调用：既有 `status`、`final_contains`、`required_tools`、`permission_denied`，任务级另有 `file_exists`、`file_absent`、`file_contains`（大小写不敏感子串）和 `git_committed`（初始提交之外存在新提交且 `git status --porcelain` 为空；非 Git 沙箱报 `git evidence unavailable`）。模型最终输出只参与 `final_contains`，从不作为完成证据。报告在 `mode/suite/passed/total/pass_rate/average_duration_ms/tool_success_rate/safety_blocks/duration_ms/results` 之外提供 `task_completion_rate`、`failure_reasons` 分布和 `average_steps`；live 模式另计 `total_input_tokens` / `total_output_tokens`，offline 模式带 `regression_smoke: true` 标记。CLI 默认打印人类可读摘要（完成率、失败原因分布、失败用例），`--json` 输出完整报告；退出码保持"全部通过才为 0"。每条 `EvalCaseResult` 透传 RunResult 的 `steps`、`usage`、`status`。

评测的 `expected.successful_tools` 要求对应 `tool.result` 的 `is_error` 明确为 false；`required_tools` 仍仅检查调用是否发生，供拒绝策略测试使用。注意工具成功标志并不代替子进程退出码或文件内容验收。

`examples/rag_repair.py` 是独立离线工程闭环：临时 Git fixture → 索引 → `search_knowledge` / `read_file` → 步数上限停驻 → 重开 Host → Continue → `edit_file` / 越界拒绝 / `bash` 测试。使用真实文件工具、策略、持久化与本地子进程，但 Provider 决策是 scripted。独立 verifier 通过当前 Python 的 unittest 退出码和测试源码完整性判定修复证据，不信任最终回复或 Shell 工具的成功标志；不修改生产 Shell 返回协议，也不新增事件。

## Web 展示投影与交互

Web 是原生 ES modules，无构建步骤。`index.html` 定义两栏外壳：左侧导航（Workspace 切换与 Session 列表）与工作区（topbar、正文与 composer）；topbar 的任务计划、工具、详情、用量四个胶囊按钮各自弹出悬浮详情卡，替代原右侧常驻工作面板；`app.js` 协调 Workspace 注册、Workspace/Session 选择与管理、API 状态、审批、Continue、四个胶囊对应的浮层与 composer；`projection.js` 提供纯展示投影；`transport.js` 消费同一 SSE 路由；`view.js` 处理稳定 DOM 与安全 Markdown 子集；`config.js` 管理 Host 模型配置。composer 的模型下拉读取 `/api/models`，每次提交把非默认选择写入 RunBody；旁边“配置”按钮继续打开 Host 连接弹窗。添加工作区对话框调用仅限本机的 Workspace POST，成功后刷新内存目录并直接切换，不自动创建空 Session。Session 行的可见“⋯”与 contextmenu 打开同一管理对话框；归档列表显式切换，删除冲突保留服务端说明。左侧会话列表按“今天 / 7 天内 / 更早”分组，状态点来自 Session 投影，计划进度徽标复用 `planGroups` 计数。

传输层将兼容 tool.request/result 名称归一化，按 event_id 或 run/seq 去重、按 session_seq 排序；流断开后从最后已消费游标补齐，即使 Run 已终止也不跳过尾部事件。Event cursor 在 SQLite 查询中直接过滤，不先重放旧事件。导航与历史索引只投影状态、审批和终态等轻量事实，Run History 按稳定 run_id cursor 分页；展开章节时才读取该 Run 的事件。最新 Run 摘要另用 SQL 聚合步骤、工具数和最后事件时间；`get_run_summary` 与 `list_runs` 还把 RuntimeStateProjection 已派生的终态 `reason` 与 status/error 一并返回，供前端解释停驻原因，这只是字段透出，不新增事实源。Web 将摘要与已收到的 SSE 合并，状态 pill 显示会话状态、已观察步数、正在执行的任务和运行时长。运行状态和 pending approval 始终查询服务端 Runtime Projection，事件通知触发状态协调，周期查询发现其他入口启动的 Run。异步结果按所属 Workspace/Session 缓存，不写入当前选中 Session 的其他记录。

Semantic Work Projection 从 canonical 事件证据与 Run 关联生成只读工作片段：工具 prepared/completed 按 run_id/call_id 配对，保留请求参数与结果；Continue 的 abandoned 沿 continuation_of 找到原操作，未派发调用可关联原 model.response。调用状态区分等待结果、中断时结果未确认、完成、失败、拒绝和 abandoned。Continue 的接续说明块在 `run.started.resumed_task_id` 命中当前计划时写明“继续执行任务 tX：<内容>”，任务内容取自 `session_status` 的 `task_state`；查不到该任务时保持“未重发原始指令”的原句。普通 checkpoint 不切分阶段，也不在主视图占一行；null 不被描述成有效证据。有限的命令分类只识别明确调用，未知 Bash/MCP 保持中性措辞；操作完成不推导未记录的验证结论。

每条用户意图和其 Continue 链组成一个章节；最新章节默认展开，旧章节的终态事件按需加载，工具详情默认折叠。首章识别不到意图时标签回退为“最初的目标”，后续章节为“后续工作”。Execution Block 包含稳定 ID、来源事件、Run、操作状态和可读描述，不写 SQLite；只有 attention 与 approval 类块保留独立“查看详情”按钮，continuation、context 等说明块不再各带按钮。用户消息以右对齐气泡完整展示原文；每章渲染一行由 `toolStats` 聚合的执行统计，点击后调用 `openToolsForChapter` 打开工具浮层并定位本章明细；章节之间的 Run 谱系徽标展示该章各 Run 的状态与 continuation 链，失败/中断且没有结果的 run 仍以 run 徽章行展示。原始事件、模型协议与 checkpoint 的详情通过「详情」胶囊打开的浮层的时间线与过滤查看。每轮的最终输出作为回复卡内联在该轮末尾，每轮最多一次；回复卡头部标注 run 编号（点击打开详情浮层）、步数、时长与时间。界面显示的“本次执行结束”区别于 Session 生命周期结束。

「计划」胶囊弹出的任务计划浮层是 `session_status` 中 `task_state` 的纯展示投影：`projection.js` 的 `planGroups` 只做分组与计数（“N / M 已完成”总进度，非零时追加进行中、受阻数量，不单独显示待开始；active 高亮，completed 折叠进“已完成的任务”分组，summary 与证据展开），不复制 TaskPlanProjection 的身份与窗口规则；`planRevisions` 按 `task.plan_updated` 事件顺序折叠修订历史，只保留带稳定 `id` 的条目。证据条目点击后通过 `openToolAt` 定位到工具浮层中的对应调用。Session 状态行在运行中复用同一投影显示“正在做：<active 任务内容>”。收到 `task.plan_updated` SSE 时与状态类事件一样触发 `session_status` 重新查询，浮层随之刷新；证据措辞与 CLI 相同，只描述发生过的调用，不推导验证结论，也不提供任何编辑入口。

停驻原因由 `projection.js` 的 `parkLabel`/`parkSummary` 做纯展示翻译：状态行短语与恢复区首句按终态结构化 `reason` 映射为用户可读中文（`step_budget`、`task_step_budget`、`cancelled` 各有措辞，无 `reason` 且错误为 "Host stopped before terminal fact" 时按服务中断处理）。`cancelled` 同时覆盖用户停止与 Host 关停引发的后台取消，措辞不指明是谁停止；未识别的 `reason` 回退显示原始错误文本，原始错误文本也始终可通过“查看中断详情”在详情浮层查看。

Continue 使用异步 POST 契约；收到 202 后立即按返回的 run_id 订阅，包括处理新审批。仅最新 parked Session 提供 Continue；409 留在恢复区，不发普通 Prompt 绕过。审批按钮提交前检查服务端 pending 状态。历史展开、草稿、阅读位置按 Session/章节保存于页面内存；选择项、主题与 composer 模式保存在 localStorage。按稳定 key 更新发生变化的 DOM，详情内容以 ID 获取最新投影后渲染在对应悬浮卡；原生 dialog 继续承载工作区管理、模型配置和能力面板，支持 Escape 和焦点返回。Markdown 通过 DOM 文本构造，禁用原始 HTML 和非 HTTP(S)/mailto 链接协议。

顶栏四个胶囊各自弹出悬浮卡，承载任务计划、工具活动、详情、用量四个只读投影分区；渲染函数与投影事实不变，只是承载容器从常驻右栏改为按需浮层：工具活动浮层用 `toolStats` 汇总调用统计并按章节折叠分组列出明细与耗时，失败与未确认调用高亮；详情浮层用 `timeline` 折叠事件时间线、提供事件过滤并承载审计导出按钮（复用按 Run 的 NDJSON export 接口）；用量浮层读取 Run 摘要与详情的 `usage`、步数和 `request_chars`，展示最新 run 与会话累计。composer 是交互中枢：附件“＋”上传后以 chips 展示，附件接口缺失时按钮置灰；模式选择是下拉菜单，Auto/Plan/Agent 三个选项各带一行说明，Plan/Agent 语义由后端 `run_policy` 决定，前端不复制白名单；发送按钮空闲时为圆形 ↑、运行中原位变为 ■ 停止；上下文占用以小进度条显示，以最新 `model.request` 的 `request_chars` 除以 `GET /api/runtime/settings` 的 `context_limit` 计算百分比，超过 80% 变为警示色，字段缺失时显示 `--`。请求失败通过右下角 toast 提示，error toast 带重试按钮；空状态按“无工作区 → 无工作 → 无记录”分层引导。

界面主题用 CSS 变量实现浅/深双模式：默认跟随 `prefers-color-scheme`，手动切换写入 localStorage，侧栏折叠状态同样持久化。性能上每秒 tick 只重绘状态 pill 文本（步骤/时长），不触发章节或浮层重建；每章投影按事件数量、Run 数量和 task_state 修订组成的 key 缓存（`projectCached`），未变化时直接复用渲染结果；任务计划浮层按投影版本 reconcile。与服务的连接断开时状态 pill 进入重连显示，轮询补齐后自动解除。

## 工程边界

本轮演进以 Web 工作台、同项目隔离并发、正式 Skills 与 Token 成本治理为目标，编号步骤及验收证据见根目录 `AGENTS.md`。该清单是实施目标，未完成步骤不代表现有能力；当前仍使用下文说明的单 Workspace 串行与原生前端，用户 CLI 在页面/API 承接前保留。长期取舍见 ADR-048。

系统仍面向可信单机使用：没有账号、多租户、公网鉴权、分布式锁或 Agent Graph。可选 Docker 只隔离 Shell 命令，不是覆盖文件工具与 Provider 的完整 Agent 沙箱；默认 Local 使用本机权限。状态根锁不等于工具安全隔离。AGENTS.md 是模型指令，不会扩张 PolicyEngine 权限。生产代码的 import、--help、测试与离线评测不需要 API Key 或网络。

完整验证门禁见 AGENTS.md。测试创建隔离目录和独立状态根，Git 测试使用测试仓库；不使用或删除用户级真实状态。

## 生产能力合并与演进边界

`todo_write` 的 `depends_on` 使用已有稳定 id，按完整原子快照检查存在性、重复、自引用、成环及依赖完成状态；旧事件无该字段时投影为空数组，不改写日志。省略依赖保留旧值，显式 `[]` 清除；不能删除仍被引用的项。依赖图是计划约束，尚未成为工具或子 Agent 调度器。

`WorktreeService` 不在 import/构造时写文件。记录与 checkout 位于源 Workspace 外的状态根，校验注册路径、分支、base commit、git-common-dir 和 Workspace 身份；创建前持久化身份，缺口记录保留用于人工检查。Host 控制面 `manage_worktree()` 持有 Workspace 锁，写入专用管理 Session 的 `worktree.requested` / `worktree.completed` 与 Run 终态；Git 在线程中执行时，即使调用方取消也保留所有权直到结果已记录。CLI 是用户管理入口，未向模型注册 worktree 工具，不绕过模型的 ToolExecutor。删除期间关闭目标 checkout 的 Workspace/Session admission，成功后移除没有 Session 的 checkout 注册。删除拒绝文件变动、ignored 内容、base 之后的提交、隐藏文件变动的索引标记或关联 Session；保留分支，没有 force/discard 通道，不自动处理旧目录。

`CommandExecutor` 返回 `CommandResult`，携带退出码、stdout、stderr、超时、耗时和精确省略字节计数；`ToolResult.metadata` 向 canonical `tool.completed` 透出这些字段。默认每流保留 25,000 字节头尾，实时消费 pipe，不先完整缓冲再截断。模型能看到退出状态，工具错误来自退出码/超时；注入返回字符串的自定义执行器继续可用。Shell 丢弃的中段不进入日志或分页存储；这与非 Shell 全量结果的请求侧预览不同。取消会等待进程树清理，不伪造完成结果。

能力扩展围绕可验收的软件工程闭环：只读 worker 复用主 Runtime，写入 worker 使用受管 worktree，经独立验收后由用户显式采纳；取消与预算仍归 Host 管理。容器执行通过 `CommandExecutor` 替换边界进入；代码结构检索与真实 embedding 通过现有只读检索工具进入；OTel 作为 Event Log 的可选观察出口，不形成第二套事实源。下文记录这些能力的当前实现。后续若确有跨阶段编排需求，LangGraph 只作为外层 adapter；Redis、Temporal、外置向量库按已有性能或调度证据决定，不作为基础依赖。

覆盖率覆盖全部现存生产源码，mypy 不再通过 legacy 或 tools 通配排除；删除的教学测试由生产依赖校验、真实 worktree 生命周期和结构化命令测试替代，数据库迁移及 HTTP/SSE 兼容测试继续保留。

## 受控只读子 Run

`workers.py` 提供不可变 ReadonlyProfile、Git 当前源码快照、角色协议与严格报告校验，没有第二个 Agent loop。Host 的 delegate_readonly 是经过 ToolExecutor/PolicyEngine 的动态工具，默认关闭。父 Run 持有源 Workspace 所有权，创建绑定该 Workspace 的独立归档 Session，在自己的执行作用域内直接复用同一个 `_execute`，将全部文件根覆盖为独立快照；子 Run 不重新争用父锁，避免同 Workspace 等待死锁。常规根任务继续串行，最多两个内部只读快照子任务可并发。子身份记录在 run.admitted/start 的 parent_run_id、worker_role、snapshot_id；普通接收和 Continue 按持久身份拒绝对子 Session 执行，API 拒绝附件。

子 Run 不读取父消息、不连接 MCP、不使用 custom handlers/Skills/RAG/delegate，目录和执行器仅含四个安全读取工具。深度为 1，每父 Run 最多 4 个子任务，8 步/4,000 Token 软上限/60 秒批次期限；在开启父 Token 上限时按剩余量分配。worker.started 保留源 checkpoint、manifest 和排除数；worker.completed 保留子用量和报告校验状态，父 usage 为 inclusive，子 usage 为 exclusive。未知失败请求消费不估算。父取消或超时先取消并等待子终态，再删除自己创建的快照；重启不自动重派或采纳结果。

## 可选结构切块与观测出口

显式 code_structure 模式通过懒加载 tree-sitter Python/JS/TS grammar 提取类、函数和嵌套符号，heading携带符号路径；装饰器/UTF-8/CRLF行号按真实源码保留。大符号继续窗口化，class骨架与方法分开避免复制整类。build统计报告解析文件数与fallback原因；窗口/结构模式、parser版本组成chunking_profile/fingerprint，在写事务内复核，变化必须force。缺依赖/不支持语言/解析损坏时回退窗口；默认不导入parser。检索eval可对相同复制语料分别运行两种切法，记录实际提交的chunking指纹和hit heading，不以机制测试声称质量提升。

telemetry.py 的 build_run_trace 是纯日志派生；export_run_trace 只在显式调用时懒加载SDK，使用局部TracerProvider和官方IdGenerator/SpanProcessor，保留历史时刻与确定性哈希identity，不修改全局provider。CLI收集所选Run的Session及worker.started引用的子Session，Continue提供link，缺失来源标记unobserved。默认JSON无网络，OTLP endpoint必须显式。固定元数据allowlist阻止正文/参数/结果/密钥进入出口，exclusive模型用量避免父子doublecount；没有响应的重试消费和账单不推测。导出失败不改变执行事实。

## 写入交付与独立验收

DeliveryController 是 Host 的控制面组件，无独立循环、Store 或状态库。prepare/run/verify/apply 使用源 Workspace owner；需要时再获取 checkout owner。每次操作建一个归档 control Run，delivery.prepared/verified/applied 为唯一事实，查询在源锁内重读最新状态，避免排队请求复用旧通过结论。worktree 继续由 WorktreeService 管理，receipt绑定干净源基线、路径/common dir、分支、base、保护清单；build_patch使用临时index与no-filters对象写入，不修改真实index。补丁文件是状态根的不可变派生产物，canonical receipt记录hash/checkpoint；不是第二份运行状态。

Writer通过同一durable admission创建独立checkout Session/Run，execution_config记录backend/image/disable_mcp/writer_delivery_id。Workspace后续Run、Continue和重启从事件派生执行配置，显式Local可由下一次控制面run选择；空image拒绝，不降级。根任务仍串行；容器仅替换CommandExecutor，不改变文件工具边界或Provider执行位置。verify使用ToolExecutor/PolicyEngine显式命令，绑定检查前后相同checkout checkpoint与patch；模型不能改受保护测试/配置伪造验收。apply须最新verified=passed，重读实际patch并再次验证source+checkout；不自动暂存或提交。apply在线程内已生效后被Stop，仍先落delivery.applied，再记interrupted，不假装撤销已发生的Git写入。

ContainerCommandExecutor通过Docker create/start/rm、argv执行和有界输出，使用本机endpoint、预拉镜像、非root、network none、只挂载cwd、资源上限与readonly验收。创建/清理阶段屏蔽重复取消并有独立期限，运行停止后必须确认rm。ContainerCleanupError携outcome_unknown，ToolExecutor保留此元数据；Host不把它变成tool.completed，而将Run停驻并按canonical tool.outcome_unknown隔离可能仍在写的Workspace。Delivery在源及checkout入口都检查该状态；readonly验收未知不宣称通过，也不误隔离原项目。普通同步处理器在多次取消下继续shield等待真实结束，已知结果提交后才释放owner。

acceptance_tests工程评测使用一次性Git fixture，原测试复制到外部临时目录并保存SHA清单。基线必须退出1；结束后测试源/数据清单不变，清理fixture的派生bytecode，再以-I/-B独立Python、先导入stdlib unittest后加入候选源码，运行原版测试。check_project通过唯一ToolExecutor/Policy留下独立Run证据，不接受模型自判。report增加verification字段；baseline/verification run id、exit/timed_out、manifest与tests_unchanged可追溯。测试、数据与模型预算均有限，失败消费不推测；这些功能测试不构成恶意代码隔离证明。

CI 分开验证默认依赖、可选 parser/OTel、Docker 与浏览器：optional-extensions 安装全部 extras；container job 显式预拉测试镜像后开启真实 readonly boundary 测试；web job 固定 Playwright 版本并安装 headless Chromium，严格模式缺浏览器必须失败。默认离线门禁仍不依赖 Key；未执行的远程 CI 不能当成本机真实 Docker 验证证据。


## 请求用量账本基础（U1 进行中）

`usage.py` 提供纯供应商归一化和 `build_usage_ledger(events)`，不读数据库或网络。标准字段为 input_tokens/output_tokens，以及可选 cache_read_input_tokens/cache_creation_input_tokens/cache_miss_input_tokens/uncached_input_tokens/reasoning_tokens；缺失不补零，严格拒绝 bool、负数或字符串计数。OpenAI 缓存/推理字段为总量细项；Anthropic 的 input_tokens 先与缓存读取和创建求和为完整输入，另保留未缓存输入。该区别依据[Anthropic 提示缓存文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。

新账本按 Run 与 attempt_id 唯一归并 model.attempt_started/finished；同请求重试使用不同尝试身份。已有尝试事实的 Run 不再累计 model.response、context.summary_generated、worker.completed 或 run 终态中的汇总。缺结束事实或缺 usage 保持未知；reported 和 estimated 汇总分别输出，缓存细项不再额外加入输入/输出总量。只传父 Run 得到 exclusive 消费，汇总子任务须提供子 Run 自身事实，不能把父 inclusive 汇总相加。重复事件重放不增加消费，冲突的尝试事实拒绝静默改账。

无尝试事实的历史 Run 仅回退已观察到的 model.response/context.summary_generated，标记历史证据不完整，不补造请求、重试或费用。JSON/OTel trace 对新 Run 直接派生尝试 span，仍只输出固定类别、哈希身份和数字，不导出请求参数或模型正文。Host与用量API已接线，流式供应商及embedding等后续验收仍见U1清单。

## 临时流式预览（A6/A7）

Host对具有stream方法的主调用消费text_delta/response_completed，完整响应之前不派发工具；辅助摘要保持complete。预览每Run最多16,000字符、最多64个活跃条目，完成/失败/取消删除，不写Event Log。已知凭据保留足够跨片lookahead后脱敏，过长密钥禁预览而非泄漏。完整内容与usage仍由持久响应/attempt事实承担。SSE显式include_preview才发送无id临时帧；持久游标不被改变。流式失败/超时不自动重新请求或降级complete，未知消费保留；三类官方SDK适配已集成；Vue显示按F阶段承接。

Host的全部实际目录已接入严格工具契约，包括MCP、Skills、RAG及只读Worker过滤后的目录。每Run仅允许一次参数错误后的修正轮次，再次出现参数错误即在保存全部已知结果后失败；不重新执行先前副作用。截断响应中的完整JSON也不执行，未派发调用写tool.abandoned补齐消息配对。严格schema使基础目录实测从约2,738增加至3,283字符；上下文测试固定成本相应更新，保留独立目录上限检测，不能把新增校验成本当作Token优化收益。

## Skills 包、作用域和资源快照（S 基础）

`skill_packages.py` 仅处理有界包字节，不拥有安装启停状态。目录和 ZIP 先预检再形成 `SkillPackagePreview`；内容清单按路径、大小和 SHA-256 派生版本。`SkillPackageService` 把包物化到状态根的 `skill-packages/<name>/<content-hash>/`，临时目录完整写入后原子重命名。版本没有可变启用 JSON；可选导入回调只向调用方报告内容事实，Host 必须把启用、回退和选中写入唯一 Event Log。回调或事件提交失败可以留下未启用的内容产物，不能从“磁盘有目录”推断授权。

每次 `inspect_version` 和服务级资源读取重新计算清单与完整版本摘要，文件被替换、删除或篡改时拒绝读取。只读权限位减少误修改，不被描述为 OS 不可变保证。资源包不允许任意目录、符号链接、特殊文件、遍历路径或 ZIP 重复项，声明体积与实际读取均受限；预检不执行脚本或联网装依赖。

`SkillCatalog.discover` 默认扫描项目 `skills/`，可显式接收已经检查的用户包、启用身份及用户选择。不存在隐式同名覆盖；同作用域冲突的项目包均不可用，不同作用域要求精确身份。普通唯一名称仍保留 `load_skill` 兼容。目录最多 64 个入口、10 MB 冻结资源；单包超限和元数据错误通过 `diagnostics` 返回，既有项目文件不会自动重写。

快照对象持有不可变 `bytes`，`snapshot()` 只返回资源清单与摘要，`resource_bytes`／`read_resource` 从同一组冻结字节读取。将这些字节持久化并绑定 admission、恢复时取回固定资源、注册读取工具和受控脚本执行器属于 Host 集成责任，本模块不替代 ToolExecutor／PolicyEngine。`allowed-tools` 元数据只是声明，不能授予权限、模型配置或预算。

### Skills 主链接线

SkillRuntime 从 skill.installed 和 skills.configured 唯一事实派生用户安装和项目启用；磁盘有内容不能替代授权。接收 Run 时将冻结资源物化到内容寻址产物，admission 只存版本、资源清单、作用域和选择。Python 直接运行也保存 skills.snapshot；Continue 和重启从相同产物重验 hash，不重新采用当前项目包。skill.loaded 单独记录实际加载和正文字符数，不将字符当作 Token 或费用归因。用户版本可以查看差异并重新固定旧版；项目导入只创建新包，更新由 Git 管理，不自动覆盖源码。

加载正文后才可分页读取资源或执行脚本。仅有相应资源时才增加工具目录；run_skill_script 要求精确 scope:name、包内 scripts/*.py 或 *.sh 及有界参数，经过严格契约、Policy ASK 和所选 CommandExecutor。Plan 不开放脚本。本地 Python 脚本使用 argv 和当前 Python；Shell 脚本要求 POSIX 或容器后端，不自动装依赖。执行前暂存冻结字节，执行器完成清理后删除，容器结果未知保留资源并沿原规则阻止继续。导入和快照线程登记 Host 所有权，关闭须等其收尾再关闭 Store。

## 真实模型基线（2026-10-16）

使用已保存的DeepSeek Responses连接、deepseek-flash及reasoning_effort=none，在独立临时项目与数据库中执行普通响应、只读文件工具和三个工程fixture，各两次。每次12步、20,000已观测Token软限、300秒总等待；输出上限4,096，单响应允许越过Token软限。10次中7次完整完成，3次因Token预算停驻仍独立验收通过，六次修复的固定测试均通过且未改动。累计48次模型请求，131,359输入/7,630输出Token，逐请求usage完整、未知0；父子/终态不另加账。报告与失败产物在忽略的.task_outputs和临时目录保留，不提交源码/数据库原文；这是小样本接入基线，不是成熟成功率或成本优化效果证明。

## Vue 工作台基础与核心迁移

`frontend/` 是独立开发构建工程，组件由 Router 管理，Pinia 只保存 UI 选择、草稿、临时预览、可重建 HTTP 结果和有限事件缓存，不保存第二份执行事实。API 客户端区分后端 Run 的 `id` 与接收凭据的 `run_id`，统一规范化供组件使用；历史页按时间正序返回，最新 Run 取末项并加载完整详情。Session 全历史用量来自专用账本接口，不把历史分页汇总当总账。

`api:generate` 用显式临时 Settings/RuntimeHost 导出 OpenAPI，再生成 TypeScript 请求类型；不会调用默认 Host 或读取用户真实状态。当前后端字典型响应仍由小型领域接口描述，SSE 是独立契约。持久事件按 Run/seq 去重；`model.preview` 没有持久游标，只更新临时区域，最终响应/终态清除预览。UI 观察断开不取消后台任务。

发送前生成 Idempotency-Key；网络、超时及 5xx 等接收结果不明时，按 Session 保存原请求与键，显式重试原内容，不改成第二个任务。已知参数拒绝允许编辑。草稿与待确认提交存当前标签页 sessionStorage，API Key 不进入 Pinia/浏览器持久存储；设置保存完成后清空密码输入。HTTP 请求有 30 秒等待上限，超时不代表后台任务已取消。会话切换与刷新使用代次检查，旧响应不覆盖新选择。待审批时 MessagesProjection 的不完整工具历史 409 只延用上次可见消息，运行详情和审批仍正常更新。

开发服务器仅监听回环地址，HTTP 代理保留 Host 通过 API 同源校验；生产仍计划同源托管构建产物。Markdown 子集只用 Vue 文本插值与代码块，不执行 raw HTML，不创建任意协议链接。页面支持跟随系统的深浅主题和窄屏导航；生产资源打包、完整管理页面与旧入口删除仍待后续步骤。


### Skills 管理 HTTP 与 Vue

`http_api/skills.py` 将本机目录、ZIP和内置包预检/导入、配置替换、版本差异、资源分页及Run加载记录接入同一Host。预检只读；导入可要求expected_version，在重新读取源后先比对摘要，再物化该次预检的固定字节，避免检查后再次读源产生不同内容。配置可带expected_configuration_hash；单服务事件循环在同步比较和追加之间没有await，旧页面冲突返回409。管理查询将损坏配置变成可见诊断，但运行admission仍拒绝损坏目录，不能降级获得执行能力。

`http_control` 先追加mutation_started，完成后追加completed/failed；状态源仍是Event Log。重复键完成时直接返回原结果；只有started时结果不明，保守拒绝重放。磁盘导入和数据库回执不是跨资源原子事务，崩溃可能留下未启用内容或待核查的项目目录；页面通过状态查询帮助核对，不能把未确认当失败后再次执行。

SkillsView 区分用户安装、项目启用、主动选择和Run实际加载。网络不确定的写请求只把SHA-256指纹与幂等键放入标签页存储，不持久化本地路径或ZIP正文；成功后删除待确认键，后续同内容操作可产生新的用户意图。导入预览随来源/作用域变更失效；页面、项目及资源请求用代次隔离，迟到响应不能覆盖新选择。Run加载记录读取canonical session_seq后规范化为seq；SSE订阅skills.snapshot/skill.loaded仅触发事实刷新，不把选择等同加载。
