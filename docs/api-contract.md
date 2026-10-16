# 前端接口契约（Web UI ↔ Runtime API）

本文是 Web 前端依赖的全部 HTTP 接口契约与实现状态。前端按此开发并对缺失接口优雅降级；后端按此补齐。实现状态：`已实现` / `预留（待后端）`。

## 现有接口（前端已依赖）

### Web 服务入口 —— 已实现

`GET /`返回307，Location为`/workbench/`；Vue资源继续位于`/workbench/`，Hash路由与/api保持同源。旧`/assets/*`返回404。缺失Vue构建产物时`/workbench/`明确503，healthz和API仍可用。部署入口eventide-server和python -m eventide只启动同一FastAPI服务；不提供用户终端命令或兼容转发。

### HTTP 统一错误 —— 已实现

HTTP 状态码保持原义，JSON 错误包含 `code`、可读 `message` 与兼容的 `detail`。例如不存在的 Run 返回 404 和 `{"code":"not_found","message":"Run not found","detail":"Run not found"}`。已有客户端可以继续读取 detail，新客户端优先用 code 判断类别并展示 message。405 保留 Allow 等协议响应头。

常用 code：400 bad_request、401 unauthorized、403 forbidden、404 not_found、405 method_not_allowed、409 conflict、413 payload_too_large、422 validation_error、429 rate_limited、500 internal_error、503 service_unavailable；来源守卫仍使用 invalid_origin。结构化 detail 的 message 为通用说明，字段级信息保留在 detail 列表的 loc/msg/type 等项中。为避免密钥或附件内容泄漏，校验错误不回显 input 和 ctx；已知模型密钥继续脱敏。内部异常不返回原始异常内容。OpenAPI 统一声明 ErrorResponse。

模型配置连接检查仍返回 HTTP 200 及 success/error_type/message/detail，不属于 HTTP 错误包裹。SSE 与导出成功响应保持原媒体类型；在流已经开始后的传输失败不能改写成新的 JSON HTTP 响应。

连接检查另返回`run_id`及`usage`（统一attempt账本）。接收前配置验证失败时run_id为null且无调用；已建立控制Run则成功及业务失败均返回精确身份。预算拒绝error_type为budget_exceeded。控制Session标记connection_probe，不可接受普通Run/Continue；不进入根Agent队列，使用共享模型槽、该Run及UTC日预算。取消/超时无usage保留未知，重启不重发。

预算reservation.context.project_id为string|null；只有事务内验证的独立连接检查可以为null，其费用不归属控制Session存储目录所在项目。已有Run、每日usage/cost及未知核对接口均适用。

`POST /api/config/provider/test`必须携带1–128个可见ASCII字符的`Idempotency-Key`；缺失或非法时422且不创建控制Session。相同键及候选内容返回原HTTP200结果，不新建请求；不同内容409。在途或未确认Run返回409并附`X-Eventide-Run-ID`，应保留原键查询/重试，不能换键规避未知消费。接收事实仅保存内容摘要；候选密钥不落库。未显式提供密钥时首次检查使用当时冻结的已保存配置；同键重试仍返回首次结果，不因之后配置变化重新调用。

重启或响应记录中断后，重试只读取原Run终态并补齐HTTP回执，不重新派发。恢复结果`recovered=true`，无法复原的`latency_ms`和`checked_at`为null；已接收但未建立Run的中断记录返回`error_type=not_dispatched`和null run_id。普通结果`recovered=false`。成功/业务失败继续HTTP200；请求身份、来源或格式错误仍用HTTP错误包裹。30秒触发取消后等待客户端收尾，不保证严格30秒总墙钟上限。

Vue设置页通过`durableMutation('probe', ...)`调用此接口，结果独立于已保存Provider状态；仅显式点击发起。等待时锁定候选字段及保存/恢复操作。网络/5xx/未标记409保留原键及表单摘要，刷新后由用户重新输入原密钥核对，不保存凭据。新候选不得替换尚未确认的请求；HTTP200业务失败仍是确定回执，用量未知由账本及预留保守保留，不视为零消费。页面区分null耗时、unknown用量和无Run的未派发结果。

### 浏览器写请求与运行幂等 —— 已实现

工具调用错误的canonical metadata含稳定code（invalid_arguments、invalid_tool_contract、unknown_tool、permission_denied、timeout、execution_failed、outcome_unknown）。第一次参数错误交回模型修正，再次参数错误结束该Run；错误结果仍保存，不自动重放已执行工具。截断响应的调用不会派发，其未执行事实补齐历史消息。

写请求的Origin必须匹配本机服务源，cross-site/null/异常端口和DNS-rebinding来源拒绝；本机非浏览器客户端可省略Origin。没有放开CORS，开发前端应使用同源代理。

下文“仅回环／仅本机”的管理权限默认仍只接受回环连接。容器部署可通过启动配置 `EVENTIDE_LOCAL_PROXY_PEERS` 显式追加精确 IP；这些额外连接必须同时携带匹配服务端回环 Host 的 Origin，缺失／不匹配返回403，不能利用伪造转发头获得权限。配置在应用创建时固定，不属于模型或运行配置 API。即使未来只读管理路由使用此守卫，额外连接也需满足同样来源检查；现有普通GET契约不变。宿主仅发布回环端口，服务入口禁用代理头解析，不提供公网认证。

Run与Continue要求`Idempotency-Key`（1–128个可见ASCII字符）。同Session/操作/键确定一个持久Run身份，内容hash写入admission。重试相同内容返回原202接收凭据，不新增消息、调用或turn；相同键不同内容409。接收尚未持久化的竞争请求可返回busy，客户端保留相同键稍后重试。幂等接收不等于自动重放工具，未知结果仍停驻。Skills 导入和配置的幂等契约见下文；Delivery等长任务使用下述专用持久控制面。

### 模型生成参数 —— 后端及 Vue 已实现

- `GET /api/generation/defaults?model=...` 返回 `model_identity/defaults/session/capabilities/config_hash`，身份绑定Provider、endpoint和模型。
- `PUT /api/generation/defaults` 接受 `{model, generation, capabilities?, expected_config_hash}`；显式能力声明标为declared，不冒充已验证。
- `GET/PUT /api/sessions/{id}/generation` 读取/替换该模型下的会话覆盖，PUT不允许重新声明模型能力。
- `POST /api/sessions/{id}/runs` 新增 `generation`：temperature、top_p、max_output_tokens、reasoning_effort、thinking。省略继承，null恢复默认，0有效；额外字段/非有限值/范围错误422。
- 写配置仅本机来源；支持情况未知时拒绝显式采样参数。接收时冻结，Continue相同接入继承；已有调用不受后续设置影响。尚无自动运行探测或按任务预设。

### 请求用量账本 —— 后端及 Vue 已实现，费用控制面见 U2/U3

`GET /api/runs/{id}/usage`、`GET /api/sessions/{id}/usage` 返回独立模型尝试的 `calls/reported_totals/estimated_totals/unknown_attempts/incomplete_attempts/complete`。默认包含持久parent_run_id关联的子Run；`include_children=false`只取所选自身。Session汇总覆盖全部历史，不受Run列表分页限制；每个真实调用只计一次，不重复加入终态或worker汇总。

无usage是unknown，不是零；缓存与推理Token为细项，不能再加到总量。账本暂不报价，原`usage`字段保留兼容；需要完整性判断时使用新账本。历史响应只能回退统计已观察响应，complete保持false。取消和失败请求无返回usage时保留未知。

calls另带`usage_basis: input_output|input_only`；旧调用默认input_output。仅输入型embedding事实采用input_only，reported输入完整即可usage_complete=true，output_tokens保持缺省，不能渲染为已报告0。费用和预算从首次预留的相同口径派生，普通生成缺输出仍为不完整。底座已验收，远程embedding生产调用按C阶段接入。

### 流式预览传输 —— 主链、SDK 与 Vue 已实现

Run事件流可显式传`include_preview=true`接收`event: model.preview`。此类帧无SSE id及session_seq，含ephemeral、active、request_id、revision、有界text与省略字符数，不可用于推进持久事件游标或Token计数。默认不发送，兼容原客户端。完整响应仍由持久model.response提供，结束/失败清理内存预览；重启不会恢复预览。Vue 已订阅临时预览并独立维护持久游标。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/workspaces` | 工作区列表 |
| `POST /api/workspaces` | 注册工作区（仅回环） |
| `GET` / `DELETE /api/workspaces/{id}` | 工作区详情 / 移除（仅回环、需无会话） |
| `GET /api/workspaces/{id}/sessions` | 会话列表（`include_archived`） |
| `POST /api/sessions` | 创建会话（`workspace_id` / `working_directory`） |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | 会话状态（含 `task_plan`/`active_task_id`/`task_state`）/ 标题与归档 / 删空记录 |
| `GET /api/sessions/{id}/runs` | Run History（`limit`/`before` 分页） |
| `POST /api/sessions/{id}/runs`（202） | 提交 Run |
| `POST /api/sessions/{id}/continue`（202） | 恢复停驻（409=校验失败） |
| `POST /api/sessions/{id}/abandon` | 放弃恢复 |
| `GET /api/runs/{id}` | Run 详情（含 usage/steps/tool_calls/reason/gap_files） |
| `POST /api/runs/{id}/cancel` | 停止运行 |
| `GET /api/runs/{id}/events`（SSE） | 事件流（`after` 游标；prepared/completed 以 `tool.request`/`tool.result` 别名推送） |
| `GET /api/runs/{id}/export` | NDJSON 审计导出 |
| `POST /api/runs/{id}/approvals/{approval_id}` | 审批决定 |
| `GET` / `POST /api/sessions/{id}/attachments` | 待提交附件清单 / 上传 |
| `GET` / `DELETE /api/sessions/{id}/attachments/{aid}` | 下载 / 删除未使用附件 |
| `GET`/`PUT`/`DELETE /api/config/provider`、`POST /api/config/provider/test` | 模型配置（仅回环） |

## v2 新增（全部已实现）

### `GET /api/workspaces/{workspace_id}/capabilities` —— 已实现

只读能力自省。纯文件解析：不连接 MCP 服务、不含任何密钥/命令/env。

```json
{
  "workspace_id": "ws_x",
  "skills": [{"name": "code-review", "description": "Review code for ..."}],
  "mcp": [{"name": "docs", "transport": "stdio"}],
  "paths": {"mcp_config": "C:\\proj\\mcp.json", "skills_dir": "C:\\proj\\skills"},
  "notes": ["未配置 mcp.json"]
}
```

错误：404 工作区不存在；mcp.json 解析失败写入 `notes`，不返回 500。前端：顶栏「能力」面板；请求失败显示空态。

### `GET /api/runtime/settings` —— 已实现

```json
{"context_limit": 50000, "approval_timeout": 60.0, "max_steps": 30, "task_max_steps": 12, "run_token_limit": 0}
```

前端：上下文指示器分母、审批超时文案、停驻文案。请求失败前端使用默认值。

### Run summary 增加 `usage` —— 已实现

`GET /api/sessions/{id}/runs` 与 `latest_run` 每条 Run 增加 `usage: {"input_tokens": n, "output_tokens": n}`，供用量分区的会话累计。

### 计划依赖与命令结果 —— 已实现

`task_plan` / `task_state.tasks` 的任务项支持 `depends_on: ["t1"]`；投影对旧事件默认返回空数组。前端只展示服务端给出的前置任务，不重复校验依赖图。只有 `todo_write` 写入计划，校验失败不产生计划更新事件；依赖关系只约束计划状态，不表示验证通过。

canonical `tool.completed`（SSE 别名 `tool.result`）增加 `metadata`，其他工具默认为 `{}`。Shell 提供 `exit_code`、`timed_out`、`duration_ms`、`truncated`、`stdout_bytes`、`stderr_bytes` 和两流的 `*_omitted_bytes`；stdout/stderr 文本在 `content` 中分开展示。每流最多保留 25,000 字节头尾，省略部分不会保存或被分页恢复。Shell 的 `is_error` 按非零退出码或超时判断，不依据输出中的 `Error:` 字样。

### `model.request` 事件 payload 增加 `request_chars`、`tool_catalog_chars` —— 已实现

每次模型请求的序列化大小（与上下文预算同一度量）与工具目录大小。前端：composer 上下文指示器 `request_chars / context_limit`；缺失时显示 `--`。

### RunBody 扩展 `mode` —— 已实现

`"auto"`（默认）| `"plan"` | `"agent"`；未知值按 `auto` 处理，不拒绝。

- `plan`：工具目录收敛为只读白名单（`read_file`/`glob`/`read_tool_result`/`compact`/`todo_write`/`load_skill`），system 追加只读声明；写/bash/MCP 不在目录中，模型无法调用，executor handlers 同步受限，幻觉出的写调用不会命中处理器。注意：plan 模式仍会按 Workspace 配置连接 MCP 服务，只是其工具不进入目录。
- `agent`：该 Run 内 ASK 决策自动允许，不再等待用户；审批审计事件照常写入，`approval.resolved` payload 带 `"auto": true` 且 `author="runtime"`（前端时间线显示“agent 模式自动批准”）。
- `auto`：现状行为。

### RunBody 扩展 `attachment_ids`、附件生命周期与多模态 —— 已实现

上传（JSON，不引入 multipart 依赖）：

```json
POST /api/sessions/{id}/attachments
{"name": "notes.py", "media_type": "text/x-python", "content_base64": "..."}
→ 201 {"attachment_id": "att_<12hex>", "name": "notes.py", "size": 1234,
       "media_type": "text/x-python", "kind": "text", "used": false}
```

约束：单文件 ≤10MB；base64 或 media type 非法、声明为文本但不是 UTF-8 时返回 422；会话不存在返回 404。`media_type` 可省略并按文件名推断。内容存储于状态根 `attachments/~<sha256(session_id)>/{attachment_id}`，不进入 Workspace 或 Git。

生命周期：`GET /api/sessions/{id}/attachments` 默认只列出未使用项，传 `include_used=true` 查看全部；单项 GET 下载原始字节；DELETE 只删除未进入 Run 的附件，已使用附件返回 409。前端刷新时恢复未使用 chips，移除 chip 同步调用 DELETE。

提交：RunBody 带 `attachment_ids: ["att_..."]`；202 前校验归属和当前 Provider 能力。UTF-8 文本内联进用户消息（分隔符 `--- 附件：{name} ---`），单附件超过 512KB 截断并标注。图片作为多模态块支持 Anthropic、OpenAI-compatible 和 OpenAI Responses；PDF 支持 Anthropic 与 OpenAI Responses；普通二进制可存取，但没有当前 Provider 协议映射时返回 422。事件日志只保存附件 id、名称、类型与大小，Provider 请求前从状态根水合 base64，因此审计导出不复制原始二进制。

### `GET /api/models` —— 已实现

按当前 Provider 配置探测可用模型列表（`eventide/model_catalog.py`）：

```json
{"models": ["model-a", "model-b"], "error": null}
```

失败语义：未配置 API Key 或探测失败时返回空列表与 `error` 说明，不返回 5xx；响应不含任何密钥。前端把结果放入 composer 的本次模型下拉，并始终保留 Host 默认模型作为回退。

### RunBody 扩展 `model` —— 已实现

`model` 为可选的非空模型名（≤256 字符）。省略或空值使用 Host 默认模型；非空值只覆盖本次 Run 的模型请求、`run.started.model` 和上下文 checkpoint 身份，不修改持久 Provider 配置。Provider、Base URL 与 API Key 仍由 Host 配置决定。

### Run 接收、Continue 与 Token 软上限 —— 已实现

普通 Run 与 Continue 返回 202 前，Host 在同一事务写入 Run/turn 身份、`run.admitted`、用户消息或恢复事实及 checkpoint；返回后 GET 可立即查询，取消可以早于后台执行。默认source准备和实际执行占用Workspace，queued仅持Session身份；显式snapshot/worktree在准备完成后释放源锁，按Session和执行副本执行，等待时不持执行锁。同 Session 冲突、直接源 Workspace 冲突或停驻冲突返回 409，模式/模型/附件校验失败返回 422（Continue 恢复校验失败为 409）。重启只将未结束 Run 停驻，不自动执行。

Continue 保持原 Plan，只在相同 Provider 和 Base URL 下沿用选定模型；连接变化则使用当前默认模型。原 Agent 模式恢复为 Auto，自动审批不跨 Run。附件的归属以完整 Session ID 为准，DELETE 对 canonical 引用返回 409，不仅依赖文件 used 标记。

`reason=token_budget` 表示已观测的输入+输出用量达到 `run_token_limit`，默认 0 关闭。摘要成功响应也计入，摘要事件不作为用户消息显示。单次响应可越过上限，Continue 重置预算；没有用量的失败请求不估算。前端用量继续消费 Run 的 usage，停驻区显示 Token 软上限原因。

### 受控只读子任务 —— 已实现（显式开启）

Workspace capabilities 增加 readonly_workers（enabled/roles/max_concurrency/max_depth/max_tasks_per_run）。父 Run 的 worker.started/completed 事件关联 child_run_id，子 Run 查询和 history 返回 parent_run_id/worker_role/snapshot_id。子Session默认归档且不能接收普通Run、Continue或附件。父usage含子消费，子usage仅自身；跨父子汇总需按exclusive模型事件计算。

新子任务的canonical `run.admitted.delegation`含fingerprint和parent_call_id，不含额外目标正文。相同Run/显式Continue链的重复角色、精确目标和源码快照（包括批内重复）使整个delegate工具调用返回错误，零子任务派发；已有任务返回`existing_calls`中的父run_id/call_id及child_run_id供查询，未缓存回答。该错误是工具结果，不更改HTTP状态码或工具schema；普通新Run、其他Session与源码/角色/目标变化不共享去重。旧无指纹接收事实不补造精度，仍保留原四子任务上限。

### 交付、执行配置与未知结果 —— 已实现

交付由下述持久HTTP与Vue有界修复控制面提供准备、修复、查询、补丁核对和人工采纳；手动run/verify保留Python开发接口。原Run/Session/SSE/export可读取Writer与control Run证据；delivery事实与patch artifact hash存同一Event Log。Writer的execution_config在后续Run/Continue生效；图片/PDF契约不变。control Session归档且不能用于普通模型执行。

新增canonical tool.outcome_unknown与终态reason=unknown_tool_outcome；它保留未确认调用，不伪装成tool.completed，相关Workspace阻止普通Run、Continue和交付写入。前端显示“工具结果未确认”，不能将其展示为已停止或允许继续。未知状态不因abandon或重启清除。导出仍包含完整canonical证据，默认脱敏Trace只派生安全元数据。


## Vue 客户端兼容规则

Vue HTTP 客户端规范化列表/详情返回的 Run `id` 为内部 `run_id`；202 接收凭据仍直接使用 `run_id`。Run 列表是最近一页按追加顺序排列，最新项位于末尾。工具待审批期间，`/messages` 可能因工具历史尚未配对返回 409；客户端暂留上次可见消息，继续查询 Run/Session，不能因此隐藏审批或停止按钮。

历史页 `#/history?session=<session_id>&run=<run_id>` 使用 `GET /api/projects/{id}/sessions?include_archived=true` 确认当前项目归属，再调用既有 Session Run 列表、精确 Run 详情及 export。列表请求固定 `limit=100`；下一页 `before` 使用当前已加载最旧 Run 的身份，服务端按追加 rowid 排序且排除游标本身，不使用时间戳翻页。每页仍按旧到新返回，Vue 反向展示；`hasOlder` 仅由满页推断，没有伪造总数。精确详情验证 `run_id` 和 `session_id` 后才显示导出入口；URL 恢复不要求先拉取全部中间页。JSONL 返回 canonical 事件，工具详情投影不是独立 trace 导出。本页没有写请求，不更改当前执行会话；会话管理与独立 trace 已分别由专页及历史详情承接，历史导入仍待迁移。

Vue 使用 `include_preview=true` 订阅流式临时预览。`model.preview` 无 seq/id，不推进持久游标、不写入消息历史；`model.response` 与终态触发真实消息刷新并清除预览。客户端保留原提交键处理网络/超时/5xx 下的不确定接收，明确参数拒绝后允许编辑。开发代理保留原 Host，使浏览器 Origin 与 API 的请求源相符。

## Skills 管理与运行记录 —— 已实现

| 方法与路径 | 契约 |
|---|---|
| `GET /api/skills/bundled` | 三个内置包的预检元数据、版本及资源清单，不安装 |
| `GET /api/workspaces/{id}/skills` | installed、configuration、configuration_hash、enabled、project_packages；启用配置损坏返回诊断，仍可管理 |
| `POST /api/workspaces/{id}/skills/preview` | 目录／ZIP／内置包预检，返回 valid、diagnostics、manifest、version，不写入 |
| `POST /api/workspaces/{id}/skills/import` | 必填 Idempotency-Key，完成返回内容版本；不执行脚本 |
| `PUT /api/workspaces/{id}/skills/configuration` | 必填 Idempotency-Key，替换当前项目的用户版本、禁用项目包及主动选择 |
| `GET /api/skills/{name}/versions/diff?before=&after=` | 固定版本的新增、删除、修改资源和最多20,000字符的正文差异，含截断标记 |
| `GET /api/skills/{name}/versions/{version}/resource?path=&offset=&limit=` | UTF-8资源分页；offset为字符偏移，limit最大12,000；路径越界、二进制或篡改拒绝 |
| `GET /api/runs/{id}/skills` | 固定 snapshot（旧数据可为null）与实际 loaded 事件；seq来自canonical session_seq，刷新和重启不丢失 |

PackageBody 严格接受 directory、archive_base64、bundled 三选一，以及 scope=user/project；可选 expected_version 为预检得到的64位摘要，源变化则422且不安装。目录指服务所在主机路径，ZIP仍执行压缩体、解压总量、文件数与链接检查。导入/预检/配置仅接受本机来源并通过同源守卫。当前包操作有界、完成后返回200，不是后台长任务；索引/评测/交付另行提供长任务身份。

配置字段为 enabled_user（名称→固定版本）、disabled_project（project:name列表）、selected（精确scope:name列表），各最多64项。可选 expected_configuration_hash 绑定读到的配置；不一致409，旧页面不得覆盖新配置。保存配置不影响已接收 Run。RunBody.selected_skills 省略或null继承项目选择，显式[]清空本轮选择；选择不会启用未授权包。

幂等键为1–128可见ASCII字符，身份由项目控制Session、操作与键确定。同键不同内容409；已完成请求重放原结果，不再读取导入源或重新安装。失败重放原拒绝；仅有started事实时返回409 running/outcome unknown，不自动重放潜在副作用。写入回执来自唯一Event Log的http.mutation_started/completed/failed，原ZIP与正文不写入回执。Vue只持久保存请求指纹和键，配置冲突需先刷新；网络不确定的原请求可用原键显式重试。


## Vue 静态入口

`GET /workbench/` 提供Python包内Vue页面；Hash路由无需后端路径回退，HTTP/SSE仍走同源`/api`。静态资源缺失404；整个构建缺失时工作台返回503 JSON错误，healthz/API仍可使用。根地址旧前端继续保留到X验收。`/workbench/manifest.json` 仅含构建来源指纹与文件校验清单，不含运行状态或凭据。


## Project 聚合与执行副本身份

- `GET /api/projects`：返回稳定 `project_id`、`source_workspace_id`、`name`、`git_common_dir`（可空）、`created_at`、`source_path`、`workspace_count`、`session_count`。
- `GET /api/projects/{project_id}`：单项目；不存在为404。
- `GET /api/projects/{project_id}/workspaces`：项目的全部源目录和执行副本，返回原 Workspace 状态及新增 `project_id`、`source_workspace_id`、`kind=source|worktree`。
- `GET /api/projects/{project_id}/sessions?include_archived=false`：跨该项目全部副本聚合 Session，归档过滤与原 Workspace 列表一致。Session 状态增加项目和源身份，保留实际 `workspace_id` 和 `working_directory`。

注册继续通过 `POST /api/workspaces`；按规范路径重复注册返回同一身份。Git worktree由服务器验证共同元数据并关联，客户不能指定Project归属或修改已有Session绑定。独立clone不按remote合并。移除仍经原Workspace DELETE；源有副本或任一Workspace有Session/Run执行引用时409，不能留下孤立历史。查询与模型执行不创建额外项目事实源；新Run在canonical接收/开始事实固定项目归属，旧事件不回写。Project聚合不改变202/SSE游标；执行并发与Continue采用下述P2执行副本契约。


## 请求预算事件（U2部分接入）

Run的SSE/export新增budget.reserved、budget.settled、budget.warning、budget.blocked。预留和尝试开始、结算和尝试结束分别原子提交，各使用正常持久session_seq；临时模型预览口径不变。budget.blocked含request_id和violations，不产生虚假的model.attempt_started，也不派发请求；Run以interrupted结束且reason=token_budget。budget.warning含attempt_id及达到80%的作用域摘要。预留中的input_estimate/output_estimate和estimate_method都是估算，不能作为已报告用量。

Run/Session现有usage接口仍只按model.attempt事实计费，不累加budget.settled。worker主调用的kind为worker，摘要仍为context_summary；预算/费用HTTP控制面如下，Vue接入另行验收。

## 预算、价格和费用控制面（U2/U3 后端已接入）

以下写接口均要求本机同源和`Idempotency-Key`（1–128可见ASCII字符）。正文严格校验，不把字符串、布尔或小数转换成整数。相同操作/键同内容返回原结果，同键不同内容409；重启继续识别原键，结果未知的mutation返回409要求检查状态，不自动重做。错误沿用统一code/message/detail结构。

对于预算路由中确定未生效的前置拒绝或已回滚的原子账本写入，失败响应额外携带`X-Eventide-Mutation-Outcome: rejected`，并在`http.mutation_failed`保存`outcome:"rejected"`；原键重放保留相同标记。客户端仅在收到此明确标记后才能丢弃旧键，让用户在条件变化后重新提交。活跃attempt核对被拒绝后，即使已经结束，原键仍重放原拒绝，新键才能重新检查。正在执行/结果未知、同键异内容、意外存储错误及未显式声明的旧失败均没有此标记；HTTP409本身不表示无副作用。

| 方法与路径 | 结构与语义 |
|---|---|
| `GET /api/budgets` | 全部已配置预算及全部预留明细；未配置不代表消费为零。 |
| `GET /api/budgets/{kind}/{identity}` | kind为run/project/delivery/day；返回指定预算，即使尚未配置。day是UTC的YYYY-MM-DD，其他身份必须存在。 |
| `PUT /api/budgets/{kind}/{identity}` | `{token_limit:int|null,expected_configuration_hash?:string}`；额度0阻止新请求，null关闭；可选hash来自GET，只随配置改变，过期409。 |
| `GET /api/prices?provider=&model=&currency=` | `{prices:[...]}`，参数为可选精确过滤，全部版本保留。 |
| `POST /api/prices` | `{version_id,provider,model,category,currency,price_per_million,effective_at}`；返回登记的价格；版本身份内容冲突或同类别/币种/生效时间重复409。 |
| `GET /api/costs` | 全历史费用和用量，筛选参数见下文。 |
| `POST /api/budgets/attempts/{attempt_id}/resolve` | `{action,actual_tokens?,reason}`；返回`{action,attempt}`，精确预留不存在404，在途或已结算409。 |

预算响应为`{timezone:"UTC",budgets:[],reservations:[]}`。预算项包含kind/identity、token_limit、actual_tokens、reserved_tokens、committed_tokens、unsettled_attempts、remaining_tokens、warning、blocked、configuration_hash。预留项包含attempt_id/run_id/session_id、status、reserved_tokens、actual_tokens、usage_source、buckets、warnings、context（project_id/root_run_id/delivery_id）、input_estimate/output_estimate/estimate_method/reserved_at及audit。预留项reserved_tokens是原始请求估算；部分已报告量超过估算时，各bucket保留更大的额度。actual_tokens未知为null，不能呈现为真实零；status区分unsettled/settled/manually_resolved。

价格category限定input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens/reasoning_tokens；currency为3位大写代码，price_per_million是非负有限十进制字符串，effective_at必须含时区并归一UTC。新增价格不重写旧费用；HTTP未开放重估操作。

费用查询支持成对的`scope_kind=run|session|project|delivery|day`与`scope_id`，另有`include_children=true`（只影响Run/Session）、可选`date_from`/`date_to`，均为包含端点的UTC日期。新调用使用预留UTC日，跨午夜结果仍归原日；历史未预留调用按调用开始UTC日。Run默认含持久子Run，Session包含全部历史Run和子Run，Continue不会隐藏历史消费。Project包含全部执行副本，Delivery使用实际固定交付身份，不按页面选择猜测归属。

返回known_costs与estimated_usage_costs（各币种→十进制字符串）、unknown_attempts、estimated_attempts、unpriced_attempt_ids、complete、latest、history、usage（现有独立调用账本）、attempt_count、historical_unreserved_attempt_ids、scope、timezone:"UTC"、date_bounds:"inclusive"。历史无预留调用保持用量但费用未知；既没有费用也没有价格时返回空映射，不能显示为免费。latest每尝试仅一份最新估价，history保留全部版本，不重复累计；缺失用量或价格时complete=false，币种不可直接相加。

未知处理action必须显式选择：keep_unknown禁止携带actual_tokens，仅追加已核对审计并保留原额度；record_known必须给出非负整数actual_tokens且不低于已报告部分用量。reason必须为非空1–256字符，已知密钥脱敏后审计。处理需要调用结束或Run终态证据，不能干扰在途结算。人工总量只进入预算，Provider usage及旧费用仍保持原精度；未知处理没有释放成零的默认动作。

## 根 Run 执行副本（P2）

RunBody 新增 `execution_target: "source" | "snapshot" | "worktree"`，默认 source。snapshot 必须同时使用 mode=plan，否则422且不接收Run；未知target由请求schema拒绝。幂等指纹包括target，同键变更目标返回409。Python RunRequest提供同名字段。

`GET /api/runs/{id}` 与 `GET /api/sessions/{id}`、Session列表增加 execution：source为 `{"kind":"source"}`；snapshot为 `{"kind":"snapshot","workspace_id":"原逻辑WorkspaceID","snapshot_id":"64位摘要","working_directory":"相对cwd","skipped":0}`。Session顶层working_directory仍是逻辑源路径；execution明确实际副本，不返回内部绝对产物路径或重绑Session。完整manifest和源checkpoint保存在canonical run.admitted/run.started中，可由已有事件/export读取。

同Session接收冲突409；不同Session的snapshot模型执行可以同时进行。准备时源目录独占，已在直接运行的源Workspace拒绝新快照；复制期间漂移、无可信Git HEAD、规模超限、产物损坏或附件/参数无效422。快照执行期间源目录释放，直接源操作仍按原规则独占。停止按精确Run归属，Continue自动继承原snapshot，不接受客户端改换执行副本；恢复校验失败409。重启仅停驻，用户显式Continue。快照暂不提供MCP、源知识索引、嵌套worker和脚本执行，排除的文件不会静默回读源路径。

Vue预算写入共享持久mutation客户端，按操作、正文摘要和路由保存请求身份，不把凭据或完整正文写入浏览器存储。确定拒绝可生成新键，未标记409和网络失败保留原键。接口类型已随预算、价格、Unknown处理和execution_target重新生成；前端响应展示不改变服务端全历史、未知值、UTC日或价格版本口径。

worktree 使用现有 auto/agent/plan 模式，首次接收要求源目录干净且可信，失败422并不接收Run。Run/Session 的 execution 为 `{"kind":"worktree","workspace_id":"逻辑源ID","execution_workspace_id":"实际副本ID","working_directory":"相对cwd","name":"受管副本名","base_commit":"完整提交哈希"}`；顶层 Workspace/cwd 保持逻辑身份。完整副本身份及源checkpoint留在 canonical admission，客户端不能指定实际副本或项目归属。

同 Session 后续 Run 必须继续传 worktree，服务复用首次副本和未提交修改；省略字段恢复默认 source 也会422，不能静默更换执行目录。Continue 自动继承绑定；新副本由新建 Session 获得。放弃中断保留绑定及副本未知结果隔离。接收后副本漂移阻止模型派发，Continue 的副本漂移/未知结果为409；源后来的变化不改变已创建副本。不同 Session 的写入 Run 可并发，同 Session 或实际副本冲突仍409。Worktree引用阻止DELETE，源忙状态不代表所有隔离对话都忙；共享Git命令仍由服务协调。

Vue已生成source/snapshot/worktree类型；读取Session execution恢复绑定，既有worktree新Run显式携带worktree。普通状态轮询不能改变待提交target，创建新Session时禁止在旧会话控件上修改执行选项；Continue无需客户端重发副本身份。

## 运行队列与接收容量（P3）

`GET /api/queue?project_id=<id>`返回`limits`、`counts`、`queued`与`active`。可选项目不存在为404；过滤只影响两个列表，limits/counts始终描述整个服务。默认limits为`{active_roots:4, model_requests:2, waiting_roots:20}`。counts包含active_roots、waiting_roots、preparing_admissions、active_models、waiting_models；准备ticket尚未成为接收Run，因此不出现在列表中。

每项包含run_id、session_id、project_id、workspace_id、execution、admitted_at、position、status及dispatch_state。execution沿用Run查询的source/snapshot/worktree身份；queued的position为按提交顺序的全局一基位置（过滤后可有间隔），active的position为null。dispatch_state取queued/starting/running/finishing；status取现有canonical Run状态，并新增queued。starting已拥有根名额但还没有run.started；finishing已写终态但工具清理尚未结束，仍占名额。

Run和Continue仍在输入、参数、Skills、执行位置、checkpoint及身份持久接收后返回202，原accepted响应形状和幂等重放不变。4根加20等待满额时返回429，不创建Run/消息；同Session忙或源正在直接运行返回409。源忙时不能准备新快照/worktree。直接源等待任务若无可信Git checkpoint在接收前422拒绝；Python入口遵循同一约束，不后台无身份等待，调用成功仍await最终RunResult。

GET Run/Session/list-runs中的queued不是终态；SSE持续观察到真实终态，run.admitted是已接收证据。停止沿用`POST /api/runs/{id}/cancel`；queued取消写interrupted、保留目标、没有模型attempt/Token预留。停止活跃任务必须等副作用收尾才释放根名额。重启将queued/started停驻，不自动执行；恢复Continue使用新身份并复核旧checkpoint，不能用恢复时的源码替换接收基线。队列等待和模型槽等待均阻止Provider配置变更，原来源与幂等保护继续生效。

Vue队列路由已绑定OpenAPI路径，queued保留SSE观察与精确取消入口。6个真实API对话验证4根/2模型/2等待，取消等待后的usage.attempt_count=0且预算预留为空；停止根任务后自动认领下一项。

用量账本calls新增兼容字段`provider: string|null`、`duration_ms: number|null`、`stop_reason: string|null`，Run/Session usage及costs内嵌usage共用同一投影。耗时仅透传非负有限的已记录数值（含真实0），started-only尝试不生成终态耗时/结束原因；历史事件缺失返回null，不以finished_at-started_at推算。原Token去重、来源和完整性口径不变。


源码checkpoint现可包含`checkpoint_policy:{version:2,excluded_untracked:[相对文件],excluded_untracked_directories:[相对产物目录]}`；它随原canonical checkpoint事实保存，不是客户端可配置的排除项。仅实际Runtime产物为例外，项目Skill或普通源码变化仍拒绝派发。状态目录等于Workspace根的Run接收返回422；历史停驻Run的指纹策略与当前不同，Continue返回409且原checkpoint不变，不能由重试或重启覆盖。

### 输入组成字符统计

上述usage账本的每个calls项新增`input_composition: object|null`，Run/Session usage和costs内嵌usage完全一致。新调用从`model.attempt_started`读取，结构为：

```json
{"method":"serialized_unicode_characters_v1","unit":"characters","total_characters":100,"components":{"instructions":10,"history":20,"tool_catalog":10,"skills":10,"retrieval":10,"tool_results":10,"envelope":30}}
```

所有计数为非负整数，七组件合计必须等于total_characters；图示数值仅说明结构。字段集合、method、unit、数值和合计严格验证，非法/旧版/缺失为null，不修正成零或重新估算。仅started携带才有效，finished不能替代或覆盖；取消、失败和尚未结束的尝试仍可具有有效字符统计，usage_source继续保持其真实状态。

统计是最终Provider中立system/messages/tools对象经json.dumps(ensure_ascii=False)序列化后的Unicode字符，含转义/结构，不是供应商wire、Token或费用。instructions统计system序列化字符串，tool_catalog统计整个tools数组；message.content块按已确认配对的工具结果分别归skills/retrieval/tool_results，其他内容及message中role/content以外的扩展字段值归history，剩余角色/字段名和JSON结构归envelope。仅唯一且先出现的assistant tool_use可为user tool_result建立归属，加载Skill正文/资源和search_knowledge分别归对应项；错序/孤立/重复ID不猜来源。摘要、图片及provider_state按当前请求本身统计，不还原已压缩来源；不能把Skills字符项解释为其完整因果成本。

## 工程评测

支持内置工程冻结集，默认offline的scripted机制验证标注`measurement_kind=scripted_mechanism_validation`、`billing_applicable=false`，不代表真实模型成功率。live须按下文规划确认契约显式接收。以下POST沿用本机/同源防护，`Idempotency-Key`必填（1–128可见ASCII字符）。

- `GET /api/evaluations/suites` → 数组：`id,benchmark_id,version(int),description,split(null),total,splits,manifest_sha256,suite_sha256,inputs_sha256,supported_modes:[offline,live],default_mode:offline`。先完整验证20题再公布，损坏不能静默隐藏。
- `POST /api/projects/{project_id}/evaluations` → 202：`evaluation_id,run_id,session_id,project_id,status=accepted,status_url,events_url`。严格请求为`{suite_id:engineering-v1,suite_sha256:<catalog hash>,split:development|holdout|all,mode:offline}`；split默认development，mode默认offline。live额外要求quote_hash与confirmed=true；未知字段422，禁止路径、脚本、模型或凭据输入。同键同内容返回原202和身份，包括重启/终态；同键异内容409。suite过期、同项目busy为409，全局队列满429，确定未接收的拒绝带`X-Eventide-Mutation-Outcome: rejected`。不得在内容冲突或结果未知时自行换键。
- `GET /api/projects/{project_id}/evaluations?limit=20&before=<run_id>` → 最新在前的详情数组，limit范围1–100。
- `GET /api/evaluations/{evaluation_id}` → 身份、suite_id/suite_version/suite_sha256、split/mode、机制标记、`status`（queued/running/completed/failed/interrupted）、`dispatch_state`（queued/starting/running/finishing/null）、queue_position、total/attempted/passed/failed/remaining、current_case、cases、report_available/report_url/events_url/error。cases逐项`id,category,split,status,passed(bool|null),reasons,duration_ms`；未执行passed为null。批次completed表示全部案例执行完，失败数另计。
- `POST /api/evaluations/{evaluation_id}/cancel` → 当前详情；排队/运行取消均等待实际收尾并变为interrupted，终态重复停止无副作用。活动取消请求按键摘要记入原Run事实；已完成终态不追加事实、不重启。通用Run取消端点亦有效。
- `GET /api/evaluations/{evaluation_id}/report` → B2 schema_version=2完整JSON（含status/passed/total/attempted/remaining/results/provenance等），验证文件名、目录身份与不可变SHA256。无报告404，已有证据文件丢失/变更/越界409。此为只读GET，容器同源浏览器不需Origin；POST仍严格校验。

事件复用`GET /api/runs/{run_id}/events`；评测事实为evaluation.accepted/case_started/case_completed/report_saved/finished及普通Run生命周期。通用Run/queue的execution投影为`{kind:evaluation,workspace_id,evaluation_id}`，不使用snapshot专属字段。刷新读取持久事实，不重复计数。重启将未完成批次标interrupted，已完成案例仍可查询，活动案例中断、剩余未执行；禁止普通Run、Continue和abandon进入控制Session，新测试使用新身份。

### Vue 离线评测请求恢复

Vue提交固定`engineering-v1`任务集目录返回的`suite_sha256`、development/holdout/all及默认`mode=offline`，live需额外规划确认；接收和取消均使用随机Idempotency-Key。网络错误或未明确拒绝的409保留原身份，刷新重试也固定原指纹与划分。明确无副作用拒绝（X-Eventide-Mutation-Outcome: rejected）或403/404/422才允许新请求身份，成功202只代表持久接收，运行结果从详情和报告查询。

详情的queued/running/completed/failed/interrupted与逐题passed分别展示；passed=null、duration_ms=null不显示为false或0。报告接口失败不会抹掉运行详情，下载内容保留schema v2原字段和scripted_mechanism_validation/billing_applicable=false标记。`#/evaluations?evaluation=<id>`可恢复选择，但客户端必须核对详情project_id与当前项目；切项目或选择其他评测时丢弃迟到响应。

### 配置写入的版本与持久回执

Provider PUT、Provider DELETE、模型默认参数 PUT、会话参数 PUT 均要求 `Idempotency-Key` 和正文 `expected_config_hash`（64位小写十六进制）。Provider DELETE 也有 JSON 正文；旧版无正文调用需升级。对应 GET 返回 `config_hash`，摘要绑定当前有效配置，模型参数摘要还绑定 Provider 与会话创建身份。

同操作/键/正文重试返回原成功结果；同键不同正文或未确认请求返回409且不能换键规避。过期摘要、服务忙等明确拒绝返回409并标记 `X-Eventide-Mutation-Outcome: rejected`。未知存储故障返回脱敏503，保留原身份；明确发生在写入前的客户端关闭失败返回503并标记 rejected，刷新后可显式新建恢复操作。成功回执和配置事实同一 SQLite 事务提交；历史回执不代表最新配置，确认后应再次 GET。

Provider 状态新增 `client_status=ready|cleanup_required|restart_required`。关闭旧客户端失败保留旧配置并阻止普通运行，明确发起新的恢复配置操作可重试关闭；无法核实持久配置时要求重启。配置保存不发起模型请求。会话生成配置事实由内部配置控制 Session 保存，绑定会话创建时间，删掉空会话后重建同名会话不会继承旧覆盖。

Vue仅在浏览器会话存储中保存随机请求键、版本和候选摘要，不保存候选密钥。未知结果后要求重填原候选重试；确定冲突需刷新后重新编辑。连接测试和配置修改互相阻止在途覆盖。

### 本地检索诊断与工具证据

已配置本地语义包在Run准备时不可用且存在有效索引时，持久事件`retrieval.degraded`返回固定`reason`诊断码及`fallback=sparse|unavailable`。不会回显底层异常或创建第二事实源。`search_knowledge`结果注明`mode: sparse|dense+sparse|dense+sparse+rerank`，降级注明`Retrieval degraded`；重排分数独立展示，最多20候选/5结果。未启用重排时保留原k=1–10。

本地模型配置目前通过启动配置和Python准备接口提供；索引管理HTTP与页面见后文；未提供模型下载或检索对照HTTP页面。

### 远程检索用量与预算停止

Host检索新增kind=embedding、usage_basis=input_only的调用事实，复用现有Run/Session/项目usage与费用查询，无新增账本。tool.completed.metadata.code=budget_exceeded表示请求派发前预算拒绝，Run随后以token_budget停驻。仅输入调用的output_tokens缺省表示不适用，不能渲染成供应商已报告0；未知输入仍保持unknown。

### 查询改写事件与用量

部署开关 `EVENTIDE_RETRIEVAL_QUERY_REWRITE=1` 允许每根Run及子任务共用一次改写。持久事件 `retrieval.rewrite_claimed`、`retrieval.supplement_requested`、`retrieval.rewrite_finished` 归属根Run，payload含 `requesting_run_id`，查询仅保存hash。完成状态包括completed、rewrite_invalid_or_unchanged、rewrite_index_changed、failed、cancelled、budget_blocked。请求失败也消耗次数，恢复不重放；请求用量以实际调用Run的 `query_rewrite` attempt计入原账本，根汇总不重复计费。

search_knowledge在启用时最多返回5条，附原查询/改写来源；失败及次数耗尽诊断随工具结果返回。改写不会派发返回文本内的工具指令，预算拒绝仍停驻Run。该开关不是默认收益承诺，也不增加另一套HTTP模型调用入口。

### 索引构建控制面

- `GET /api/indexes/configuration`：返回provider/model/algorithm_version/dimension/config_hash，不返回凭据或endpoint原文。服务端模型资源缺失时409。
- `POST /api/projects/{project_id}/indexes`：严格body为`config_hash`、`force=false`、`code_structure=false`；必须本机同源及`Idempotency-Key`。持久接收后202，返回run_id/session_id/project_id/status_url/events_url。同键同内容回放，内容冲突409，配置变化或项目构建繁忙409，队列满429。确定未接收的失败附`X-Eventide-Mutation-Outcome: rejected`，未知保持原身份重试。
- `GET /api/projects/{project_id}/indexes?limit=20&before=<run_id>`：按身份分页，limit最大100。`GET /api/indexes/{run_id}`：运行状态、固定配置/选项、发布证据及错误；`published=null`表示本次无已保存发布证据，不能伪造为0个文件。
- `POST /api/indexes/{run_id}/cancel`：同源及幂等键，等待精确任务清理；重复停止或终态调用返回同一任务现状。取消排队任务不请求模型、不生成索引。

持久`index.accepted`、`index.published`、`index.cancel_requested`沿用Run SSE；没有逐片段伪进度或第二日志源。重启未完成任务为interrupted，普通Continue不适用于索引控制Session。发布证据含generation和files/rebuilt_files/deleted_files/chunks/skipped/duration_ms/structured_files/structure_fallbacks/chunking_fingerprint；代次与统计同事务绑定。远程embedding用量通过既有Run/project成本接口查询，索引终态不重复累计。

### 有界交付内部事实

Python `RuntimeHost.manage_delivery("repair", delivery_id, goal, checks, image?, max_rounds=3)` 只接受1–3轮、1–4个明确验收命令。`show`新增repair（固定条件）、repair_result（停止原因/用量）、repair_status（running/passed/stopped/interrupted）和rounds。repair开始后普通run/verify被拒绝，apply要求修复通过且原有双方checkpoint/patch校验仍有效。

`delivery.repair_started`含control_run_id/deadline/limits；`delivery.round_started/round_finished`含round和候选证据；`delivery.repair_finished`含reason、verified_candidates及usage投影。常见reason为passed、round_limit、no_source_progress、repeated_acceptance_failure、writer_stopped、unknown_tool_outcome、cancelled、delivery_time_limit、delivery_token_limit、delivery_step_limit、repair_failed。终态usage只是派生展示，不能再次入账。API消费者不能把控制Run的completed等同候选验收通过，应读取repair_status/reason。

### Web 交付接收和人工采纳

- `POST /api/projects/{project_id}/deliveries`：`{name}`，1–48个小写字母/数字/连字符，创建独立副本。
- `POST /api/deliveries/{delivery_id}/repair`：`{goal,checks,image?,max_rounds?}`，目标1–20000字符，1–4个最多2000字符的明确验收命令，最多1–3轮（默认3）。固定后不允许改验收条件或重启同一修复流程。
- `POST /api/deliveries/{delivery_id}/apply`：`{patch_sha256}`，必须与当前通过验收的补丁一致。持久接收和执行时均检查，源码漂移仍由原采纳检查拒绝。
- 以上写入严格禁止额外字段，要求本机同源和`Idempotency-Key`；202返回run_id/delivery_id/project_id/status_url/events_url。同键同正文返回原身份，不重放；同键不同正文409。确定未接收的繁忙/冲突409或队列满429附`X-Eventide-Mutation-Outcome: rejected`，未知结果保留原键。
- `GET /api/projects/{project_id}/deliveries`、`GET /api/deliveries/{delivery_id}`：已准备交付、固定修复条件、轮次、独立验收与采纳状态。
- `GET /api/projects/{project_id}/delivery-operations?limit=20&before=<run_id>`：按提交顺序逆序查询，包括准备失败/排队/中断，limit最大100。`GET /api/delivery-operations/{run_id}`返回精确操作状态、接受正文和错误。
- `GET /api/deliveries/{delivery_id}/patch`：返回delivery_id/patch_sha256/patch；没有验收产物或产物变化409。
- `POST /api/delivery-operations/{run_id}/cancel`：同源/幂等键；精确取消并等待清理，重复调用返回现状。排队取消不创建副本、不调用模型。

持久`delivery.operation_accepted`/`delivery.cancel_requested`与既有交付事实沿用Run SSE。父控制Run与Writer共享根名额，Writer为子Run；usage汇总通过既有include_children口径，不能再加repair_result.usage。重启停驻，普通Agent Continue不能恢复控制操作。`completed`不等于`repair_status=passed`。


### 脱敏 Trace 读取

`GET /api/runs/{run_id}/trace`返回schema_version/source/spans。span包含name、trace_id/span_id/parent_span_id、纳秒时间范围、status、固定allowlist属性与links；不包含原Run/Session身份、提示、任意模型/工具名称、参数/结果、路径或凭据。模型用量沿用独占attempt口径，不能把父子区间的时长或计数直接累加成总成本。

`?download=true`返回相同JSON并设置attachment文件名与Cache-Control:no-store。不存在Run为404，超过64个相关Run或canonical关系无效为409。只读取所选Run及子任务；祖先和Continue来源用于关系定位，不额外展示无关运行。缺失来源保留unobserved语义。读取和下载不修改事件、不启动全局TracerProvider，也不向OTLP发送数据；显式外部导出仍由Python开发接口负责。

### Session 元数据管理与重试

Session 查询增加 `metadata_hash` 和 `managed`。PATCH/DELETE
`/api/sessions/{id}` 都要求本机同源、`Idempotency-Key`，以及 JSON 中的
`expected_metadata_hash`。PATCH 只接收严格类型的 title/archived；DELETE
仅删除无 Run、无 Session 事件历史且不忙的普通会话。服务控制会话拒绝修改。
版本过期返回带 rejected 标记的 409；同键不同内容拒绝，同键成功重试返回原回执。
元数据、管理审计和成功回执在同一 SQLite 事务提交，响应丢失不会重复执行。
未知结果保留原身份，不自动生成新键。哈希包含创建时间，已删除后同名重建的
会话不能被旧删除请求再次删除。

Vue `#/sessions?session=<id>` 承接改名、归档、恢复和确认删除空会话。
编辑固定选中时的版本，冲突后须显式刷新；未知请求跨刷新保留原正文与身份。
归档不取消后台 Run，切换项目不接受旧列表或旧修改请求的迟到响应。

### 工作副本控制面

- GET `/api/projects/{id}/worktrees` 返回 catalog_hash 和受管记录，包括
  history_protected（有会话、运行历史或正在使用，禁止清理）。
- POST 同一路径要求 Idempotency-Key 与严格正文：
  action=create/keep/remove、name、ref（默认 HEAD）、expected_catalog_hash。
  持久接收返回 202、run_id/session_id、status_url/events_url。
- GET `/api/projects/{id}/worktree-operations?limit=20&before=<run_id>` 返回
  操作历史，GET `/api/worktree-operations/{run_id}` 查询精确状态。
- POST `/api/worktree-operations/{run_id}/cancel` 要求幂等键，
  同一停止身份只记一次请求；重复停止终态返回现状。

写入要求本机同源。版本过期、名称冲突、无效引用拒绝接收；满队列429。
创建接收时固定完整 commit，keep/remove 执行前在源码/Git 锁内再次复核目录
版本；目录版本含记录文件身份，名称复用不能满足旧请求。清理继续执行原有
干净检查、Session/Run引用检查，不提供force选项。清理保留Git分支。

Run execution.kind=worktree_control，含 operation_id/workspace_id，使用同一
持久队列、Run/SSE/精确取消。服务控制会话不能作为普通Agent对话或Continue。
停止派发后的Git操作会等待结果；已完成的操作仍记录completed，不能解读为撤销。
进程退出的未完成操作标为interrupted，不自动重放。页面通过目录与历史查看结果。

### 旧版历史预检与确认导入

- POST /api/projects/{id}/history-imports/preview：严格正文 {source: 本机数据库路径}。
- POST /api/projects/{id}/history-imports：严格正文
  {preview_id, archive_sha256, confirmed:true}；confirmed必须是真正布尔true。
- 两者要求本机同源及Idempotency-Key，持久接收返回202及
  run_id/session_id/project_id/status_url/events_url。同键不同内容拒绝。
- GET /api/projects/{id}/history-imports?limit=20&before={run_id}：按身份分页的操作历史。
- GET /api/history-imports/{run_id}：预检计数/大小/SHA256或导入结果、状态、错误。
  预检的imported表示已导入；成功结果包含first_session_id供历史页定位。
- POST /api/history-imports/{run_id}/cancel：精确取消，要求幂等键。

预检是只读任务，固定脱敏档案，不恢复Provider配置；原路径只在接收进程内存
保有，事件仅保存请求摘要。确认必须匹配项目、已完成预检和hash；派发时再检查
受管档案的路径、大小与hash。导入记录使用预检命名空间映射身份，同一预检不能
重复导入，空档案也遵守该规则。记录与控制成功回执/终态同事务提交；错误整批回滚。

run.execution.kind=history_import并带operation_id/workspace_id；共享4根/20等待
调度。排队取消不读取源库，已派发操作停止时等待真实线程收尾；已提交的导入保持
completed，停止不表示撤销。重启不自动重放。源文件在预检后变化不影响固定档案。

Session查询增加legacy_imported；导入会话归档且作为只读控制历史，Run/Continue
拒绝，不借用旧事件作为执行或审批依据。GET
/api/sessions/{id}/legacy-messages?limit=50&before={session_seq} 仅对导入会话开放，
返回seq/role/content；limit为1–100，每页升序，before排除游标自身。
普通会话或不存在的身份404，非法分页422。历史页以纯文本展示消息，旧汇总用量
保留在legacy.run_summary，原事件保留在legacy.event，不加入现代逐调用账本。

POST /api/sessions 要求 Idempotency-Key（1–128可见ASCII），body严格只接受workspace_id/working_directory。缺键422，同键同内容回放201和原session_id，不同内容409；缺失body与空对象均规范化为默认字段。会话及成功回执原子提交，结果未知503或在途409保留原键；明确拒绝附X-Eventide-Mutation-Outcome: rejected。删除原会话后旧请求仍返回原身份，不重新创建。Vue均保留未知请求身份，Python内部创建接口不受HTTP头约束。

POST /api/workspaces 与 DELETE /api/workspaces/{id} 强制Idempotency-Key，缺失422，同键异内容409。注册body严格为path；注册201和移除200原结果持久回放，先检查回执再检查目标存在性，因此已移除目标仍可回放。路径重新注册得到新workspace_id，旧移除不会影响新记录。登记/关系变更与成功回执原子提交；未知503/未标记409保留键，明确拒绝带X-Eventide-Mutation-Outcome: rejected。工作区详情新增startup_workspace布尔值，启动工作区禁止移除409；已有会话/执行引用/副本/活跃操作也拒绝。磁盘目录不删除。

用量投影可返回authority_references（kind=evaluation、run_id、case_id、case_index）。这是隔离评测执行日志对主服务调用账本的引用，不是一笔新消费；尚未解析引用时complete=false，不能把空calls解释为免费。现有公开服务Run仍直接展示主账本事实；评测Web默认offline，live按下述规划确认契约接收。

EvaluationDetail.mode现在为offline|live，measurement_kind为scripted_mechanism_validation|provider_experiment，billing_applicable为布尔值，供受控Python实验历史查询。POST默认offline；live须按规划确认契约接收。批次遇共享预算拒绝或模型故障时主Run interrupted、报告status=aborted；remaining仍保留，不能把批次结束等同全题通过。

### 真实评测规划与确认

`GET /api/projects/{project_id}/evaluations/quote?split=development|holdout|all`只读返回quote_hash、任务指纹/数量、provider/model/generation、planning_tokens、estimate_method、assumptions、limits和按币种分列的estimates。每币种包含known_amount、complete、unknown_categories及固定price_version的lines；缺价格返回空estimates且complete=false。规划不是实测或账单上限。

创建评测支持mode=offline|live（默认offline）。live额外要求confirmed=true和64位quote_hash；模型配置、任务、有效价格或划分变化导致409 rejected。首次持久接收冻结规划；同键同正文重放在重新核对规划之前返回原身份。offline仍使用原四字段请求指纹，不因新增默认字段破坏旧请求恢复。目录用supported_modes/default_mode描述支持能力；measurement_kind/billing_applicable由具体批次决定，不能将目录解释为免费声明。

Vue显式读取估算并确认后才可发起live；未知结果保留原确认和请求键，刷新不会自动发请求。live报告为provider_experiment/billing_applicable=true，失败验收保留，实际用量从主控制Run查询；本地case账本引用不重复计费。

### 附件写入请求身份

`POST /api/sessions/{id}/attachments`与`DELETE /api/sessions/{id}/attachments/{attachment_id}`要求本机来源及Idempotency-Key。无键422；同操作/键不同内容409，同键成功回放原公开结果；回放上传不会复活已删除附件。上传输入校验在文件操作前，错误422。文件发布/删除或回执失败503且不标rejected，原键重试409未知，须检查列表，不自动重做。已引用附件删除409 rejected。Worker及控制Session不能上传。正文仅作摘要绑定，不落入事件原文。

### 运行决定的持久请求身份

Run、Continue、cancel、approvals、abandon全部要求本机来源和Idempotency-Key，无键422且不创建Session/Run。Run及Continue继续通过admission回放原接收；停止/审批/放弃恢复使用控制Session的持久回执，成功回放在检查当前终态之前进行。同键不同目标或审批决定409，操作或回执结果未知503并保留原键，不能通过自动换键重做。

`POST /api/sessions/{id}/abandon`严格正文为`{expected_run_id: <页面看到的中断Run>}`；接收前及Host执行锁内再次检查，过期身份409，不处理后来产生的中断任务。审批approved必须为JSON布尔值，拒绝字符串/数值；审批结果仅针对指定Run/approval。重复成功回放只确认原决定，不表示最新Run仍处于原状态，应再次查询。

生成参数capabilities增加向后兼容字段：`thinking_modes`（默认enabled/disabled，仍受thinking开关限制）、`top_p_min`（默认0）、`sampling_precision`（默认null）、`max_output_tokens`（默认null）。已确认GLM-5.3-Flash Chat端点返回仅enabled、top_p下限0.01、两位小数、输出上限131072；非法值接收前422，不静默修正。已有存量能力声明不补造新约束。

Run／Continue接收补充：已经存在canonical接收身份时，后续附件缓存或派发故障仍返回202和该run_id/session_id；accepted不表示执行成功，需查询Run终态。未创建Run的已知前置拒绝附`X-Eventide-Mutation-Outcome: rejected`，未分类故障返回503、保留原键。客户端不得将未标记409当作允许重发的新意图。Skill导入预检错误可明确拒绝；进入实际发布或成功回执阶段后失败返回503，不附rejected，原键后续返回409未知且不重新发布。

### 摘要节流诊断

`context.trimmed`可附带`summary_skipped`，取值为`same_completed_prefix`、`summary_cooldown`、`insufficient_completed_content`或`newer_completed_prefix_already_claimed`。它表示未新建逻辑摘要请求，不代表Token为零或预算已结算；现有请求内的有限重试仍逐尝试记账。Session级`context.summary_claimed`只保存摘要资格依据，不绑定或伪造模型attempt，也不进入消息投影。节流后仍超出硬上下文上限时，沿用`context_overflow`失败，不派发主模型请求；错误说明中包含跳过原因。

### 重复失败工具的停驻

`run.interrupted.reason`新增`repeated_tool_failure`。对应`tool.failure_loop_detected`保存`threshold=3`、`matching_failure_window=3`、`tool`、`failure_fingerprint`、`checkpoint_hash`、`calls[{run_id,call_id}]`和`continued_failure_chain`。这些字段描述三次已发生结果和源码证据，不表示工具缓存命中或验收通过。`tool.completed`可附带`failure_fingerprint`；真实用量继续只从model.attempt事实派生。

Executor捕获真实异常时，metadata可带`exception_type`类名。新窗口只接受实际异常、timeout或非零exit_code依据；仅有字符串`Error:`前缀的旧execution_failed不纳入，保留原is_error呈现和步数控制。

同批剩余调用以`tool.abandoned(reason=repeated_tool_failure,dispatched=false)`补齐，不产生`tool.prepared`或执行。Continue仍须通过现有恢复检查和显式请求，保留原失败窗口；若再次同样失败，可立即再次停驻。没有可验证源码checkpoint时不生成此停驻理由，沿用原步数上限。预算拒绝、未知结果、参数纠错和取消不改变为该理由。


受控Python检索实验没有新增HTTP写入口。已有Run/Session/队列只读投影可显示 `execution={kind:"python_experiment",purpose:"retrieval_benchmark",operation_id,workspace_id}`；Session为managed且归档。普通Run、Continue和abandon拒绝此身份，停止仍使用精确Run停止接口并等待实际清理。事件包含control.operation_accepted、retrieval.benchmark_result和原Run/模型尝试事实；重启停驻且不能通过HTTP自动重放实验。
