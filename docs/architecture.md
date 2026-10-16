# Eventide 架构

## 核心关系

```text
RuntimeHost
  └─ Project（源码项目）
      └─ Workspace（源目录或 Git 执行副本）
          └─ Session（固定 Workspace/cwd）
              └─ Runtime Event Log
                  ├─ MessagesProjection
                  ├─ RuntimeStateProjection
                  └─ ContextBuilder
```

RuntimeHost 是进程内唯一执行 Owner，持有状态根的 OS 文件锁、RuntimeStore、Provider 配置、SessionManager 和 Workspace 锁。FastAPI 与 Python 开发/评测接口直接复用 `RuntimeHost`；不再保留旧 Python 门面。首版不含后台 daemon 或 IPC client：同状态根已有 Host 时第二个进程失败。进程终止由 OS 释放文件锁。

Workspace 保存稳定 ID、规范路径、Git 根、名称和创建时间；Git 子目录统一绑定仓库根。Session 的 Workspace 绑定不可改变；直接源运行按该绑定解析 cwd，显式快照或 worktree 运行按 canonical admission 固定的执行副本解析。同 Session 串行，直接源 Workspace 运行串行，独立 Plan 快照和写入 worktree 可以并发。Host 以 run_id 索引活跃执行，服务关闭或用户取消时先停止异步 Provider、MCP 与 Shell 子进程；同步线程工具结束后才释放 Workspace 所有权。

主要实现：`host.py` 管理生命周期与 Agent loop；`workspace.py` 管理路径、锁和 Git evidence；`store.py` 保存身份与事实；`projections.py`、`context_builder.py` 负责投影；`skills.py` 负责 Workspace Skill 的发现、快照和按需加载；`task_plan.py` 定义有界计划、依赖校验和紧凑提示；`worktrees.py` 提供注入 Workspace/状态根的 Git checkout 生命周期；`attachments.py` 管理 Session 附件生命周期、文本内联与多模态引用水合；`run_policy.py` 以纯函数定义每次 Run 的模式策略（plan/agent）；`model_catalog.py` 提供探测式模型列表辅助；`rag/` 负责 Workspace 文本切块、SQLite 混合索引、检索工具与离线检索评测。生产基础工具位于 `tools/runtime_catalog.py`，动态 Runtime 工具由 Host 注入，不导入 legacy 注册表。

Project 身份由 RuntimeStore 在 SQLite 事务内建立，按已验证的本地 Git common directory 聚合主 checkout 与 worktree；不使用 remote URL 合并独立 clone。注册执行副本会同时登记缺失的主 checkout。bare 仓库没有可运行主目录时，第一个注册的 checkout 固定为 source；非 Git 目录独立成项目。身份表仅保存不可变关联，全部运行状态仍由 Event Log 派生。原普通目录后来初始化Git时可补齐common-directory证据，不改变项目、源或Session身份。

v4 迁移遍历原 Workspace，补齐一次性项目关系，保留 Workspace/Session/Run 身份、cwd 与历史事件字节。缺失或无法验证的历史路径保留独立身份，不在后续打开时偷偷重组。新 run.admitted/run.started 保存 project_id/source_workspace_id；交付 checkout 自动归属同项目，只读 worker 保留源 Workspace 绑定并使用已有 snapshot_id，不注册会消失的快照目录。Continue 从固定 Session 绑定得到同一项目；旧事件不补造字段。

Project 查询聚合所有副本 Session，不改变原 Workspace API 的过滤语义。源 Workspace 有其他执行副本时拒绝删除，任何含 Session 或 canonical admission 执行引用的副本仍受历史保护。Project 身份不是调度器或锁；U2按该身份共享预算。直接源运行使用 Workspace 锁，Plan 快照及写 worktree 使用下述独立执行所有权，P3以同一事实链实现4根/2模型/20等待的持久调度。

## 普通 Run 的重复失败窗口

`tool_loops.py`从当前 Run 及其显式 `continuation_of` 链的 canonical 工具结果与 checkpoint 重建最近三次实际结果，不保存计数表或缓存。普通新 Run 不继承窗口；Continue 和服务重启不清除证据。只对有非零exit_code、timeout或Executor捕获真实异常时记录的exception_type依据的 `execution_failed`／`timeout` 结果计数；成功、不同参数／错误或不同源码 checkpoint 打断窗口。仅凭字符串`Error:`前缀的旧判错不纳入窗口，避免文件正文误触发；旧is_error呈现不在此重构。预算、未知结果、无效参数／契约和权限拒绝不会被改成普通执行失败。未派发的 `tool.abandoned` 只补齐协议配对，不算已执行结果，也不抹掉失败证据。

`tool.completed.failure_fingerprint`由工具名、实际参数、错误正文及稳定错误字段在脱敏前计算，仅保存hash，避免两个不同秘密参数被脱敏成同一个值后误合并。耗时等易变元数据不参与，错误正文不做模糊匹配。旧事件缺少指纹时不补造历史精度；相同错误只是保守的字面证据，不代表语义判断。写工具复用已有post-tool checkpoint；只读工具出现候选窗口时才额外复核一次源码，避免每步扫描全仓。没有有效checkpoint时退回原步数上限。

窗口满足阈值并复核无源码进展后，保存`tool.failure_loop_detected`，包含threshold、matching_failure_window、三条call引用、checkpoint_hash及continued_failure_chain，终态为`interrupted/repeated_tool_failure`。计数明确表示最近三条匹配窗口，不冒充整个历史失败次数。已发生的结果和唯一模型账本保留；模型同批剩余工具追加`tool.abandoned(dispatched=false)`，不伪造prepared或执行副作用。取消、异常和未知结果保持原有优先语义；停止前按原路径保存恢复checkpoint。证据范围沿用Git可见源码或固定快照，忽略文件和外部服务状态不包含在内。外部进程仍可能在检查之间编辑文件，此机制不是文件系统隔离或测试通过证明。

## 根 Run 执行副本与所有权（P2）

`RunRequest.execution_target` 默认 source，可显式选择 snapshot 或 worktree；snapshot 仅允许 Plan。Host 接收快照时同时冻结模型配置、Skills、源 checkpoint、完整源码 manifest、相对 cwd 与 snapshot_id，写入同一个 run.admitted 事务；Session/Workspace/Project 的身份和绑定不改。execution_snapshots.py 将 SourceSnapshot 的有界文本复制持久化到状态根 execution-snapshots/<摘要>，只保存内容产物，不创建第二个运行事实源。相同源码可复用只读内容，但每个 Run 仍有独立执行所有权和取消身份。

准备阶段短暂获取源 Workspace 锁，复制、冻结 Skills 后再次验证源 checkpoint；取消准备时等待复制线程完成才释放锁。运行阶段获取 Session 锁与 snapshot_run:<RunID> 所有权，不持有源锁。工具 cwd、项目指令、Skill恢复和checkpoint全部从固定副本解析，模型/工具仍走唯一 `_execute`、ToolExecutor、PolicyEngine 和 U2 请求入口。Plan禁止Shell、写工具与Skill脚本，快照不连接MCP、源索引或嵌套worker，以免读取可变源数据。

每次开始、Continue和成功终态重验资源摘要、大小、链接边界；副本损坏明确拒绝。Continue复用原admission绑定，源端漂移不影响副本，启动恢复只写停驻事实，不重放请求。快照中未知工具结果保留并阻止该次Continue，不将已知只读副本的异常扩散为源Workspace写入隔离。用户停止只影响精确Run；同Session只接受一个未完成请求，Python/HTTP共用接收与P3调度；父子任务和模型额度的分离见后文。

### 写入副本链与短期 Git 协调

execution_worktrees.py 用 WorktreeService 从干净源 HEAD 创建副本，Run admission 保存完整受管身份、源checkpoint、相对cwd及实际 execution_workspace_id。后续新 Run 从该 Session 首次 canonical admission 复用副本，不从当前 HEAD 重建；Continue 和 abandon 同样保留绑定，Session 的逻辑 Workspace 不改。已绑定副本后拒绝 source/snapshot 切换，防止历史工具内容和实际文件不一致。首次创建失败保留已有受管记录用于人工检查，不伪造接收成功。

执行键为 snapshot_run:<RunID>、worktree 实际 WorkspaceID 或 source WorkspaceID；Session 锁始终串行。接收在实际副本所有权内冻结 Skills/checkpoint，执行前重验；恢复和未知副作用按实际副本判断。源漂移不阻止现有副本工作，但副本漂移阻止已接收运行和 Continue。首次创建才检查源干净，后续脏修改必须保留；运行引用由 canonical admission 查询，不能移除虽无直属 Session 却被源 Session 使用的副本。

文件根、项目指令和索引按实际副本解析；Skill 使用逻辑源启用配置及实际副本固定资源。只读 worker 从实际副本创建快照，子 Session 绑定实际 Workspace 并保留父项目预算归属，不继承父的可写 execution_binding。所有副本暂禁外部 MCP。SourceSnapshot 排除状态目录时只排除位于源码内部的状态树，不把状态根内的受管 checkout 全部误排除。

项目级 asyncio 锁协调受管 Git 创建/移除、源码采集及 Shell/Skill 命令；锁顺序先执行 Workspace、再项目 Git。CoordinatedCommandExecutor 在真实调用期间持锁，显式委派 backend_kind 和 execute_argv，保留本地 Python 的 sys.executable/字面参数及容器固定资源的跨 UID 权限。等待模型不持项目 Git 锁，普通文件工具也不串行化；本机 Shell 的项目外副作用不由 worktree 隔离。


## 存储与事件契约

默认用户状态根中包含 `runtime.sqlite`、`host.lock` 和按需生成的 `secret.key`。EVENTIDE_STATE_DIR 优先；SQLite 使用 WAL、外键和 schema_migrations。当前 schema 版本为 4，版本 1/2/3 在同一 Runtime 数据模型内自动升级；未知版本和直接作为 RuntimeStore 打开的未版本化旧数据库明确拒绝，不自动删除。`migration` Python模块通过独立只读连接校验旧 TraceStore schema，再把数据以单个目标事务导入当前库。

| 表 | 职责 |
|---|---|
| projects | 稳定项目身份、固定源 Workspace、本地 Git common directory（可空） |
| workspace_projects | Workspace 到 Project 的不可变关系及 source/worktree 类型 |
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

全局控制面使用 `facts_by_type(types)` 在 Store 锁内读取指定类型的全部非 partial 事实，按 SQLite rowid 的追加顺序排序，不受列表导航的 100 条分页影响；控制面不直接访问私有 SQLite 连接。

主要事件：

- run.admitted、run.started、message.user、model.request、model.response、model.retry；
- tool.prepared、tool.completed、tool.abandoned；
- approval.required、approval.resolved；
- workspace.checkpoint、context.configured、context.compacted、context.trimmed、context.summary_requested、context.summary_generated、context.summary_claimed；
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

摘要资格由 `ContextBuilder` 在调用 completion callback 前，通过 `RuntimeStore.transact_facts` 原子读取并追加 Session 级 `context.summary_claimed`，沿用唯一追加式 Event Log，不新建节流表。事实只含策略版本、provider/model、完成边界、摘要输入历史 hash 与认领时间，不复制正文或 usage，不进入 MessagesProjection。相同完成内容不再次认领；后续完整 turn 的投影新增至少 4,000 个序列化 Unicode 字符且距上次认领至少 60 秒才可重试。首次无此门槛，force 不绕过；时钟倒退保守保持冷却，没有 sleep。Session/模型分别计数，SQLite 事务防止两个 Store 同时认领。

认领在模型槽和预算派发之前，因此取消、预算拒绝、未采用、摘要无效或崩溃都可能留下没有真实消费的认领；它不是模型 attempt，也不代表 checkpoint 或预算结算。实际返回、失败和取消继续由原 Host completion callback 记账。重启不清空认领；有效 checkpoint 直接复用，失效 checkpoint 不能绕过节流重新花费。跳过原因随 `context.trimmed.summary_skipped` 记录；既有工具结果折叠后仍超限，沿用 `context_overflow` 失败，不发送主模型请求。这里的 4,000 字符和 60 秒是保守机制参数，尚无真实任务成本改善结论。

Host 的两阶段接收由 `admit_run` / `admit_continue` 和 `start_admitted_run` 组成：先在 Workspace 锁内预留精确 Run/Session 身份，通过可取消但会等待线程结束的恢复和 checkpoint 检查，再由 `RuntimeStore.admit_run()` 在一个 SQLite 事务中写入 turn/run、`run.admitted`、用户消息或恢复事实和 checkpoint。附件验证、事务提交与 used 缓存写入之间没有 await；跨文件和 SQLite 仍不是共同事务，因此附件 API 同时查询 canonical 引用来保护生命周期。202 前同步登记执行 Task，使立即取消、关闭和配置切换都能找到所有者。Python直接调用与HTTP共用持久队列，忙源在接收前拒绝；临时ticket/lease只管理进程内所有权，重启事实仍以Event Log为准。

ContextBuilder 的可选 completion callback 把生产摘要接入 Host 的模型 timeout/retry 和统一用量账本；不提供 callback 的离线调用者保持 Provider.complete 行为。`context.summary_requested/generated` 不进入 MessagesProjection，也不增加主 Agent 步数；成功生成的 usage 在是否采用判断前记账，新 `context.compacted` 不重复携带 usage。旧 compacted usage 仍可投影兼容。`run_token_limit`（默认 0）检查成功响应返回的 input+output tokens，包括摘要，在下一次主模型或摘要请求前停驻；它是按 Run 计数的软上限，不估计重试未返回的消费或视觉 token，不保证账单硬上限。

## 中断与 Continue

启动时扫描缺少终态的 run，不调用模型。崩溃可能正好落在「副作用工具已提交 tool.completed、它的 checkpoint 还没写」之间，此时 Host 用文件系统本身当证据：`changed_since()` 比较 Git 可见路径（已跟踪加未忽略的未跟踪）的 mtime 与已跟踪文件是否缺失，基准是该 run 最后一条持久事实的时间戳。全部早于该时刻且没有缺失时，当前工作区就是 Host 死亡时的状态，Host 先补记 workspace.checkpoint 再写 run.interrupted，Session 停驻但仍可 Continue；查到改动则不补 checkpoint，并把改动路径写进终态 payload 的 `gap_files`（最多 20 条），让用户知道该核对什么。无法作答（非 Git、不可读、文件数超过扫描上限）时按没有证据处理，行为与本次修改前一致。单次 run 用尽步数预算（`max_steps`，默认 30）时所有工具结果已提交，Host 先记录工作区 checkpoint，再写入 run.interrupted，Session 停驻，用户可 Continue 到新 turn 并获得新的步数预算。存在计划时还有一条更小的软边界：同一条 `in_progress` 任务连续占用超过 `task_max_steps`（默认 12，`0` 关闭）个 step 时同样停驻，终态 `reason` 记为 `task_step_budget` 并在错误信息中点名任务 id。用户停止运行（`reason=cancelled`）时也先补记工作区 checkpoint 再写 `run.interrupted`：取消可能正好落在"副作用工具已提交、它的 checkpoint 还没写"之间，补记这一步保证停驻后仍可 Continue。该计数只在本次 run 内存在、不落盘，因此 Continue 后重新计数；`max_steps` 始终是硬上限，Host 不因为单任务超时而改写计划状态。

直接源运行的用户主动 Continue 必须通过 Workspace 锁内检查（副本运行改为校验实际执行副本）：

1. 最新 run 为 interrupted，且 Workspace 为可验证的 Git 仓库。
2. 当前 HEAD、暂存与未暂存 binary diff digest、未跟踪文件路径和内容 digest 与最后可信 checkpoint 相同。
3. 不存在结果未知的非只读 prepared 调用；MCP 即使声明只读，也采用保守的恢复策略。
4. 已完成副作用工具后存在 checkpoint；无法采集证据时拒绝继续。

Continue 从来源 `run.admitted` / `run.started` 继承 Plan 的只读限制与相同 Provider/endpoint hash 下的模型；原 Agent 自动审批不延续，其他模式归为 Auto。Provider 或 Base URL 改变时使用当前默认模型，旧记录缺 mode 时按 Auto。恢复事实扫描整个 Session 未闭合的工具与审批，覆盖执行前已被停止的 Continue 链。Continue 创建新 turn/run，并以唯一 continuation_of 关联来源 run；不会重放旧工具或重复用户消息。未完成 read_file/glob/compact 和确定尚未派发的调用写入 abandoned，补齐下一次模型请求的 tool-result 结构。只读结果也不伪装成成功。续跑的 `run.started` 记录 `resumed_task_id`（来源 run 停驻时的 `active_task_id`），system 中写明正在接续哪一条任务。

没有 HEAD、非 Git、存在 submodule、证据采集失败、未知副作用或源码变化时维持 parked 并解释原因。用户可以显式 abandon：Runtime 创建一个不调用模型的关联 run，把未配对调用记录为结果未知且未重放，保留全部历史并解除 Session 停驻。范围仅为 Git 可见源码；ignored 文件、外部 MCP 服务和 OS 全局状态不在快照内。不是指令级恢复，也不是操作系统沙箱。

## 入口、模型与 MCP

`api.py` 是 FastAPI 组装入口，保留 `create_app(runtime=None)`；现有 HTTP 路由按工作区、Session、Run、模型生成参数、Provider 配置、附件及 SSE/导出拆入 `http_api/`。请求模型集中声明；每个 router factory 接收同一应用的 `ApiContext`，其中只保存 Host 引用、审批 Future、暂存 reservation 与 Task 集合，不是新的运行状态库。每个应用独立创建上下文，禁止模块级共享可变状态。Host/Store 仍管理接收、执行和全部事实；应用 lifespan 在退出时取消并等待自己跟踪的 Task 后再关闭 Host。

容器部署的本机转发可通过启动配置 `EVENTIDE_LOCAL_PROXY_PEERS` 指定最多32个精确IP，默认空。Settings启动时校验并规范化，ApiContext创建时固定副本；API及模型设置不能修改该信任边界。管理守卫仅在socket peer精确匹配时追加放行，并始终检查回环Host和匹配Origin（包括使用该守卫的GET）；缺少Origin或cross-site仍拒绝。`eventide-server`禁用Uvicorn代理头解析，不信任请求中的转发地址。部署必须保持宿主回环发布；这不提供公网身份认证，也不自动信任私网段或Docker网关。

同源写入守卫仍在应用级，领域路由继续执行原本的回环、身份和权限检查。统一 HTTP 错误包含 code/message/detail，保留状态码、HTTP 异常响应头以及字符串或列表 detail；RequestValidationError 不返回原始 input/ctx，内部异常仅返回通用错误。OpenAPI 使用共同 ErrorResponse 描述该契约；模型连接探测的 HTTP 200 失败结果不改为协议错误。当前领域HTTP路由统一注册，用量查询仍覆盖完整历史，SSE 持久游标及 opt-in 临时预览语义不变。Skills、索引、离线评测及交付管理已有专用领域路由；真实模型评测也已接规划确认和共享模型账本；真实效果另按B3/U5验收。

HTTP写请求先校验浏览器同源与本机Origin；无Origin的可信非浏览器路径沿用现有权限边界。Run/Continue必须用Idempotency-Key生成操作/Session隔离的确定性Run身份，在admission原子保存内容指纹；重复提交先查持久接收事实，再检查busy/parked，不重新启动已接收任务。未落库接收竞争仍拒绝为busy，不通过内存缓存伪造持久成功。

生成参数使用 `generation.py` 严格校验和能力描述。模型默认/会话覆盖保存在 `generation.configured` 追加事实中，通过 Provider、endpoint、model 的哈希隔离；模型默认管理使用归档 Session，不新增独立可变配置表。省略继承、null清除、0有效，`run.admitted/started.generation` 保存有效请求快照。Continue在原接入下继承并重验，切换接入则重新解析；摘要/只读子Run不隐式继承用户采样覆盖。`ModelRequest.generation` 由adapter映射到SDK参数，最大输出仍受现有请求字段约束。

每次模型调用记录 `model.attempt_started/finished`，同重试组共享request_id，每次真实尝试有独立attempt_id。成功保存报告usage，失败/取消无usage记unknown；事件不记录请求正文。原model.response和summary事件继续提供上下文事实，后续用量账本只以attempt事实计算，避免重复计数。

SDK内部重试显式关闭，Host统一重试，因此每条attempt对应一次SDK请求。默认最大输出也纳入有效参数快照，重启改变启动默认不会改变Continue。Session覆盖查询同时绑定模型身份，切换模型再切回仍可恢复各自设置。Chat兼容adapter保留并回传其reasoning_content私有状态，不泄漏到其他Provider格式。

三类 Provider adapter 同时提供 `complete(request)` 与可选 `stream(request)`。`streaming.ModelStreamEvent` 只有 `text_delta` 和 `response_completed`；只有后者含完整 ModelResponse 和工具调用，增量中没有可执行 ToolCall。Responses 读取 SDK 的终态对象，Chat 按调用 index 拼接参数，Anthropic 按消息/内容块生命周期拼接 JSON；各自保留必要的私有推理状态，增量只发送可见文本。收到最终对象前不会执行工具。Chat 等待结束原因并读取后续 usage，Anthropic 用累计 message_delta 覆盖临时用量，不把 message_start 的输出零值冒充最终消费。

SDK stream 在终态返回前关闭；异常、取消和消费者 aclose 同样释放连接。SDK 请求创建成功后任何断流均为不可重试错误，不能因预览失败回退为 complete 重发。适配器限制保留内容/参数/推理合计 8 MiB、128 个输出项及 65,536 个事件；超限与缺终态均不返回完整响应。这些是响应组装边界，不是请求费用上限或操作系统隔离。适配器协议已通过内存 SSE 与官方 SDK 验证，Host 的临时预览、SSE 及尝试账本集成仍按 A6/A7 单独验收。

Responses 终态为 incomplete 时，即使带有格式正确的 function_call，也保留 max_output_tokens 等原始截断原因，不能改写成 tool_use。外层 completed 但工具项仍为 in_progress/incomplete、缺少调用身份或 JSON 无效时，同样标记 incomplete；缺身份的项不制造伪调用，带身份的 JSON 错误保留审计占位。完整 ModelResponse 继续携带最终 usage，使 Host 可先记账，再终止并标记未派发的调用，不能因工具不完整而丢失已知消费。

`RuntimeStore.usage_events`在一个递归查询中选取指定Run/Session及可选持久子Run的调用事实；`usage.build_usage_ledger`纯派生去重、已报告/估算/未知及完整性，HTTP用量接口不依赖列表分页。终态/worker汇总不再次收费，历史只做不完整响应回退。价格、共享预算及Vue控制面见U2/U3章节；最终请求字符组成按下文固定方法记录，Vue按Run、Session、项目、交付及UTC日查询完整历史。

`RuntimeHost.run(RunRequest)`、create_session、Provider 配置和 RunResult 原字段保持当前契约；RunResult 新增带默认值的 turn_id、continuation_of。RuntimeHost 还提供 resolve_or_register_workspace、continue_session、session_status。

部署入口为eventide.server.main，console script为eventide-server，python -m eventide调用同一服务启动实现；仅解析host/port/初始项目目录，不保留chat/run/eval/rag等子命令或CLI转发层。帮助在读取Settings、创建Host或连接网络之前结束。服务关闭由FastAPI lifespan执行，启动失败也释放Host状态根所有权。CI通过tests/check_service.py实际启动一次性服务检查健康、Vue跳转、项目身份与零模型调用；离线评测直接调用Python接口。

HTTP提供项目/会话管理、固定cwd、普通Run/Continue、精确停止/审批/放弃恢复、canonical JSONL及trace。普通接收返回202，执行结果经Run查询和SSE读取；会话归档与空会话删除继续受版本与历史门禁保护。源码和任务状态仍由同一Runtime投影，不因退役终端而删除检索、评测、交付、导入或观测底层接口。

历史导入统一使用 migration 的有界只读事务，包含已提交WAL；限制源库与WAL合计64MiB、5万行、32MiB文本、单字段1MiB、JSON深度32和读取10秒。导入前验证引用与类型并脱敏，不读取Provider配置。旧消息保留为 message.imported，原始事件一律封装为 legacy.event，旧总量放在 legacy.run_summary；只生成服务自己的历史run.started/终态。会话归档且带受信任legacy_history控制标记，不能作为Agent或Continue入口；旧事件不能伪造执行授权或现代调用账本。源文件不写入，目标身份冲突或完成回执失败回滚整批。Web可用固定预检命名空间稳定映射身份，防止与运行控制身份冲突；Web通过HistoryImportController接收预检/确认操作，使用共享根任务队列和单一控制Run；预检生成受管脱敏JSON，事件固定hash/大小/计数，确认只接受相同项目的已完成预检。原路径仅在在途内存中传递，事件不保存原路径。导入会话/运行/历史事实及控制成功回执/终态在同一目标事务提交；取消等待SQLite线程结束，已提交结果保持completed。重启停驻未完成任务，不自动重放。旧消息使用session_seq游标分页读取，内容仅作为文本展示。

模型配置为 Host 级，活跃 run 期间不允许修改。RunRequest 可携带可选 `model`，只覆盖该次 Run 的 ModelRequest 和 checkpoint 身份，不修改 Host 默认配置或重建 Provider 客户端。Provider 客户端惰性创建，连接检查不切换活动 Provider。密钥继续使用 Fernet；主密钥优先 EVENTIDE_SECRET_KEY，否则状态根 secret.key。解密失败不退回明文。凭据管理及 Workspace 注册/移除仅接受本机回环请求。

每个 run 按 Workspace 读取 mcp.json；MCP SDK transport 在同一 owning task 内连接和关闭，避免跨 task 的资源退出。各 MCP Server 独立连接和报告错误，单个 Server 的连接、发现或关闭异常不覆盖其他能力及已完成结果。模型请求、MCP 连接/调用和本地 Shell 分别受 `EVENTIDE_MODEL_TIMEOUT`、`EVENTIDE_MCP_TIMEOUT`、`EVENTIDE_COMMAND_TIMEOUT` 限制；取消 Shell 时终止其进程树。所有工具统一进入 ToolExecutor/PolicyEngine，文件工具内部再次检查路径。生产基础目录包含文件、Shell、compact、`read_tool_result` 和 `todo_write`；前者只能按当前 Session、指定 Run 和 call identity 读取 canonical `tool.completed`，单页最多 12,000 字符；后者替换当前 Session 计划，最多 20 项且只修改 Event Log，作为 recovery-safe 工具不会触发 Workspace checkpoint。Workspace 存在有效 Skill 时动态加入 `load_skill`；存在与当前 embedding fingerprint 匹配的知识索引时动态加入只读 `search_knowledge`。旧教学 task graph/teammate/cron、全局 Skill loader、同步 Agent/LLM、context/hooks、模拟 MCP 与全局注册表已经删除；不再提供 `AgentRuntime` / `TraceStore` 别名。Skills、摘要、恢复与审批均沿用已有生产实现。离线 Eval 使用 scripted Provider；离线与 live 评测均显式关闭真实 MCP，并为每条用例创建独立临时项目和评测数据库。

RunBody 支持每次 Run 的 `mode`（auto/plan/agent，未知值按 auto 处理）、`attachment_ids` 与可选 `model`，策略在 `run_policy.py` 中保持为纯函数，wiring 在 Host。`plan` 把模型可见的工具目录收敛为只读白名单（`read_file`/`glob`/`read_tool_result`/`compact`/`todo_write`/`load_skill`/`search_knowledge`），system 追加只读声明；executor handlers 同步裁剪到可见名字，模型幻觉出的写调用不会命中处理器；MCP 服务仍按 Workspace 配置连接，只是其工具不进入目录。`agent` 在该 Run 内自动允许 ASK 决策，不再等待审批处理器，`approval.resolved` payload 带 `auto: true` 且 `author="runtime"`，审计事实完整。附件上传采用 JSON base64、单文件 ≤10MB，存储在状态根 `attachments/~<sha256(session_id)>/{attachment_id}`，不属于 SQLite schema；内部 v3 记录保存精确 session_id，读取、标记、删除和水合均校验；新目录始终使用完整 ID 的 SHA-256，拒绝链接逃逸与重名覆盖。旧安全目录保守只读兼容，无法确认归属的旧记录拒绝访问。清单默认只返回未使用附件，used 由文件缓存或 canonical 引用共同确定；下载保持原始媒体类型，删除只允许未使用附件。HTTP 在返回 202 前校验归属与当前 Provider 能力。UTF-8 文本以分隔符内联且单附件超过 512KB 时截断；图片和 PDF 在事件中只保存不可变引用，每次 Provider 请求前从状态根水合，避免 canonical Event Log、审计导出和字符预算复制 base64。图片映射到 Anthropic、OpenAI-compatible 与 OpenAI Responses 的各自多模态协议；PDF 只映射到 Anthropic 与 OpenAI Responses。普通二进制可以存取，但没有协议映射时明确拒绝提交。

## 工具参数契约

`tool_contracts.py` 是内置工具参数的类型来源：严格 Pydantic 模型生成目录 schema，`production_tool_catalog()` 将 Host 动态内置目录映射到同一类型，并保留当前 Skill 名称枚举。省略字段通过 `exclude_unset` 保留省略，传入的 JSON 不作类型强制转换。依赖拓扑、Session 归属和文件路径仍由既有处理器及策略验证。

`ToolExecutor` 固定 handler、只读集合及 schema 快照。未知 handler、缺契约、无效 schema 和无效参数均在 PolicyEngine/审批/实际 handler 前返回带稳定 `metadata.code` 的错误。验证成功的原始参数继续通过策略；只有文件与 Shell 工具可在验证后注入运行时 cwd。CommandResult 的 timeout/exit code 和已知未知副作用继续保留，参数修正不得成为副作用重放路径。

动态 schema 使用 jsonschema 和显式空的 referencing Registry。构造时按 dialect 遍历真实子 schema，拒绝网络/文件/相对外部引用、未知 dialect、未支持的引用关键字和无法解析的本地引用；const/default 中的普通数据不会被当作 schema。坏条目不使其他工具不可用。MCP 的 session、工具原名及 timeout 通过词法闭包绑定，模型 `_session` 等同名字段只能作为远端工具参数，无法替换 transport。

Host已对实际可用目录传入 `tools=`，归一化请求目录，并限制每个Run最多一次参数修正。定向测试覆盖严格类型、字段省略/空值、权限边界、冻结目录、局部引用、失败隔离和两个真实本地 MCP transport。

## Workspace 知识检索

RAG 是与 Runtime 状态库分离的 Workspace 派生能力。用户从知识索引页显式构建后，Git Workspace 内 `.eventide/index.sqlite` 保存 `files`、带文件/行号/标题路径的 `chunks`、float32 embedding BLOB、FTS5 trigram 索引和 embedding fingerprint；数据库可随时删除重建，不进入 canonical Runtime schema 或 Event Log。v0.4 只枚举 `git ls-files --cached --others --exclude-standard` 可见的 UTF-8 文本，跳过 symlink、二进制、超过 1 MiB 的文件以及 `.git/`、`.eventide/`。增量判断以内容 SHA-256 为准；构建在写事务外完成读取、切块和 embedding，提交时重新核对语料与 index generation，并以单个 SQLite 事务替换文件、chunk、FTS 和 meta，因此查询只看到旧或新完整快照。

C1 进一步固定 `IndexStore` 的 Workspace 目录身份。扫描拒绝非规范相对路径及任何祖先链接，打开文件后检查常规文件类型与身份，读取最多 1 MiB 加一个检测字节。索引只允许固定 `.eventide/index.sqlite` 路径；父目录、数据库与三个 SQLite 附属文件检查真实路径、文件类型与链接，数据库和附属文件拒绝硬链接。新建数据库先以独占创建取得已知文件身份，SQLite 打开后复核，失败关闭连接；构建在 embedding 完成后及提交扫描后再核对索引目录/数据库身份。链接与目录重定向拒绝，不改变 Runtime 的状态根配置或预算事实。

Markdown 按 fenced code block 外的 H1～H3 切 section，超长 section、源码和普通文本再使用 600～800 字符窗口及约 100 字符 overlap；每块保存准确的 1-based 闭区间行号。默认 HashEmbedder 用 SHA-256 token hashing 生成确定性本地向量，只验证离线机制，不声称具备学习得到的语义能力；可选 OpenAI-compatible provider 只在显式配置后惰性调用。provider、model、base URL、dimension 和算法版本组成 fingerprint，任何变化都要求 `--force`，避免比较来自不同向量空间的 BLOB。

增量复用前同时验证已存 embedding 及切块指纹与其元数据一致，损坏时要求显式全量重建；不以写入新指纹掩盖旧向量身份不明。历史索引同时缺少切块元数据时仍按原固定窗口兼容，单项缺失或内容不一致则拒绝。未变化文件不重新 embedding，新增/改变/删除统一在一笔写事务生效；计算异常、源码漂移或另一个构建已提交新 generation 时，旧快照或另一个构建的获胜快照保持可读。此处仍是可重建索引，不建立第二运行事实源；embedding 网络请求治理属于后续 U1，未在 C1 扩充。

读连接使用 SQLite URI `mode=ro`，不运行 schema 初始化。查询 embedding 在读事务外生成，随后在同一个 WAL 快照内重验 fingerprint、读取 dense 和 sparse 候选，防止重建复用 chunk ID 导致跨代融合。构建的第二次 manifest 检查在 `BEGIN IMMEDIATE` 之后、写入之前执行；它检测两次扫描之间的变化，但不等价于锁住工作区文件系统，用户仍应避免构建期间编辑语料。Host 将 SQLite 错误视为索引不可用。

查询同时执行全量 float32 余弦 Top-2k 与 FTS5 BM25 Top-2k，再用一基排名、常量 60 的 Reciprocal Rank Fusion 合并。BM25 与余弦只贡献各自排名，不直接相加不同量纲的原始分数。Host 在**每次 Run**按当前 Session 的 Workspace 检查索引及 fingerprint；有效时动态加入 `search_knowledge(query, k)`，无效或不存在时不向模型暴露。该工具属于 Plan 可见的只读集合，仍由 `ToolExecutor` 和 `PolicyEngine` 执行，调用事实继续使用既有 `tool.prepared` / `tool.completed`，不新增事件类型或副作用 checkpoint。

`run_retrieval_evaluation` Python接口 将标注语料复制到临时 Git Workspace，强制使用 HashEmbedder 建索引，按 query→期望文件计算 macro recall@5 与 MRR；普通 scripted smoke 通过显式 case 级 `rag_index: true` 在既有 Git sandbox 中预建索引。两条离线路径都不读取真实 embedding 配置、不访问网络，也不会在真实 checkout 写索引。

检索评测在同一索引、同一组 query/标签上依次执行 sparse、dense 和 hybrid。文件召回以去重的相关文件数计算；MRR 以第一条相关 passage 的排名计算，`expect_contains` 可进一步限制相关 passage。聚合前不舍入。顶层指标保持 hybrid 语义，`comparisons` 保留三路完整结果。JSON 还记录复制后语料 SHA-256、套件 SHA-256、embedding fingerprint、索引规模，以及每题耗时和 nearest-rank p50/p95；耗时是单次本机观测，不是性能基准。复制排除 `.git` / `.eventide`，拒绝 symlink，并拒绝不存在的期望文件标签。

## 评测

评测套件是 YAML 的 `cases` 列表（`evals/smoke.yaml` 覆盖机制，`evals/tasks.yaml` 覆盖任务级完成）。每条用例包含 `id`、`prompt`、离线 `script`（ScriptedProvider 步骤）、`expected`，以及可选的 `workspace`（相对路径 → 文件内容的 fixture 映射）。所有用例各自创建一次性 Git 沙箱：未声明 `workspace` 时使用空 fixture，其他情况仅接受相对路径映射；写入 fixture、`git init` 并提交初始 commit（仓库级 user identity 和 `.eventide/` 排除规则写在沙箱内）。Runtime 使用沙箱 `workdir`、沙箱内 `.eventide` 状态根和独立数据库，离线与 live 都关闭外部 MCP。每次评测统一临时产物根保存所有用例与 JSON 报告，不自动删除，不写启动 checkout 或用户状态根。报告顶层 `artifact_dir` / `report_path` 和逐用例 `artifact_dir` 可定位失败证据；Python `output_dir` 只导出报告，不复制状态数据库。

live 在更换状态根之前调用共享 `resolve_provider_configuration`；其使用 `mode=ro` SQLite 读事务读取已提交 Provider 配置（包括活跃 WAL），不初始化 schema、不获取 HostLease、不恢复历史 Run。Host 与 live 共用 `apply_provider_configuration` 的已保存配置优先级；没有保存配置时保持启动配置，密文不可解密时禁止环境密钥回退。SecretBox 解密不创建密钥，缺失/无效/不可读密钥分别明确报告；凭据只留内存，评测使用注入 Provider，不向临时库保存连接凭据。离线路径不调用 resolver，并移除启动 API Key 与真实 embedding 凭据。

判分（`_judge`）是确定性的 fs/git 证据检查，零模型调用：既有 `status`、`final_contains`、`required_tools`、`permission_denied`，任务级另有 `file_exists`、`file_absent`、`file_contains`（大小写不敏感子串）和 `git_committed`（初始提交之外存在新提交且 `git status --porcelain` 为空；非 Git 沙箱报 `git evidence unavailable`）。模型最终输出只参与 `final_contains`，从不作为完成证据。报告在 `mode/suite/passed/total/pass_rate/average_duration_ms/tool_success_rate/safety_blocks/duration_ms/results` 之外提供 `task_completion_rate`、`failure_reasons` 分布和 `average_steps`；live 模式另计 `total_input_tokens` / `total_output_tokens`，offline 模式带 `regression_smoke: true` 标记。Web报告和Python返回值保留完成率、失败原因和所有逐例结果；tests.check_offline_evaluations只做离线CI断言，全通过才返回成功。每条 `EvalCaseResult` 透传 RunResult 的 `steps`、`usage`、`status`。

评测的 `expected.successful_tools` 要求对应 `tool.result` 的 `is_error` 明确为 false；`required_tools` 仍仅检查调用是否发生，供拒绝策略测试使用。注意工具成功标志并不代替子进程退出码或文件内容验收。

`examples/rag_repair.py` 是独立离线工程闭环：临时 Git fixture → 索引 → `search_knowledge` / `read_file` → 步数上限停驻 → 重开 Host → Continue → `edit_file` / 越界拒绝 / `bash` 测试。使用真实文件工具、策略、持久化与本地子进程，但 Provider 决策是 scripted。独立 verifier 通过当前 Python 的 unittest 退出码和测试源码完整性判定修复证据，不信任最终回复或 Shell 工具的成功标志；不修改生产 Shell 返回协议，也不新增事件。

## Web 工作台与展示投影

Vue 3/TypeScript/Vite、Vue Router和Pinia提供唯一用户界面，Hash路由保留 `/workbench/` 地址。服务根地址307重定向该路径；旧原生前端、静态 `/assets` 挂载和用户终端产品已退役。FastAPI只托管构建产物，Node只用于维护构建，安装包与运行容器无需Node。

`frontend/src/api`按领域调用同源HTTP/SSE，OpenAPI生成接口类型。`stores/workbench.ts`保存当前选择、按Session隔离的状态与草稿、持久接收身份和运行观察器。切页不取消后台任务，跨项目迟到结果只更新所属缓存；未知写入保留原键，明确拒绝才允许新意图。事件客户端分别管理持久游标和临时预览，刷新从后端事实恢复；用量不按SSE接收次数累计。

项目、对话、会话管理、Skills、索引、评测、交付、副本、历史导入、历史、用量、队列及设置均由领域页面承接。历史运行按稳定身份分页，不替换当前对话；JSONL与有界Trace从后端事实导出。计划展示读取session_status，不复制服务端任务身份、依赖与证据规则；模型标记completed不等于独立验收。

`SafeContent.vue`仅将用户内容作为文本和围栏代码块渲染，不使用v-html、原始HTML或任意协议链接。审批、停止、Continue和补丁采纳始终以精确Run/Session/候选身份调用后端，界面状态不授予工具权限。

## 工程边界

本轮演进以 Web 工作台、同项目隔离并发、正式 Skills 与 Token 成本治理为目标，编号步骤及验收证据见根目录 `AGENTS.md`。该清单是实施目标，未完成步骤不代表现有能力；直接源运行继续使用 Workspace 串行，显式 Plan 快照和独立 worktree 可并发，Vue已承接用户控制面，旧原生前端和用户CLI已删除；服务部署与Python开发接口继续复用同一Host。长期取舍见 ADR-048。

系统仍面向可信单机使用：没有账号、多租户、公网鉴权、分布式锁或 Agent Graph。可选 Docker 只隔离 Shell 命令，不是覆盖文件工具与 Provider 的完整 Agent 沙箱；默认 Local 使用本机权限。状态根锁不等于工具安全隔离。AGENTS.md 是模型指令，不会扩张 PolicyEngine 权限。生产代码的 import、--help、测试与离线评测不需要 API Key 或网络。

完整验证门禁见 AGENTS.md。测试创建隔离目录和独立状态根，Git 测试使用测试仓库；不使用或删除用户级真实状态。

## 生产能力合并与演进边界

`todo_write` 的 `depends_on` 使用已有稳定 id，按完整原子快照检查存在性、重复、自引用、成环及依赖完成状态；旧事件无该字段时投影为空数组，不改写日志。省略依赖保留旧值，显式 `[]` 清除；不能删除仍被引用的项。依赖图是计划约束，尚未成为工具或子 Agent 调度器。

`WorktreeService` 不在 import/构造时写文件。记录与 checkout 位于源 Workspace 外的状态根，校验注册路径、分支、base commit、git-common-dir 和 Workspace 身份；创建前持久化身份，缺口记录保留用于人工检查。Host 控制面 `manage_worktree()` 持有源 Workspace 和项目 Git 锁，写入专用管理 Session 的 `worktree.requested` / `worktree.completed` 与 Run 终态；Git 在线程中执行时，即使调用方取消也保留所有权直到结果已记录。Vue与受控HTTP是用户管理入口，未向模型注册 worktree 工具，不绕过模型的 ToolExecutor。删除期间关闭目标 checkout 的 Workspace/Session admission，成功后移除没有 Session 的 checkout 注册。删除拒绝文件变动、ignored 内容、base 之后的提交、隐藏文件变动的索引标记或关联 Session；保留分支，没有 force/discard 通道，不自动处理旧目录。

`CommandExecutor` 返回 `CommandResult`，携带退出码、stdout、stderr、超时、耗时和精确省略字节计数；`ToolResult.metadata` 向 canonical `tool.completed` 透出这些字段。默认每流保留 25,000 字节头尾，实时消费 pipe，不先完整缓冲再截断。模型能看到退出状态，工具错误来自退出码/超时；注入返回字符串的自定义执行器继续可用。Shell 丢弃的中段不进入日志或分页存储；这与非 Shell 全量结果的请求侧预览不同。取消会等待进程树清理，不伪造完成结果。

能力扩展围绕可验收的软件工程闭环：只读 worker 复用主 Runtime，写入 worker 使用受管 worktree，经独立验收后由用户显式采纳；取消与预算仍归 Host 管理。容器执行通过 `CommandExecutor` 替换边界进入；代码结构检索与真实 embedding 通过现有只读检索工具进入；OTel 作为 Event Log 的可选观察出口，不形成第二套事实源。下文记录这些能力的当前实现。后续若确有跨阶段编排需求，LangGraph 只作为外层 adapter；Redis、Temporal、外置向量库按已有性能或调度证据决定，不作为基础依赖。

覆盖率覆盖全部现存生产源码，mypy 不再通过 legacy 或 tools 通配排除；删除的教学测试由生产依赖校验、真实 worktree 生命周期和结构化命令测试替代，数据库迁移及 HTTP/SSE 兼容测试继续保留。

## 受控只读子 Run

`workers.py` 提供不可变 ReadonlyProfile、Git 当前源码快照、角色协议与严格报告校验，没有第二个 Agent loop。Host 的 delegate_readonly 是经过 ToolExecutor/PolicyEngine 的动态工具，默认关闭。父 Run 持有实际执行 Workspace 所有权，创建绑定该 Workspace 的独立归档 Session，在自己的执行作用域内直接复用同一个 `_execute`，将全部文件根覆盖为独立快照；子 Run 不重新争用父锁，避免同 Workspace 等待死锁。直接源根任务继续串行，最多两个内部只读快照子任务可并发；显式根Plan快照本身可并发，但当前不再嵌套委派。子身份记录在 run.admitted/start 的 parent_run_id、worker_role、snapshot_id；普通接收和 Continue 按持久身份拒绝对子 Session 执行，API 拒绝附件。

子 Run 不读取父消息、不连接 MCP、不使用 custom handlers/Skills/RAG/delegate，目录和执行器仅含四个安全读取工具。深度为 1，每父 Run 最多 4 个子任务，8 步/4,000 Token 软上限/60 秒批次期限；在开启父 Token 上限时按剩余量分配。worker.started 保留源 checkpoint、manifest 和排除数；worker.completed 保留子用量和报告校验状态，父 usage 为 inclusive，子 usage 为 exclusive。未知失败请求消费不估算。父取消或超时先取消并等待子终态，再删除自己创建的快照；重启不自动重派或采纳结果。

派发前先为整批建立并复核可信源码快照，再计算原始角色/精确目标与snapshot_id的SHA-256；目标在脱敏前参与摘要，新增元数据不保存原文。`run.admitted.delegation`在子身份事务内固定fingerprint和parent_call_id；源于该Run的子admission本身即为已接收证据，不依赖稍后可能缺失的worker.started或父工具结果。child message沿原语义脱敏入口保存，已知凭据不复制到新子会话。

`delegation_history`只沿相同Session的显式continuation_of链，`RuntimeStore.worker_admissions`定向查询这些父身份的子接收事实，并校验真实父delegate工具调用引用。不读取其他根任务或其他Session的调查历史，不从旧脱敏目标猜测缺失指纹。整批存在历史重复或批内重复时不创建任何子身份、不增加已委派数量、不发额外模型请求，工具返回Error及已有父Run/call和child身份；原回答须由本Session的read_tool_result或子状态显式查询，未完成/失败不伪装成功。源快照、角色或精确目标改变与普通新Run允许新调查；原4worker/共享2模型槽/预算/只读及取消所有权边界保持。这里仅做字面去重，不判断两个不同表述是否语义重复，也不宣称真实成本收益。

## 可选结构切块与观测出口

显式 code_structure 模式通过懒加载 tree-sitter Python/JS/TS grammar 提取类、函数和嵌套符号，heading携带符号路径；装饰器/UTF-8/CRLF行号按真实源码保留。大符号继续窗口化，class骨架与方法分开避免复制整类。build统计报告解析文件数与fallback原因；窗口/结构模式、parser版本组成chunking_profile/fingerprint，在写事务内复核，变化必须force。缺依赖/不支持语言/解析损坏时回退窗口；默认不导入parser。检索eval可对相同复制语料分别运行两种切法，记录实际提交的chunking指纹和hit heading，不以机制测试声称质量提升。

telemetry.py 的 build_run_trace 是纯日志派生；export_run_trace 只在显式调用时懒加载SDK，使用局部TracerProvider和官方IdGenerator/SpanProcessor，保留历史时刻与确定性哈希identity，不修改全局provider。HTTP/Python开发接口共用collect_trace_events，按精确Run收集子任务及父级/Continue关系端点，最多64个相关Run；不扫描同Session的无关历史。Continue提供link，缺失来源标记unobserved。默认JSON无网络，OTLP endpoint必须显式。固定元数据allowlist阻止正文/参数/结果/密钥进入出口，exclusive模型用量避免父子doublecount；没有响应的重试消费和账单不推测。导出失败不改变执行事实。

## 写入交付与独立验收

DeliveryController 是 Host 的控制面组件，无独立循环、Store 或状态库。prepare/run/verify/apply 使用精确 delivery owner；每个交付使用独立归档管理 Session，每次操作建一个 control Run。delivery.prepared/verified/applied 为唯一事实，进入 delivery owner 后重读最新状态，避免排队请求复用旧通过结论。prepare 和源码比对短暂获取源 Workspace → 项目 Git 锁；writer 模型阶段释放源锁。verify 持 checkout owner，命令前后分别协调源并检查漂移；apply 持 checkout → source → 项目 Git 锁，直到采纳结果写入事实，取消不能提前释放同步副作用所有权。worktree 继续由 WorktreeService 管理，receipt绑定干净源基线、路径/common dir、分支、base、保护清单；build_patch使用临时index与no-filters对象写入，不修改真实index。补丁文件是状态根的不可变派生产物，canonical receipt记录hash/checkpoint；不是第二份运行状态。

Writer通过同一durable admission创建独立checkout Session/Run，execution_config记录backend/image/disable_mcp/writer_delivery_id。Workspace后续Run、Continue和重启从事件派生执行配置，显式Local可由下一次控制面run选择；空image拒绝，不降级。同一 checkout 串行，不同交付 writer 可以并发；容器仅替换CommandExecutor，不改变文件工具边界或Provider执行位置。verify使用ToolExecutor/PolicyEngine显式命令，绑定检查前后相同checkout checkpoint与patch；模型不能改受保护测试/配置伪造验收。apply须最新verified=passed，重读实际patch并再次验证source+checkout；不自动暂存或提交。apply在线程内已生效后被Stop，仍先落delivery.applied，再记interrupted，不假装撤销已发生的Git写入。

ContainerCommandExecutor通过Docker create/start/rm、argv执行和有界输出，使用本机endpoint、预拉镜像、非root、network none、只挂载cwd、资源上限与readonly验收。创建/清理阶段屏蔽重复取消并有独立期限，运行停止后必须确认rm。Docker自动删除与显式rm竞争返回removal in progress时，在同一清理期限内重试；只有删除成功或已不存在才确认结束，不能把“正在删除”当作已完成。清理控制调用启动失败（如OSError）同样归为结果未知，保留Skill暂存资源并停驻，不能降为普通可重试工具错误。ContainerCleanupError携outcome_unknown，ToolExecutor保留此元数据；Host不把它变成tool.completed，而将Run停驻并按canonical tool.outcome_unknown隔离可能仍在写的Workspace。Delivery在源及checkout入口都检查该状态；readonly验收未知不宣称通过，也不误隔离原项目。普通同步处理器在多次取消下继续shield等待真实结束，已知结果提交后才释放owner。

acceptance_tests工程评测使用一次性Git fixture，原测试复制到外部临时目录并保存SHA清单。基线必须退出1；结束后测试源/数据清单不变，清理fixture的派生bytecode，再以-I/-B独立Python、先导入stdlib unittest后加入候选源码，运行原版测试。check_project通过唯一ToolExecutor/Policy留下独立Run证据，不接受模型自判。report增加verification字段；baseline/verification run id、exit/timed_out、manifest与tests_unchanged可追溯。测试、数据与模型预算均有限，失败消费不推测；这些功能测试不构成恶意代码隔离证明。

CI 分开验证默认依赖、可选 parser/OTel、Docker 与浏览器：optional-extensions 安装全部 extras；container job 显式预拉测试镜像后开启真实 readonly boundary、Host Skill 脚本及取消/超时测试，再构建部署镜像、执行一次性卷上的服务分发检查；web job 固定 Playwright 版本并安装 headless Chromium，严格模式缺浏览器必须失败。默认离线门禁仍不依赖 Key 或 Docker；未执行的远程 CI 不能当成本机真实 Docker 验证证据。


## 请求用量账本基础（U1）

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

`frontend/` 是独立开发构建工程，组件由 Router 管理，Pinia 只保存 UI 选择、草稿、临时预览、可重建 HTTP 结果和有限事件缓存，不保存第二份执行事实。API 客户端区分后端 Run 的 `id` 与接收凭据的 `run_id`，统一规范化供组件使用；Run 列表每页按追加顺序返回，最新 Run 取末项并加载完整详情。Session 全历史用量来自专用账本接口，不把历史分页汇总当总账。

`api:generate` 用显式临时 Settings/RuntimeHost 导出 OpenAPI，再生成 TypeScript 请求类型；不会调用默认 Host 或读取用户真实状态。当前后端字典型响应仍由小型领域接口描述，SSE 是独立契约。持久事件按 Run/seq 去重；`model.preview` 没有持久游标，只更新临时区域，最终响应/终态清除预览。UI 观察断开不取消后台任务。

HistoryView 是独立只读观察页。先从当前 Project 的含归档 Session 目录验证归属，再使用每页100条、最旧 Run 身份作为 `before` 的列表接口读取历史；响应页保持追加顺序，展示时最新在前。选择详情单独读取精确 Run 并复核 Session，不依赖它是否位于已加载页，因此 URL 可直接恢复100条之外的旧记录。项目/会话/Run 各有请求代次，切换或卸载后的响应不能覆盖新选择；同一在途详情读取不被轮询重复取代。仅活动详情每3秒刷新，手动刷新列表回到最新一页；不把分页缓存当总量，不将历史选择写入 Pinia 的当前 Session/Run。输出及工具证据只以转义文本显示，JSONL 下载沿用服务端 canonical 导出，evaluation 执行记录链接专属报告页面。

发送前生成 Idempotency-Key；网络、超时及 5xx 等接收结果不明时，按 Session 保存原请求与键，显式重试原内容，不改成第二个任务。已知参数拒绝允许编辑。草稿与待确认提交存当前标签页 sessionStorage，API Key 不进入 Pinia/浏览器持久存储；设置保存完成后清空密码输入。HTTP 请求有 30 秒等待上限，超时不代表后台任务已取消。会话切换与刷新使用代次检查，旧响应不覆盖新选择。待审批时 MessagesProjection 的不完整工具历史 409 只延用上次可见消息，运行详情和审批仍正常更新。

开发服务器仅监听回环地址，HTTP 代理保留 Host 通过 API 同源校验；生产由FastAPI在/workbench/同源托管构建产物。Markdown 子集只用 Vue 文本插值与代码块，不执行 raw HTML，不创建任意协议链接。页面支持跟随系统的深浅主题和窄屏导航；生产资源已随Python包提供；管理能力已由Vue/API或Python开发接口承接，旧终端产品及原生前端已删除。


### Skills 管理 HTTP 与 Vue

`http_api/skills.py` 将本机目录、ZIP和内置包预检/导入、配置替换、版本差异、资源分页及Run加载记录接入同一Host。预检只读；导入可要求expected_version，在重新读取源后先比对摘要，再物化该次预检的固定字节，避免检查后再次读源产生不同内容。配置可带expected_configuration_hash；单服务事件循环在同步比较和追加之间没有await，旧页面冲突返回409。管理查询将损坏配置变成可见诊断，但运行admission仍拒绝损坏目录，不能降级获得执行能力。

`http_control` 先追加mutation_started，完成后追加completed/failed；状态源仍是Event Log。重复键完成时直接返回原结果；只有started时结果不明，保守拒绝重放。磁盘导入和数据库回执不是跨资源原子事务，崩溃可能留下未启用内容或待核查的项目目录；页面通过状态查询帮助核对，不能把未确认当失败后再次执行。

SkillsView 区分用户安装、项目启用、主动选择和Run实际加载。网络不确定的写请求只把SHA-256指纹与幂等键放入标签页存储，不持久化本地路径或ZIP正文；成功后删除待确认键，后续同内容操作可产生新的用户意图。导入预览随来源/作用域变更失效；页面、项目及资源请求用代次隔离，迟到响应不能覆盖新选择。Run加载记录读取canonical session_seq后规范化为seq；SSE订阅skills.snapshot/skill.loaded仅触发事实刷新，不把选择等同加载。


### Vue 静态分发与构建一致性

Vite采用相对资源地址与Hash Router，FastAPI通过StaticFiles挂载/workbench/，HTTP/SSE继续使用同源/api路径。根地址307重定向/workbench/；未找到index时只对工作台返回503，后端正常初始化。资源不存在返回404，不能把JS请求回退成HTML。

package_web.mjs在构建后替换专用eventide/workbench目录，写入每个产物的SHA-256与前端源码/锁文件/构建配置指纹。资源作为Python package-data提交和打包，源码安装与wheel使用者无需Node；维护者修改前端后必须重建。离线pytest校验源码指纹及全部资源，CI重建并比较版本库差异，再直接运行FastAPI静态页面浏览器旅程。

wheel验证在临时目录解包并以隔离Python进程导入，断言Eventide来源为安装包，再通过TestClient获取页面、清单、所有资源及healthz。Docker多阶段构建只在web-build阶段使用Node，Python镜像复制新产物；运行用户UID1000默认进入预建且归其所有的/workspaces/demo，命名卷首次初始化继承该权限，避免注册/app为用户源码。

2026-10-16在Apple Silicon Docker Desktop完成镜像构建与实际Compose启动。tests/container_smoke.py使用唯一命名的临时容器/状态卷/源码卷，验证非root、无需Node、全部静态资源hash、默认来源拒绝/显式peer及同源放行、持久会话/假配置/项目文件在重建后恢复；仅测试环境读取访问日志获知peer，生产不会自动授予信任。另以真实容器覆盖Host调用Python/Shell Skill、权限审批、字面参数、资源读取、停止和超时。容器Skill暂存树仅对冻结副本设置目录0755/文件0644以适配Linux UID边界，本地暂存保留0700且不修改用户文件权限；umask077有离线专项验证。全部验证均无付费模型请求。


## 预算事务与价格版本底座（U2）

RuntimeStore.transact_facts在BEGIN IMMEDIATE内读取指定事实、调用同步纯决策函数并追加事实，同一事务失败整体回滚；SQLite协调两个连接的竞争请求，Python锁只保护单连接。全局事实采用rowid追加顺序，不用可回拨的wall clock排列配置。新增控制面事实仍在runtime_events中，没有平行预算表、可变余额文件或分布式锁。

BudgetLedger从budget.configured/reserved/settled/resolved派生状态，预留按attempt_id幂等；同身份不同内容拒绝。预算覆盖实际根Run及子Run、交付、项目和预留时的UTC日期，跨午夜不转移已派发消费。根身份由run.admitted/started父关系验证，同Run家族不能切换项目/交付归属；ModelBudgetController从P1冻结的项目/Workspace事实和实际父关系推导归属，校验delivery.prepared所指源项目，不能接受模型或客户端自选归属。Continue可有新Run额度，交付/项目/当日桶保持累计。

生成调用只有reported且同时具有input_tokens/output_tokens才释放预留并结算实际量。embedding底层预留可固定usage_basis=input_only，必须与kind=embedding的attempt_started同事务、output_estimate为0；此0是无输出额度而非已报告输出Token。结算从预留读取口径，不能由返回结果改变，只有已报告input_tokens才能释放，且拒绝输出/推理计数。旧预留和历史调用默认input_output，不补造精度。未知/estimated/partial继续保留预留；部分reported高于估算时保留已观察下界。完整用量后迟到的未知回执不降级状态；用户确认未知消费必须有独立身份、实际量和理由，配置/确认事实author=user。超限响应真实记录，后续请求拒绝，不宣称账单硬上限。

ModelBudgetController.begin_embedding按输入数组序列化UTF-8字节数预留（embedding_serialized_utf8_bytes_v1），不创建虚假的生成请求或input_composition，正文不入事实。费用快照据同一固定口径只要求输入价格；汇总保留output_tokens缺省。198项相关离线回归覆盖重放、重启、父子共享桶、未知和估算不释放及生成旧口径；此为C阶段远程embedding接线的底座，尚不表示生产派发已受控，本地推理也不进入供应商账本。

embedding_runtime.embed_remote提供Host所属的异步远程请求底座，先取得全局model_slot再预留，SDK关闭隐式重试；最多64项/1MiB是输入字节边界，不冒充供应商Token限制。返回prompt_tokens映射输入，total_tokens只检查一致性，缺失或冲突不推算。向量无效但已返回usage仍结算；重复取消等待真实请求及客户端清理，迟到的已报告usage也保留。54项离线派发/账本回归通过；尚未替代生产检索中的旧同步适配器，后续接线后才验收U1/U2整体。协议依据：[官方embeddings响应](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。

CostLedger从同一预留/结算事实生成cost.estimated；price.registered按Provider/model/category/currency/effective_at固定版本，价格使用严格Decimal字符串。费用行从总量划分有价格的cache/reasoning子集；不一致的细项整组标未知，不能将超出总量的缓存作为已知费用。缺价格不显示免费，不同币种分别汇总；estimated用量费用独立于reported用量费用。首次费用快照固定当时采用的价格版本；重新估价必须显式引用旧快照并生成新身份，历史保留，汇总按每次尝试最新一份去重。

ModelBudgetController已接入RuntimeHost._complete_with_retry：每次真实尝试在同一SQLite事务中提交budget.reserved与model.attempt_started，完成时同一事务提交budget.settled与model.attempt_finished，再记录版本化费用快照。没有预留不能派发；BudgetExceeded不被ContextBuilder当作可忽略的摘要失败吞掉。供应商结果回到Host后同步结算，再执行模型工具；结束前不经异步通知，取消不会把已返回的供应商用量变成未知。通知失败不撤销已提交事实。

估算按实际ModelRequest的system/messages/tools序列化UTF-8字节数，输出用max_tokens；方法名固定为serialized_utf8_bytes_v1，保守占位而非tokenizer计数。多模态水合后的数据会影响估算，正文/base64不写预算事实。主调用与worker可流式，摘要用complete；每次重试重新竞争额度，之前失败的未知额度继续保留。重启仅关闭Run，不释放未结算预留；原子提交避免用量成功事实与预算结算不一致，费用快照写入前崩溃则显示unpriced。

开发控制接口由Host提供configure_budget/budget_status/register_price/cost_status，控制Session不可当普通对话运行。http_api/budgets模块复用这些接口与原账本，budget_views只派生查询，不创建第二状态源。预算/价格/未知处理写入复用持久HTTP mutation协议，固定首个预算控制Session作为幂等所有者，服务启动目录变化也不丢失旧重试身份；尚未结束的mutation返回409而不是重做。预算配置hash只随配置事实改变，消费结算不会引起配置冲突。

失败回放也保持确定性：预算路由仅对前置拒绝或原子写入回滚显式记录mutation outcome=rejected，并在首次与重放HTTP错误携带同名响应头。客户端据此重新取得提交键；未显式标注的旧失败、进行中/结果未知和键内容冲突都不能推断无副作用。该字段是可选审计元数据，不改写或重放旧操作。

费用筛选先以budget.reserved的精确Run、Project、Delivery、预留UTC日期选择尝试，再汇总该尝试的不可变费用快照；Run/Session的子任务通过真实parent_run_id关系递归选择，不累加父终态。usage先从全部调用事实完成新旧事件去重，再按同一尝试集合或历史调用开始时间筛选，跨午夜不会把legacy response再计一次。Session不受导航列表页数限制；Project包含其全部不可变执行副本。历史调用没有预算事实时保留在usage及historical_unreserved_attempt_ids中，费用计未知；旧项目归属优先使用已有Run事实，否则沿用迁移后的不可变Session/Workspace关系，不补造价格。

未知处理需model.attempt_finished或Run终态证据，不能在途结算。record_known经既有budget.resolved记录用户总量和理由，且不得低于已报告部分；keep_unknown追加budget.unknown_acknowledged审计，保留预留及未知状态，不进入消费投影。二者都不修改模型用量事实或旧cost.estimated，避免人工总量伪装Provider输入/输出精度。价格注册不会隐式重估；查询各币种独立、estimated来源单列、缺失显示未知。Vue预算/费用、交付用量导航、持久调度和输入组成已接入，连接探测按下述控制Run接线；API embedding已接Host检索与索引构建。

## Vue预算与快照控制面

UsageView按精确Run/Session/Project/UTC日请求完整历史费用与用量，使用P1 project_id而非Workspace猜测共享范围。BudgetPanel复用服务端配置hash、保留脏草稿，旧scope与旧读取响应不能覆盖当前状态；同目标轮询串行避免慢请求饥饿。价格请求由内容摘要固定版本身份，网络失败继续使用原mutation键；只有明确拒绝才能换键。价格与Unknown处理输入由OpenAPI schema生成约束，费用和调用响应保持不同来源/币种的展示。

Vue页面显式选择source、Plan snapshot或worktree；Session与Run执行归属来自服务端。source活跃时阻止新源任务和副本准备，已存在worktree与已接收snapshot不占源UI锁。绑定worktree后表单不能切回source，Continue由服务端继承原副本。执行位置监听使用独立原始值源，普通轮询替换Session对象不重置用户选择；创建新Session期间禁选，完成切换后回到Auto。页面不能把准备期的短锁误报成持久队列，P3调度继续独立验收。

## 持久接收与有界调度（P3）

`scheduler.py`从RuntimeStore按提交顺序提供的run.admitted、run.started及终态事实重建队列，不按时钟排序、不增加运行状态表或另一套调度事件。RuntimeStateProjection将已接收未开始的Run投影为queued；started才进入running，终态不回退。parent_run_id子任务和没有admission的Delivery/verification控制Run不另占根额度；Continue是新根，实际Delivery Writer仍是根。

`RunDispatch`是RuntimeHost的进程内所有权helper。默认4根、20等待、2模型请求，短期容量ticket在冻结输入前竞争名额，不能替代持久接收；只有Store.admit_run同事务写入精确Run/Session、消息及checkpoint后才允许202。满额拒绝不写孤立Run或消息。准备中的ticket占相应容量，counts.preparing_admissions单列，未接收输入不进入active/queued列表。实际根lease覆盖准备、执行及真实清理，出现终态不自动提前释放。

Python run/continue、HTTP和Delivery writer共用admission与start_admitted_run；Python仍await最终RunResult。start_admitted_run登记同一个等待/执行wrapper Task，它获得调度通知后直接await原_admit，不再创建第二个执行Task。HTTP不维护tasks/reserved执行所有者。queued保留_admission_done的Session所有权，但释放_admissions中源码/副本执行claim。同步claim选择最早可执行任务，检查真实execution key和Session锁；根完成及控制面释放锁均唤醒，不因前一项资源忙阻塞所有后续项。任务失败先补终态，最终Task done回调在实际副作用收尾后释放root并推进队列。

队列仅接收已经可靠冻结的请求：源正在直接执行时，Python/HTTP均拒绝新源运行或新副本准备，不在无身份时等待半成品源。等待的source必须有可信Git checkpoint，否则接收前拒绝；空闲非Git直接运行保留原限制。source/worktree实际开始时在执行锁内复核接收checkpoint，snapshot复核固定manifest；Continue不绕过复核。等待时源码变更导致明确失败，不静默换基线。所有未终态工作（包括queued、已接收但尚未start的Run和短期ticket）阻止Python/HTTP修改Provider配置。

模型信号量在每次实际attempt及U2原子预留之前获得；等待名额被取消不产生尝试或消费记录。主调用、摘要、worker共用2槽，连接探测也遵守请求上限，并通过真实control Run进入同一attempt/预算账本。Provider返回/异常结算后释放模型槽，重试backoff、工具/审批和worker等待均在槽外。worker继承父预算，不另抢根槽，四个根等待子任务时仍可推进。

## 连接检查控制 Run

`provider_probe.py`由RuntimeHost委派执行，稳定归档control Session的kind为connection_probe，每次检查建立真实Run和run.started（source=connection_probe、billing_scope=service）。没有run.admitted、用户消息、工具或Agent循环，因此不进入根Agent队列；同控制Session串行，网络等待不锁源码。Host持有活动任务与精确Run取消身份，等待Provider关闭后才落终态；重启直接停驻可信probe，不读取源码或补checkpoint。

BudgetContext.project_id仅对此严格控制身份允许None：ModelBudgetController与BudgetLedger事务内都检查同Session唯一控制kind、唯一probe started、无admission/父Run/Continue/Delivery。普通Agent、worker、交付仍要求既有项目归属。probe作用域为真实Run和UTC日，费用筛选使用reservation的None项目，不能从控制Session存储目录反推业务项目。

候选配置与超时在检查前冻结；获得共享model_slot后才原子预留及写attempt_started，以候选Provider/model计价。请求只执行一次并由model_timeout约束，HTTP在30秒触发取消，但仍等待请求及客户端收尾后才返回，不承诺严格30秒总墙钟上限；收尾未结束时Run保持活跃。usage经既有ModelBudgetController结算，失败/取消/缺失usage未知不释放；候选密钥参与响应/元数据脱敏，异常只暴露稳定分类和可读说明，不保留原SDK异常链。

HTTP接收复用control Session上的http.mutation事实，Idempotency-Key必填；候选请求只存摘要（包含显式密钥的指纹，不存原值），实际已保存密钥在首次调用前冻结。Run身份由控制Session/操作/键派生；相同请求回放持久结果，内容冲突及在途/未知返回409。崩溃发生在Run完成与HTTP回执之间时，只从原终态恢复回执，耗时/检查时间无法还原则null；已接收但无Run的中断证明未派发，补写明确失败回执。进程内active集合仅阻止在途接收被误判为崩溃，不是新的运行事实源。Vue的sessionStorage仅存随机键和表单摘要，不存密钥；结果不明不换键，刷新后可重试原内容。

Vue SettingsView将候选草稿与store.provider分离，后者仍只由配置读取/保存/恢复结果更新；连接测试仅保留独立的ProbeResult。加载、保存及探测期间字段和冲突操作禁用，按钮没有自动触发路径。api/probe.ts复用durableMutation管理请求键，并保存单个候选摘要阻止未知结果被新表单替换；凭据留在组件内存，刷新后重新输入。同标签页的轻量内存观察器只共享在途请求状态及已返回结果，切页后仍锁定表单并接收原结果，避免旧请求清键后新页面把“重试”误变成新调用；它不保存凭据，也不替代持久Run事实。页面展示真实Run/usage、服务UTC日预算范围，未知Token与null耗时不转换为零。Playwright跨文件使用单worker，因为测试共用真实Host及全局队列；单场景内部继续验证并发窗口。

GET /api/queue只读查询上述事实与所有权；project_id只过滤列表，容量和counts始终是全局。dispatch_state区分queued/starting/running/finishing；Run status继续使用canonical投影，审批等待或终态清理不会伪装为新执行。排队取消复用精确Run取消并保留输入，关闭等待准备线程/执行清理后停驻全部已接收工作。重启不自动派发；尚未started的admission保留原checkpoint，不调用crash-gap补新基线，实际开始过的旧运行继续保守恢复。所有未知Token预留保持原状。前端生成类型、队列页面和整包门禁由集成阶段验证。

Vue队列控制面只读取Host队列快照，不维护浏览器调度器；全局counts/limits与带精确Run/Session/副本身份的列表分开。queued被视为同Session活跃工作，但不作为源Workspace执行锁；UI取消只发送该Run身份。每次单一在途轮询、卸载后停止回填，后台任务继续由Host拥有；狭窄屏幕横向滚动任务表。

Usage只读投影保留attempt既有provider/duration_ms/stop_reason，legacy仅透传真实存在字段。重放/墙钟回拨不重算延迟，未结束和缺失值不填零；页面读取原值并对缺失作独立展示，不将元数据补齐算作用量完整性提升。90项usage/预算回归与真实API页面验证通过。


P3源码证据补充：Host为接收、派发、只读工具基线、worker与恢复使用同一checkpoint策略生成证据。默认外置状态仍保留原指纹；若实际Store或状态产物位于源码内，`checkpoint_policy.version=2`固定精确相对路径及Runtime namespace。仅未跟踪的实际数据库/WAL/SHM/journal、实际租约、密钥，以及配置状态根下attachments/skill-packages/execution-snapshots被排除；不按扩展名或整个状态根忽略。tracked diff和恢复mtime检查始终包含同名已跟踪文件，符号链接替换仍是漂移。State等于Workspace根在接收前拒绝，避免混淆源码与保留namespace；普通项目Skills始终保留。源快照与附件标记不会因Runtime自己的持久化触发漂移。

恢复不能把旧checkpoint静默升成新策略：旧口径与当前策略不同，crash-gap只记录不可恢复证据，不补写新基线；Continue保持拒绝，用户检查后显式放弃并新建。已经固定manifest的快照继续按其原身份校验。S3在源项目更新后复用旧Skill的验收使用真正固定snapshot；source任务的项目Skill漂移在模型派发前拒绝。

P3/P4/P5集成验收已完成：完整1180项通过、覆盖93.90%，默认7项Docker跳过另由76项真实容器及生命周期回归补验；6条真实API页面旅程覆盖快照/写副本并发、排队、取消和自动补位。Ruff/Mypy87、前端类型/构建/格式、48项前端回归及smoke12/12通过。评测和交付后续控制面仍独立按B/D验收。

### 冻结任务与独立判分（B1）

`eval_suite.py` 用同一纯解析接口校验文件及包资源的严格版本化清单：任务内容哈希、相对路径、无链接引用、类别／开发留出配额和家族隔离。唯一正式manifest和20个JSON任务包已移入`eventide/bundled_evals/engineering-v1`，原`evals/benchmark.yaml`只保留严格内置别名，不复制任务正文；移动保留原字节和清单指纹。清单固定理解4、定位4、单文件6、跨文件4、恢复交付2；其中开发12、留出8。历史 engineering 三题保留并全部进入开发集。任务或策略实验发生变化时须提升任务／清单版本；参考留出结果调参后须移动并补充题目，不继续复用旧留出标签。

`bundled_evaluations`通过`importlib.resources`读取白名单ID，支持常规安装与ZIP/wheel，不把用户ID当路径解析，也不依赖工作目录。逐资源有3MiB读取上限；文件系统路径及祖先、ZIP的UNIX链接项与祖先均拒绝。整个题集验证后才选split，某个留出题损坏时不能悄悄只运行开发集。返回独立纯JSON任务快照和三个指纹：`manifest_sha256`绑定原manifest字节，`suite_sha256`保留原规范manifest口径，`inputs_sha256`绑定所选cases的UTF-8 JSON（ensure_ascii=False、sort_keys=True，逗号和冒号分隔均无额外空白）。后续Web接收须将该快照持久固定，不在队列派发时重新读取当前包或别名；本模块没有第二状态源，也不读取凭据或启动模型。

`evaluation.run_evaluations` 仍是唯一 Python 入口，旧 YAML suites 继续可用。冻结集先在读取环境或已保存凭据前禁止 live；离线数据注明 `scripted_mechanism_validation`，逐例带清单和任务版本哈希。每例源 Git 项目、状态根、冻结验收目录彼此独立，状态与验收目录作为源目录的兄弟目录，避免 worktree checkpoint 被评测自身数据库污染。模型步骤最多12、已观测 Token 软限最多20,000；整例5分钟上限及部分失败记录由下文B2实现，真实批次成本预算仍由B3前置评估。

`eval_grading.py` 仅编排已有 RuntimeHost/ToolExecutor/PolicyEngine、check_project 和 manage_delivery；没有替代执行器或结果数据库。事实题在持久 Plan 快照中运行，要求结构化事实和精确行号与人工标注一致；修复题在 Session 固定 worktree 中运行。验收先确认原版测试失败，再在独立 Python 进程运行事先冻结的 unittest；源码允许范围、固定测试、额外文件和原项目未被修改均参与判断，正文自报成功没有分数。

恢复交付题明确区分 scripted Provider 经工具修改业务文件与评测程序控制动作：一题关闭重开 Host 后 verify/apply 并复验源，另一题在 verify 后注入独立源变化并要求拒绝 apply。测试及候选产物持久保留并引用 Run 身份，不注入“已通过”事实。该机制验证不证明真实模型自主修复、不包含未来 C 检索或 D 三候选流程；本地执行仍不是恶意代码安全沙箱。Web 评测未来必须纳入主服务模型额度和项目／日预算，不能复制账本制造第二事实源。

### 评测证据与部分失败（B2）

报告 schema v2 由 `evaluation.py` 编排、`eval_evidence.py` 从每例现有 Store 只读重建。批次先冻结标准化任务输入与 Runtime commit/dirty/code SHA-256，逐例保存有效配置、Run/Session/Turn、context.configured 工具／指令／Skill 指纹、skills.snapshot 版本、模型尝试配置及全部 canonical 事件快照的哈希。源、固定验收原文和唯一事件数据库保留；JSON 报告只是可重建的审计导出，不参与运行恢复或预算决策。

用量直接调用 `build_usage_ledger`，收集用例全部 Session 的独占尝试；父终态汇总不重复累计，重试失败及取消无usage保持unknown。报告总量分别累计reported/estimated；完整性要求全部已尝试题有可用账本，缺失证据题显式列出；历史简表没有对应字段就缺省，live全未知总量为null，不填0。费用直接读取同一 Store 的 CostLedger，保存历史估算和精确价格版本。价格必须由实验调用方显式传入，不访问用户预算/费用库；费用按币种及用量来源分桶，脚本消费标注 `billing_applicable=false`。

每例准备之后，剩余整例期限交给独立 asyncio Task；到期取消并等待清理，不把单请求超时等同整例上限。所有套件默认12步/20,000已观测Token/300秒，允许更严格配置；数据库、Git同步准备和实际副作用清理不能被描述成实时强制终止。保存初始running报告、每例结果和最终状态，写入采用同目录临时文件、fsync、replace。发生取消用独立清理Task和shield等待收尾，重复取消也不截断证据导出和部分报告，然后向调用者重新抛出CancelledError；剩余题不运行、不伪造用量。离线失败保留并继续，live失败/超时/未知终态停止扩大批次。报告total保留原定题数，attempted/remaining与终态分开；证据导出失败本身也是失败。

冻结集live已通过受控Web规划确认及共享预算桥接开放；独立Python开发实验与Web机制、真实效果分别报告。B3重复实验、Web调度、价格成本对照及U5质量结论另行验收；本切片只保证证据与错误不被丢弃。

## 最终请求输入组成（U3）

`input_composition.measure_input_composition`统计实际交给统一模型入口的ModelRequest.system/messages/tools。ModelBudgetController.begin在同一原子预算预留和model.attempt_started事实中保存统计；主Agent、context_summary、worker与其他复用begin的调用共用该路径。只保存方法、单位和数字，不复制提示、资源、图片或工具原文。既有serialized_utf8_bytes_v1预算预留和Provider Token总量不变。

口径serialized_unicode_characters_v1使用Python json.dumps(ensure_ascii=False)的Unicode字符长度。instructions是system序列化字符串；tool_catalog是tools整个序列化数组；每个message.content的字符串或独立块分别归类。唯一且先出现的assistant tool_use与user tool_result按tool_use_id配对：load_skill/read_skill_resource结果整块归skills，search_knowledge归retrieval，其余或孤立结果归tool_results。重复ID、缺失、错序或非assistant调用不建立来源。普通文本、工具调用参数、图片/provider_state/未知块和压缩摘要归history；message中role/content以外的扩展字段值也归history，不能把顶层加密reasoning等长值当结构。其余角色、字段名及数组/根对象结构归envelope。七项和严格等于整个请求JSON长度，统计含转义与块包装，不能称纯正文Token或Skill总成本；system内Skill目录仍计instructions。

usage投影只验证attempt_started中的固定字段、方法、单位、非负整数与精确合计；finish不能补写或覆盖统计。旧事件、缺失、未知版本或非法结构均为null，不从消息重算；重放按原attempt去重，结束前和取消后同样可查，不增加已报告Token或费用完整性。Run/Session usage及costs内嵌usage沿用该投影。

Vue的InputComposition只展示服务端已校验的固定字符统计，旧值为null时不补造。UsageInsights由costs返回的全历史独占calls按精确run_id分组：排名只加reported输入/输出，缓存与推理细项不重复相加；仅estimated/unknown的运行不以零参与排名，部分报告保留不完整次数。父子Run各自列出，不再次求父终态和。context_summary按真实attempt数计数，重复失败仅取status=failed、取消不算失败。组件刷新替换投影，无本地累计状态。48项前端回归与6条真实API旅程验证刷新、零值、缺失和分类；桌面/移动端输入详情在跨列区域展示。

### Web 评测控制Run（B4，离线切片）

`EvaluationController`依赖唯一主RuntimeHost/RuntimeStore。每个Project固定一个归档control Session（kind=evaluation），逻辑绑定源Workspace。`Store.admit_run`同事务写入run.admitted和evaluation.accepted：包含全部已校验任务正文、manifest原字节与规范化哈希、选定cases哈希、split、固定限制和接收时Runtime版本。排队后从该事实恢复输入，不能重读用户路径或重新选择当前版本；报告分别记录接收及实际执行的Runtime证据。

接收通过现有RuntimeScheduler ticket竞争4根/20等待；RunDispatch.start执行评测控制器回调，不进入Agent循环、不重新构造提示词。执行键为evaluation_run:<id>，只持有自己的Session，不占用户源码Workspace锁。终态后的实际清理仍由_track_run_task唯一回收根lease。没有独立后台队列、任务表或状态文件；状态查询从主Store事实重建，SSE沿用Run流。

受控runner直接await已有run_evaluations：显式常量Settings、ScriptedProvider、Hash embedding、禁MCP，不读取环境或已保存凭据，不调用真实SDK。每题仍在独立Git项目和Store中测试Runtime，包括恢复题关闭重开它自己的Host；这些数据库是测试产物，不能关闭主服务或把case调用/usage再次导入主账本。主控制Run没有模型尝试，不伪造费用为免费，billing_applicable=false明确表示不适用。

同步progress回调在案例开始/结束及原子报告保存后追加主事实。报告快照各自不可变，主Store只保存受控文件名及SHA256。GET报告限制命名、10MiB、目录身份、无链接和hash，不从报告文件反推主任务终态。取消沿用B2实际收尾和重复取消保护，保存部分证据后再向外传播；排队取消不创建fixture。Host重启严格识别evaluation control身份并直接写interrupted，不检查用户源码crash gap、不重放，普通Run/Continue/abandon入口拒绝control Session。

状态根的evaluations专用namespace加入现有精确untracked排除列表；不排除整个state，已跟踪内容和symlink仍参与源码漂移。checkpoint_policy中目录列表随之变化，旧checkpoint不会被静默升级。服务安装包使用内置资源，不依赖当前工作目录或顶层evals。

离线分支保持上述隔离。live分支由下文EvaluationCallBridge接入主Host模型槽与唯一预算账本；不复制消费事实。

### Vue 离线评测控制面（B4）

`EvaluationsView`消费项目级评测目录、接收、历史、详情、取消和报告API；任务集仅固定为`engineering-v1`，允许development/holdout/all，mode默认offline，live须读取规划并显式确认。页面不生成prompt或模型配置，不把控制Run的完成状态当作每题通过。脚本机制验证、实际模型效果、真实费用口径始终分开。

`api/evaluations.ts`复用durableMutation管理接收和取消身份；不确定接收在sessionStorage中保留原项目、suite指纹和split，只有明确拒绝或成功接收才解除。模块级在途状态和接收结果跨SPA页面继续持有，防止旧页面完成后新页面再次发起；浏览器刷新依靠持久请求身份及服务端事实恢复。当前页按项目epoch丢弃旧作用域响应，按详情epoch丢弃旧任务／取消前读取；相同读取在途时轮询跳过，不反复作废慢响应。

地址查询参数evaluation只选中后端已有身份；详情必须与当前项目相符，不从URL改变归属。历史逐页读取，选中的旧记录可独立查询。报告按用户请求读取并显示为快照，后端报告缺失或指纹错误显式显示，不以浏览器缓存替代事件状态。live页面先读取模型/费用规划，确认后提交quote_hash；切换项目/划分/模式会清除确认，未知接收保留原正文，重试不重新估算或重复派发。

评测请求、目录、接收、逐题和详情类型直接引用生成的OpenAPI schemas，避免手写响应定义漂移。队列和可见历史会话中的evaluation执行绑定进入评测页面，不创建普通Agent输入或Continue操作，也不把评测当作源目录占用。真实Docker安装产物已运行12题开发集、重试接收和重启报告校验；独立case产物不影响所选源码目录，wheel验证涵盖全部21个内置任务资源。

### 配置事务与恢复边界

配置控制Session承载Provider变更、模型/会话生成参数和HTTP回执，属于唯一事件日志；不增加第二配置队列。Host在单进程内比较有效配置摘要，Provider变更期间拒绝新运行并等待旧客户端关闭，随后Store在一个事务内写配置及成功回执。事务调用返回失败时从数据库回执重新核实内存配置；无法核实则停驻，禁止沿用不确定连接。

生成参数更改不改变已接收Run的冻结参数。会话配置事实含会话创建身份，避免删除并重建相同Session ID后复用旧覆盖。浏览器保留未知操作原身份，确定拒绝才允许新身份；确认成功后重新读取服务当前状态，避免历史幂等响应造成页面回退。待确认请求记录不含原文密钥。

### 固定本地模型与有界重排

`rag/local_models.py`负责本地包清单、准备、内容校验和惰性依赖；`rag/search.py`支持已取得向量的融合接口，避免未来异步受预算管理的调用链重复请求embedding。模型profile包含包内容指纹和算法版本，索引维度在构建时固定。Sentence Transformers依赖版本固定，模型仅使用local_files_only和禁用远程代码的加载路径。

Host在既有ToolExecutor/Policy边界内绑定重排器，CPU推理通过现有同步工具线程执行，取消仍等待实际工具收尾。重排候选不超过20，输出不超过5，失败输出诊断并保留融合排序；本地模型包在运行准备时缺失，验证索引后提供词面工具并记`retrieval.degraded`事件，不静默消失。无重排的原k=1–10契约保留。

每次模型加载/推理前后校验本地文件内容，额外I/O成本需纳入C4/C5延迟测量。该层不产生远程Provider用量，不把本地字符或CPU耗时冒充供应商Token；远程embedding的主链接入和索引Web控制面仍按后续阶段完成。默认CI不安装retrieval重依赖、不下载模型；原可选扩展门禁明确安装code/telemetry，真实本地模型测试需显式提供两个固定目录环境变量。

### Run拥有的异步远程检索

retrieval_runtime在工具权限检查后验证索引、调用embedding_runtime、再复验固定profile并融合结果；没有第二次同步embedding请求。索引和重排CPU线程通过owned_local_work保留所有权至实际退出，重复取消不会提前释放Workspace。预算拒绝经ToolResult的budget_exceeded元数据持久记录后交由Host停驻。ensure_token_budget读取独占attempt账本与子Run已报告总量，避免旧响应计数漏掉辅助请求。未知消费由持久预留控制，不伪造已观测Token。

### 根任务查询改写边界

可选 `retrieval_rewrite` 包装既有 `retrieval_runtime`，仍位于ToolExecutor/Policy之后。根身份由ModelBudgetController沿可信parent链解析；同步检查及追加 `retrieval.rewrite_claimed` 在单进程事件循环内不让出执行权，子任务不能各获一次额度。认领事实持久保存，即使改写失败或服务重启也不重置。`retrieval.supplement_requested` 标记唯一补检索，完成状态记录在 `retrieval.rewrite_finished`；日志仅记录查询摘要，不复制原文。

原查询优先完成，改写使用冻结模型、统一请求账本及一次实际请求上限，不继承主任务采样覆盖。校验单行长度、工具调用和截断状态；索引generation在原检索前、改写前和补检索前后复验，代次变化则返回原结果及诊断。合并按片段路径/行号/正文去重，再对两条查询做RRF，保留原查询候选池，清除不可横向比较的CrossEncoder查询分数。只允许一次补检索，不形成递归查询Agent。默认关闭，效果晋升另由冻结对照决定。

### 冻结检索实验入口

`rag.benchmark`从打包的retrieval-v1 JSON读取严格数据模型，检查路径、标注片段、唯一查询和每划分至少20题，内置资源再校验固定SHA256。实验只在临时Git项目建立索引；一次实验共用同一语料/索引/模型，逐次记录失败而非删除失败项。报告分别保留Recall@5、MRR、p50/p95、诊断及返回字符数；默认开发集，留出须显式选择。

本地实验报告不创建生产模型调用事实、不声称零费用等于整体成本免费；同步入口拒绝远程profile。额外改写方法必须自行复用Run预算与账本。`promotion_decision`要求质量一升另一不降、p95不超过两倍，并且显式下游成功数不下降；缺少下游证据永不晋升。当前四路真实本地对照未满足条件，C4/C5仍有改写和下游实验未完成。

索引CPU线程通过 `index_runtime` 将远程embedding交回拥有Run的事件循环，共用既有模型槽、原子预算与attempt账本；不创建辅助Host或第二Store。IndexStore的128片段内部批次再拆为最多64，线程等待实际请求完成。取消标记同时禁止后续批次和索引发布；主任务等待在途请求清理及线程退出，重复取消不提前释放所有权。已完成批次的消费保留，后续失败不发布新代次。发布事务已经结束后到达的取消不能回滚既成事实，控制面应读取实际代次而非猜测文件状态。

### 索引控制Run

`IndexController`为每个项目建立归档的受信任control Session，拒绝普通Agent消息/Continue。HTTP以同源幂等键持久接收 `run.admitted` 与 `index.accepted`，随后由原Root调度器执行；同项目索引串行，索引控制身份独立于源码会话，不长期占住源码执行锁。Run冻结配置指纹与构建选项，凭据仅在内存的Settings中传递。恢复仅停驻，不根据旧事实重发外部请求。

执行使用 `index_runtime.build_index`，全部远程批次归属该Run/项目/UTC日预算。发布统计与generation从同一SQLite提交事务返回，避免提交后另读索引误绑定其他构建的代次。`index.published`仅存摘要，运行详情/分页历史从主Event Log重建；取消及失败保留既有attempt、未知预留和旧索引。队列、历史页跳转专门索引页，用量页接受明确Run身份，控制Session不进入普通聊天执行。

### 有界交付与跨轮限额

`DeliveryController`继续拥有准备/单次Writer/独立验收/采纳，`delivery_repair`仅编排有界轮次，复用同一独立worktree和执行边界。`delivery.repair_started`固定目标、checks、image、最大轮次和绝对deadline；`round_started/round_finished`绑定每轮Writer及补丁，`repair_finished`记录停止原因和派生用量。每次验收拥有唯一tool call ID，避免多轮在工具投影中覆盖旧证据。没有完成事实但控制Run已中断时，读取显示interrupted，禁止自动重新启动和采纳。

`ModelBudgetController`在主请求和embedding预留前检查 `delivery_limits`。以可信预算context.delivery_id筛选独占attempt，跨Writer根Run及其子任务累计；无await的检查/预留保持单进程串行原子边界。36次上限包含失败尝试和辅助模型请求，embedding只进Token总量。控制Run终止或交付结束后，不允许通过Writer Continue再派发。独立验收不占模型槽；Writer共用原根队列，交付管理Run不重复占根名额。

每轮先复核源码/保护范围与patch，和上一轮完全相同即停止；失败签名对测试耗时文本做归一化，重复失败停止。最多3次候选验收，成功才开放显式采纳。超时/取消只发一次子任务取消并等待收尾；真实未知工具结果停驻副本。此流程没有恢复自动重放，失败产物保留用于检查；Web接收与所有权见下节。

### Web 交付所有权

`WebDeliveryController`将prepare/repair/apply作为同一个Root调度器的持久控制Run接收；归档control Session不能作为普通聊天使用。prepare以项目Session串行，后续操作按delivery Session串行，执行锁与源checkout锁分开。事件`delivery.operation_accepted`冻结操作正文；历史从canonical事实按提交顺序重建，跨Session的delivery事实不能按各自session_seq混排。

Web repair持有一个根名额，Writer通过受信控制Run的parent_run_id借用此名额，仍独立拥有Session及checkout并经过Host原主链。派发前检查父控制Run仍持有根lease、operation=repair、delivery身份与checkout一致；没有第二调度器。四个修复控制Run不会各占根名额后再等待四个Writer根名额。模型槽仍全局最多两个。预算根为控制Run，主Writer/摘要/子任务消费按同一交付累计；管理终态投影不重复记账。取消等待Writer实际收尾再释放根。

HTTP显式采纳冻结用户已查看的patch_sha256；队列执行前再次检查当前验收摘要，既有apply仍复验双方源码。产物路径比较使用规范化state根，兼容macOS的/var→/private/var别名，state根内部符号链接仍拒绝。补丁读取最多10MiB且重验hash。Vue请求未知时保留同一请求内容和身份；项目切换的旧响应丢弃，刷新通过URL/后台历史恢复，不重放写入。

交付控制Run的execution_config仅用于归属其子请求，不是源Workspace的执行配置；workspace_execution_config排除source=delivery控制事实，防止后续普通对话继承交付预算身份。快照admission保留内部生成的execution_binding路径/hash清单，避免将test_tokens.py等文件名误作凭据键破坏manifest；提示、工具输入等用户内容仍沿用原脱敏。

### 会话元数据事务

Session 管理写入通过 session_management 的持久请求接收及 Store 原子事务。
版本来自身份、创建时间、Workspace/目录、名称和归档状态；运行进度不使编辑版本失效。
成功管理事实保存在 configuration_control 所有者下，避免给空会话添入历史后
使其永远不能删除。控制会话用 SQL EXISTS 检查 control.session 或历史交付/
验收来源，避免每次 Session 查询读取全部事件。Vue使用统一HTTP
版本与重试契约；页面只管理元数据，不取得执行或取消权限。

### Web 工作副本维护

WorktreeController 把用户控制操作接入 RuntimeHost 的唯一 Run/队列/事件链，
不向模型暴露管理工具。接收冻结源项目、动作、名称、完整创建提交和目录版本；
start只派发当前Host持有的接收身份。Host 验证控制标记、根租约、实际任务所有者
与完整接收正文后，复用原 manage_worktree 的源码锁、共享Git锁、资源移除门禁。
一次Web操作只有一个控制Run，命令结果与终态属于该Run。

目录版本基于受管记录和记录文件身份；Git操作前重验，防止旧标签操作替换后的
副本。源码锁仅覆盖该次管理操作，其他独立副本运行继续遵循原有隔离规则。
取消等待已派发线程实际退出；未派发任务直接停驻。崩溃恢复不构造Agent恢复
checkpoint、不重放创建或删除。Python开发接口继续复用同一底层实现。

会话创建通过配置控制Session记录请求身份。RuntimeStore在同一SQLite事务提交Session、session.created和http.mutation_completed；回执失败整笔回滚，未知请求不自动重放。成功回执独立于目标Session保存，删除目标后重试只返回旧身份。前端创建和项目切换共享选择代次，迟到请求只更新对应项目缓存，不改变当前页面选择。

项目注册/移除使用配置控制Session持久请求身份，身份关系变更、审计事件及成功回执在同一BEGIN IMMEDIATE事务提交。已有路径注册仍提交独立成功回执；旧注册回放不会重建已移除记录。移除按不可复用workspace_id定位，拒绝启动项目、会话历史、执行引用、源项目副本及活跃任务；不会触碰磁盘文件。浏览器只持久化待确认移除身份及请求摘要，不保存本机目录正文；移除后列表消失也保留原请求恢复入口。

受控评测模型桥接：隔离Host保留执行、工具及验收事实，EvaluationCallBridge把模型请求交给主Host已有重试/调度/预算链。主控制Run上的attempt带case_id/case_index/local_run_id来源；本地observed Token检查按本地父子关系解析这些独占事实。普通响应及摘要用usage_authority指向权威账本，不触发legacy用量回退；未解析引用时complete=false。case_evidence同时导出本地执行与精确模型权威快照，缺少或不匹配的引用拒绝导出，价格仅导出实际使用版本。Web live通过固定规划确认开放；离线控制任务拒绝使用桥接，嵌套桥接和本地有界repair批次明确拒绝，后者需先接完整交付累计归属。

受控评测执行器已接EvaluationCallBridge。live只接受与主控制Run冻结cases完全一致的输入，禁止另建Provider或重新读取连接配置；case使用拒绝直接调用的EvaluationLocalProvider。生成默认值/能力在接收时冻结，新默认不改变排队批次；Provider配置指纹改变则拒绝执行。每题保持12步/20k已观测Token/300秒，项目预算拒绝或未知模型故障终止剩余批次，主Run记interrupted。逐题报告同时保存本地执行和主账本权威快照；未完成批次重启停驻，无自动重放。Python及HTTP控制器须confirmed=True及quote_hash；摘要绑定任务、模型连接/默认配置、规划假设及有效价格版本。只读规划不创建Run、不发请求；按20k Token/题和90/10输入输出比例估算，缺价格明确未知，币种不混合。接收事件固定planning_quote，真实调用仍按请求时价格固定结算；规划不是账单硬上限。

附件HTTP写入使用配置控制Session保存http.mutation_started/completed，只保存正文指纹和公开元数据，不将附件内容写入事件或浏览器存储。上传前校验输入，发布文件与SQLite回执无法同事务，发布或回执故障保留未知started，原键拒绝重复执行。删除继续复核canonical附件引用；成功回放不重读已删除文件。Vue均保留未知请求键，刷新重新选择同内容文件可复用身份。

运行控制HTTP现在强制请求身份。Run/Continue在任何自动Session创建前检查键；停止/审批/放弃恢复由run_mutations记录内容绑定的请求开始和成功回执。已成功的决定在当前终态检查前回放；执行与回执之间发生故障保留未知，不重放。放弃恢复传expected_run_id并在Host会话/工作区锁内复核，防止旧标签页放弃新中断Run。审批严格布尔值，未来状态查询与原回执分离。所有路径继续经原Host/ApprovalBroker，不新建执行主链。

GLM Chat能力声明以供应商、HTTPS精确主机/路径和模型ID共同匹配，普通`/api/paas/v4`及套餐`/api/coding/paas/v4`只确认`glm-5.3-flash`；其他路径或未来模型不推断。GenerationCapabilities补充thinking_modes、top_p_min、sampling_precision与max_output_tokens，Vue按能力显示可用范围。服务默认输出上限也参与最终冻结校验；null仍恢复服务默认。SDK Chat与Responses分别映射推理字段，stream/complete共用同一参数生成器，工具续轮的reasoning_content仍保留为Provider私有状态。

HTTP接收边界以canonical Run身份为准，而非异常类型。admit_run/admit_continue在Store提交后仍可能遇到附件used缓存或dispatch错误；路由检查稳定Run身份，存在时返回202供查询真实终态，不把已接收任务伪装成参数拒绝。尚未创建Run的已知前置拒绝附rejected响应头，未分类故障保留未知；Vue保留未标记409及5xx的Run/Continue请求身份。Skills导入将只读预检与实际发布分开，预检固定版本；进入import_skill或成功回执阶段后发生异常，保留started而不追加失败回执，避免发布成功、安装事实失败时被自动重放。

B5提示注入专项把攻击内容分别置于文件、已固定Skill和真实Hash索引候选中，确认它实际到达模型续轮，再强制模型生成恶意工具调用。执行仍由ToolExecutor/PolicyEngine及worker只读目录决定，不从工具文本推断权限；configure_budget始终不是模型工具。7项新增测试覆盖Plan/Auto与独立worker，预算配置事实和源码都保持原值。既有并发、生成配置、Skills固定、取消恢复及唯一用量专项一并通过218项回归。

交付用量页面通过delivery身份查询cost_view及budget_view，包含旧手动writer、有界修复各轮及其摘要/worker；不以最新控制Run代替整个交付范围。Vue路由保存精确delivery身份，切换身份丢弃迟到响应，刷新重查全历史并替换投影，不在浏览器累加。119项前端及真实API有界交付、采纳、相同费用查询与页面刷新验证通过。

生产retrieval_from_vector所有模式均钳制到最多5个候选结果；format_results单片800、合计4,000源字符，保留每个片段路径/行号/原分数并明确标记截断，展示可回读提示。源文本字符预算不包含引用及格式说明，实际请求仍由input_composition按完整序列化字符统计、供应商usage记账。新边界未改变RRF或分数，缺模型降级说明保持；94项关联及49项补充回归通过（显式本地ML各跳过1项），Ruff/Mypy113通过。


### 只读实验投影与比较身份

`evaluation_metrics.py` 从 schema v2 的逐 case `evidence.usage.calls` 和
`evidence.costs.latest` 派生统计，不消费顶层/父 Run 的聚合用量，不打开或导入执行库。
真实调用按 `(run_id, attempt_id)` 唯一计数，重复报告和冲突证据拒绝；缺少 case
证据继续展示已知部分，但禁止给出完整 Token 或费用结论。reported、estimated、
unknown 独立，input-only embedding 没有虚构输出，cache/reasoning 不重复相加。
币种分开，费用来自当时的固定价格快照；完整费用除全部成功任务数，失败消费仍在
分子内。缺价格、未知消费或零成功使该比值不可计算。

延迟采用所有已观测任务/请求样本的算术平均及 nearest-rank p50/p95，同时给覆盖数量；
没有样本返回 null。缓存比率只在 reported 且同时提供 input/cache-read 的
input-output 请求内计算，返回覆盖请求数和对应输入量，不能外推到所有请求。
尝试失败/未完成、截断、重试、用量未知分别计数并注明分母；不能将没有采集到的
字段当成供应商真实零。

比较身份排除执行 UUID 和产物路径，保留冻结任务版本/hash、输入/hash、实际Python代码hash和Python版本、
有效配置、根Run及每种调用用途的实际生成参数、工具与 Skill 指纹。commit/dirty仍在原报告
溯源，但文档或前端提交在代码hash不变时不使对照失效。相同 variant 的重复报告身份必须一致。
优化差异只能由调用前保存的 `ExperimentVariant` 逐路径明确前后值，Unix 声明时间
复核到最早候选模型请求；已有开发基线可以早于声明，但留出结果不能用于选择优化
或调参。此时间检查是本机证据一致性，不是防篡改的远程预注册服务。
冻结任务、模型、生成参数和样本次数不能豁免。逐题通过数不降以及完整 Token 或
同币种/同价格版本及费率依据完整费用下降是收益声明的必要条件，延迟增量始终同时输出。
scripted 与未跑完的批次保留统计，不产生真实优化通过结论。

恢复交付 grader 的控制动作继续标注 `evaluation_harness`，修复来源分别记录
`scripted_provider_tools`、`configured_provider_tools` 或 `main_runtime_provider_tools`。
重启后的空 scripted Provider 仅服务 verify/apply 控制动作，不反写此前 Writer 的来源；
固定测试、源码保护、留出判分和人工采纳边界保持原契约。

费用比较还核对固定计价行中的供应商、模型、类别、币种、版本及每百万费率指纹；
跨独立实验只复用相同版本名不足以证明价格相同。缺少费率依据仍可显示旧快照，
但不能用该快照宣称费用下降；不会重新计价或覆盖历史。


三类官方SDK客户端显式使用Settings.model_timeout作为连接、读取、写入和连接池等待上限，避免隐含的5秒连接默认值先于配置截止；Host仍限制整个请求生命周期并保持原取消/断流不重放规则。SDK重试保持0，发生未知消费时不释放预留。此修正由真实项目重放中的早期超时暴露，但不能据此断言该次供应商超时的底层原因。



wheel分发检查同时要求workbench实际文件集合与manifest完全相等，拒绝已退役cli.py/web目录及重复ZIP路径。setuptools增量build缓存可能包含已删除源码或旧哈希资源；发布采用确认可丢弃后的干净构建，并以安装包隔离验证决定通过，不能把源码树的干净状态当作包内容证据。


实验稳定身份排除交付随机writer_delivery_id，仅该定位字段不参与对照；本地/容器执行配置等实际条件仍严格比较，原始交付身份完整保留在逐题报告与事件中。重复运行不能因为新建交付UUID被判为改变实验条件。

C4机制补充：`query_rewrite_outcome` 返回真实 `RetrievalOutcome`，原 `with_query_rewrite` 只负责相同结果的文本格式化，ToolExecutor/PolicyEngine边界未变。`rag.benchmark.run_benchmark` 的协作停止保留已完成排名和未尝试数量，不把未派发查询算失败，不完整对照禁止晋升。受控改写实验的Host生命周期适配已接入，真实效果另行验收。


C4受控Python接口复用 `RuntimeHost.run_control_operation`，仅接受 `retrieval_benchmark` 用途。控制Session绑定源项目并归档，接收事实冻结任务和模型配置，执行身份为 `python_experiment`；不借用普通Plan身份、评测控制器或工具展示文本。每条查询/重复独立占原根队列名额，原模型链路持有2槽及项目/日预算。原语料在临时Git目录索引，不修改用户源码；本地索引和排名不产生虚构Token事实。生产工具入口的ToolExecutor/PolicyEngine边界不变。

`retrieval.benchmark_result` 与唯一模型尝试写同一Store，包含原结果和结构化融合结果；导出只派生统计，不再写另一份用量。无效/不变/截断改写保留原排名并标degraded，不视为成功改写；认证、网络或协议故障造成的rewrite_failed、索引漂移、预算及取消停止扩批。队列取消时不伪造已尝试查询，原检索失败则记录已尝试失败。重复取消等待实际请求和本地线程收尾后释放根和模型槽；重启停驻，无自动重放或普通Agent/Continue入口。报告拆分纯检索、Provider及总延迟，返回字符不冒充供应商Token；缺下游证据始终禁止默认晋升。
