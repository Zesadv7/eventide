# 前端接口契约（Web UI ↔ Runtime API）

本文是 Web 前端依赖的全部 HTTP 接口契约与实现状态。前端按此开发并对缺失接口优雅降级；后端按此补齐。实现状态：`已实现` / `预留（待后端）`。

0.5.0 的 `/openapi.json` 使用 `info.version="0.5.0"`，前端生成契约及同源产物随版本同步。版本整合包含偏好记忆、系统目录选择与可选沙箱的下述已实现接口；发行元数据更新不改变这些接口的字段、来源/幂等或权限语义。2026-10-21 macOS 原生目录实选返回 HTTP200/selected 与绝对路径，实际关闭返回 cancelled；两者不自动注册项目。

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

MCP明确返回的`isError`保存为`tool.completed.payload.is_error`及`metadata.mcp_is_error`；为true时`metadata.code=execution_failed`，正文不增加Error前缀。调用中超时、断连或取消导致无法确认结果时只产生`tool.outcome_unknown`，其error保留经脱敏的异常分类和原因，终态reason为`unknown_tool_outcome`；无自动重试或补偿。该范围不包含调用前的发现失败、参数/权限拒绝。

写请求的Origin必须匹配本机服务源，cross-site/null/异常端口和DNS-rebinding来源拒绝；本机非浏览器客户端可省略Origin。没有放开CORS，开发前端应使用同源代理。

下文“仅回环／仅本机”的管理权限默认仍只接受回环连接。容器部署可通过启动配置 `EVENTIDE_LOCAL_PROXY_PEERS` 显式追加精确 IP；这些额外连接必须同时携带匹配服务端回环 Host 的 Origin，缺失／不匹配返回403，不能利用伪造转发头获得权限。配置在应用创建时固定，不属于模型或运行配置 API。即使未来只读管理路由使用此守卫，额外连接也需满足同样来源检查；现有普通GET契约不变。宿主仅发布回环端口，服务入口禁用代理头解析，不提供公网认证。

Run与Continue要求`Idempotency-Key`（1–128个可见ASCII字符）。同Session/操作/键确定一个持久Run身份，内容hash写入admission。重试相同内容返回原202接收凭据，不新增消息、调用或turn；相同键不同内容409。接收尚未持久化的竞争请求可返回busy，客户端保留相同键稍后重试。幂等接收不等于自动重放工具，未知结果仍停驻。Skills 导入和配置的幂等契约见下文；Delivery等长任务使用下述专用持久控制面。

### 模型生成参数 —— 后端及 Vue 已实现

- `GET /api/generation/defaults?model=...` 返回 `model_identity/defaults/session/capabilities/config_hash`，身份绑定Provider、endpoint和模型。
- `PUT /api/generation/defaults` 接受 `{model, generation, capabilities?, expected_config_hash}`；显式能力声明标为declared，不冒充已验证。
- `GET/PUT /api/sessions/{id}/generation` 读取/替换该模型下的会话覆盖，PUT不允许重新声明模型能力。
- `POST /api/sessions/{id}/runs` 新增 `generation`：temperature、top_p、max_output_tokens、reasoning_effort、thinking、response_format。省略继承，null恢复默认，0有效；额外字段/非有限值/范围错误422。
- 写配置仅本机来源；支持情况未知时拒绝显式采样参数。接收时冻结，Continue相同接入继承；已有调用不受后续设置影响。尚无自动运行探测或按任务预设。

`response_format`取text或json_object；`capabilities.response_formats`默认空，只有已确认的精确GLM-5.3-Flash Chat或显式声明的Chat能力接受，其他接口/未知能力在接收前422。省略不发SDK字段，null恢复未设置。json_object为最终输出契约：调查请求phase=tools保留工具并不下发原生response_format；首次完整无工具响应标draft_for=json_object，下一步统一一次phase=finalization，以空工具目录和原生JSON生成最终对象。草稿本身合法也不能省略该步骤。两阶段共享原Run步数/模型请求/预算，步数末轮的草稿停驻后由显式Continue恢复，不额外绕过上限；普通新Run不继承阶段。

model.request/response和实际attempt记录phase，request另含真实generation与工具目录hash/count；冻结admission.generation表示最终契约，不能据此宣称全程原生JSON。最终化的工具幻觉只记录abandoned配对并failed，不执行；断流、取消和未知沿原规则保存事实。最终化最多一次真实尝试；完整终态保存原model.response及用量后才验证单个JSON对象（包括非有限数值），非法时failed且错误含response_format=json_object，不剥围栏、不纠错、不重发。该契约不保证事实、字段或引用通过验收。

### 请求用量账本 —— 后端及 Vue 已实现，费用控制面见 U2/U3

`GET /api/runs/{id}/usage`、`GET /api/sessions/{id}/usage` 返回独立模型尝试的 `calls/reported_totals/estimated_totals/unknown_attempts/incomplete_attempts/complete`。默认包含持久parent_run_id关联的子Run；`include_children=false`只取所选自身。Session汇总覆盖全部历史，不受Run列表分页限制；每个真实调用只计一次，不重复加入终态或worker汇总。

无usage是unknown，不是零；缓存与推理Token为细项，不能再加到总量。账本暂不报价，原`usage`字段保留兼容；需要完整性判断时使用新账本。历史响应只能回退统计已观察响应，complete保持false。取消和失败请求无返回usage时保留未知。

calls另带`usage_basis: input_output|input_only`；旧调用默认input_output。仅输入型embedding事实采用input_only，reported输入完整即可usage_complete=true，output_tokens保持缺省，不能渲染为已报告0。费用和预算从首次预留的相同口径派生，普通生成缺输出仍为不完整。远程embedding查询与索引批次已按ADR-072/075接入Host模型槽、预算及唯一账本。

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
| `GET /api/runs/{id}/events`（SSE） | 事件流（非负 `after` / `Last-Event-ID` 游标取较大值；prepared/completed 以 `tool.request`/`tool.result` 别名推送） |
| `GET /api/runs/{id}/export` | NDJSON 审计导出 |
| `POST /api/runs/{id}/approvals/{approval_id}` | 审批决定 |
| `POST /api/runs/{id}/supplements` | 运行中补充接收（本机来源 + Idempotency-Key；同键同正文重放原收据、同键不同正文 409；正文 1–8,000 字符，`body_version` 1–9） |
| `GET /api/runs/{id}/supplements` | 补充投影（pending/consumed/late 及消费 step；迟到补充不自动续跑） |
| `GET /api/runs/{id}/calls/{call_id}/command-log` | 完整命令日志分页回读（本机 Shell/Skill；`stream=stdout|stderr`、`offset≥0`、`limit≤12000` 字符；失败保留 200+availability，未知 Run/call 404） |
| `POST /api/sessions/{id}/side-questions` | MK-17 旁枝创建（本机来源 + Idempotency-Key；`question` 1–8,000 字符，`source_run_id` 可省略时取该 Session 最新 Run；202 返回 side Run 身份、快照与高水位） |
| `GET /api/runs/{id}/side-questions` | 该 Run 的旁枝列表（含答案指纹、回传状态与补充收据；创建事实为 Session 级，终态 Run 仍可查询） |
| `GET /api/side-questions/{side_question_id}` | 单条旁枝投影（side Run 状态、answer、fingerprint、returned；未知或畸形 id 一律 404） |
| `POST /api/side-questions/{side_question_id}/return` | 显式回传答案（本机来源 + Idempotency-Key；`answer_fingerprint` 必填64位小写十六进制，与所见版本不一致或答案未完成 409；同一旁枝重复回传重放原收据；主任务终态时 status=late 不续跑；正文超 8,000 字符显式标注 `body_truncated`） |
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

### `GET /api/runtime/contract` —— 已实现（MK-00 契约切片）

显式 Web API 契约版本与真实能力发现；`schema_version` 只读自当前运行库（`schema_migrations` 的实际版本，当前为 6），运行库不可读时返回 503，不猜测。`capabilities` 只列已实现能力，新增或退役条目必须与实现同一提交；未实现的 MK 计划项不得出现。

```json
{
  "contract_version": 1,
  "schema_version": 6,
  "runtime_version": "0.5.0",
  "capabilities": ["projects", "sessions", "runs", "approvals", "attachments", "worktrees",
    "delivery", "file_undo", "skills", "mcp", "retrieval", "memory", "usage_ledger",
    "evaluations", "history_import", "docker_sandbox", "workbench"]
}
```

前端启动时先读取该接口：`contract_version` 与前端构建支持的版本精确相等才挂载工作台；不匹配显示"前后端版本不兼容"屏障页（含服务端契约/数据/程序版本与升级指引），连接失败或服务错误显示"暂时无法确认服务兼容性"并区分网络故障与版本问题，两者都提供"重新检查"入口；屏障期间不渲染任何写入口。前端常量为 `src/api/contract.ts` 的 `SUPPORTED_CONTRACT_VERSION`，破坏性 API 变更必须前后端同提交升版本。

### Run summary 增加 `usage` —— 已实现

`GET /api/sessions/{id}/runs` 与 `latest_run` 每条 Run 增加 `usage: {"input_tokens": n, "output_tokens": n}`，供用量分区的会话累计。

### 完整命令日志分页 —— 已实现（MK-05 切片 1）

`GET /api/runs/{run_id}/calls/{call_id}/command-log?stream=stdout|stderr&offset=0&limit=4000`：本机原生 Shell（`bash`）与 Skill 脚本（`run_skill_script`）调用的完整脱敏输出回读。日志在执行时绑定 Run/call/副本身份保存，先脱敏再落盘，单流 1 MiB、每 Run 24 MiB、全局 256 MiB 配额，终态后保留 7 天。响应为 `available` 页：`start/end/total_chars`（Unicode 字符分页）、`stored_bytes/received_bytes`（`received>stored` 即明确截断）、`capture_state`（complete/interrupted/quota_exceeded/write_failed/not_started/redaction_unavailable）及 `encoding_loss`；配额只标记省略，不改变命令真实退出码。不可用时返回 `availability`（expired/missing/corrupt/not_saved/not_recorded），不假装空输出；未知 Run/call/流为 404，`limit>12000` 或非法流为 422。日志正文不能从事件元数据重建，文件为独立脱敏备份，读取校验 recorded 摘要；容器/沙箱执行未接入此采集，也不降级为宿主采集。工具结果 metadata `command_log` 携带 `artifact_id/run_id/call_id/streams` 供工作台绑定。

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

`reason=token_budget`可来自唯一attempt已报告用量软限，或共享预算拒绝（后者查看budget.blocked）。软限run_token_limit默认0关闭，累计摘要、辅助及持久子Run；一次返回可越过观测上限。Continue仅获得新Run额度，交付、项目、UTC日累计及未知预留不重置。没有usage的失败保持未知；页面读取完整账本及预算事实，不把终态usage当总账。

### 受控只读子任务 —— 已实现（显式开启）

Workspace capabilities 增加 readonly_workers（enabled/roles/max_concurrency/max_depth/max_tasks_per_run）。父 Run 的 worker.started/completed 事件关联 child_run_id，子 Run 查询和 history 返回 parent_run_id/worker_role/snapshot_id。子Session默认归档且不能接收普通Run、Continue或附件。父usage含子消费，子usage仅自身；跨父子汇总需按exclusive模型事件计算。

新子任务的canonical `run.admitted.delegation`含fingerprint和parent_call_id，不含额外目标正文。相同Run/显式Continue链的重复角色、精确目标和源码快照（包括批内重复）使整个delegate工具调用返回错误，零子任务派发；已有任务返回`existing_calls`中的父run_id/call_id及child_run_id供查询，未缓存回答。该错误是工具结果，不更改HTTP状态码或工具schema；普通新Run、其他Session与源码/角色/目标变化不共享去重。旧无指纹接收事实不补造精度，仍保留原四子任务上限。

### 交付、执行配置与未知结果 —— 已实现

交付由下述持久HTTP与Vue有界修复控制面提供准备、修复、查询、补丁核对和人工采纳；手动run/verify保留Python开发接口。原Run/Session/SSE/export可读取Writer与control Run证据；delivery事实与patch artifact hash存同一Event Log。Writer的execution_config在后续Run/Continue生效；图片/PDF契约不变。control Session归档且不能用于普通模型执行。

Run详情的`tools[call_id].status`将该调用投影为`outcome_unknown`，保留prepared的脱敏参数和unknown事件的error/归属；只计一次调用，刷新/重启重建不产生新的完成事实。Web卡片显示“结果待确认”，优先于通用is_error标记。

新增canonical tool.outcome_unknown与终态reason=unknown_tool_outcome；它保留未确认调用，不伪装成tool.completed，相关Workspace阻止普通Run、Continue和交付写入。前端显示“工具结果未确认”，不能将其展示为已停止或允许继续。未知状态不因abandon或重启清除。导出仍包含完整canonical证据，默认脱敏Trace只派生安全元数据。


## Vue 客户端兼容规则

Vue HTTP 客户端规范化列表/详情返回的 Run `id` 为内部 `run_id`；202 接收凭据仍直接使用 `run_id`。Run 列表是最近一页按追加顺序排列，最新项位于末尾。工具待审批期间，`/messages` 可能因工具历史尚未配对返回 409；客户端暂留上次可见消息，继续查询 Run/Session，不能因此隐藏审批或停止按钮。

历史页 `#/history?session=<session_id>&run=<run_id>` 使用 `GET /api/projects/{id}/sessions?include_archived=true` 确认当前项目归属，再调用既有 Session Run 列表、精确 Run 详情及 export。列表请求固定 `limit=100`；下一页 `before` 使用当前已加载最旧 Run 的身份，服务端按追加 rowid 排序且排除游标本身，不使用时间戳翻页。每页仍按旧到新返回，Vue 反向展示；`hasOlder` 仅由满页推断，没有伪造总数。精确详情验证 `run_id` 和 `session_id` 后才显示导出入口；URL 恢复不要求先拉取全部中间页。JSONL 返回 canonical 事件，工具详情投影不是独立 trace 导出。本页没有写请求，不更改当前执行会话；会话管理与独立 trace 已分别由专页及历史详情承接，历史导入已由独立页面及受控预检/确认API承接。

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

`GET /workbench/` 提供Python包内Vue页面；Hash路由无需后端路径回退，HTTP/SSE仍走同源`/api`。静态资源缺失404；整个构建缺失时工作台返回503 JSON错误，healthz/API仍可使用。根地址307跳转/workbench/，旧/assets/*为404。`/workbench/manifest.json` 仅含构建来源指纹与文件校验清单，不含运行状态或凭据。


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
- `POST /api/projects/{project_id}/evaluations` → 202：`evaluation_id,run_id,session_id,project_id,status=accepted,status_url,events_url`。严格请求为`{suite_id:engineering-v1,suite_sha256:<catalog hash>,split:development|holdout|all,mode:offline,preset:none|engineering}`；split默认development，mode默认offline，preset默认none。live额外要求quote_hash与confirmed=true；未知字段422，禁止路径、脚本、模型或凭据输入。同键同内容返回原202和身份，包括重启/终态；同键异内容409。suite过期、同项目busy为409，全局队列满429，确定未接收的拒绝带`X-Eventide-Mutation-Outcome: rejected`。不得在内容冲突或结果未知时自行换键。
- `GET /api/projects/{project_id}/evaluations?limit=20&before=<run_id>` → 最新在前的详情数组，limit范围1–100。
- `GET /api/evaluations/{evaluation_id}` → 身份、suite_id/suite_version/suite_sha256、split/mode、机制标记、`status`（queued/running/completed/failed/interrupted）、`dispatch_state`（queued/starting/running/finishing/null）、queue_position、total/attempted/passed/failed/remaining、current_case、cases、report_available/report_url/events_url/error。cases逐项`id,category,split,status,passed(bool|null),reasons,duration_ms`；未执行passed为null。批次completed表示全部案例执行完，失败数另计。
- `POST /api/evaluations/{evaluation_id}/cancel` → 当前详情；排队/运行取消均等待实际收尾并变为interrupted，终态重复停止无副作用。活动取消请求按键摘要记入原Run事实；已完成终态不追加事实、不重启。通用Run取消端点亦有效。
- `GET /api/evaluations/{evaluation_id}/report` → B2 schema_version=2完整JSON（含status/passed/total/attempted/remaining/results/provenance等），验证文件名、目录身份与不可变SHA256。无报告404，已有证据文件丢失/变更/越界409。此为只读GET，容器同源浏览器不需Origin；POST仍严格校验。

事件复用`GET /api/runs/{run_id}/events`；评测事实为evaluation.accepted/case_started/case_completed/report_saved/finished及普通Run生命周期。通用Run/queue的execution投影为`{kind:evaluation,workspace_id,evaluation_id}`，不使用snapshot专属字段。刷新读取持久事实，不重复计数。重启将未完成批次标interrupted，已完成案例仍可查询，活动案例中断、剩余未执行；禁止普通Run、Continue和abandon进入控制Session，新测试使用新身份。

### Vue 评测请求恢复

Vue提交固定`engineering-v1`任务集目录返回的`suite_sha256`、development/holdout/all及默认`mode=offline`，live需额外规划确认；接收和取消均使用随机Idempotency-Key。网络错误或未明确拒绝的409保留原身份，刷新重试也固定原指纹与划分。明确无副作用拒绝（X-Eventide-Mutation-Outcome: rejected）或403/404/422才允许新请求身份，成功202只代表持久接收，运行结果从详情和报告查询。

详情的queued/running/completed/failed/interrupted与逐题passed分别展示；passed=null、duration_ms=null不显示为false或0。报告接口失败不会抹掉运行详情，下载保留schema v2原字段及该批次实际的measurement_kind/billing_applicable，不能把真实live标为scripted。`#/evaluations?evaluation=<id>`可恢复选择，但客户端必须核对详情project_id与当前项目；切项目或选择其他评测时丢弃迟到响应。

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

检索升级的前端请求约定：Run 与交付修复可传 `knowledge_index=existing|prepare`，页面默认 existing 时省略字段，保留旧请求指纹；显式勾选发送 prepare。未知接收重试沿用原 body。Run 投影的 `retrieval` 与持久 `retrieval.status` 描述实际副本和准备结果，缺失历史字段显示“未记录”；SSE 使用既有持久游标并触发 Run 查询，不累计临时预览。

`GET /api/workspaces/{workspace_id}/index-status` 提供所选副本状态，页面不得将 available 描述为全仓源码最新。构建可选 `workspace_id`，省略仍指项目源目录；构建回执/详情携带目标身份。前端只提供同项目已注册副本的选择，服务端仍负责权限与归属校验。工具记录的 `metadata.retrieval` 展示实际副本、索引代次、配置指纹及 hits 的路径、索引行号、内容哈希与 freshness；未记录时不补造值。

- `GET /api/indexes/configuration`：返回provider/model/algorithm_version/dimension/config_hash，不返回凭据或endpoint原文。服务端模型资源缺失时409。
- `POST /api/projects/{project_id}/indexes`：严格body为`config_hash`、`force=false`、`code_structure=false`、`source_scope=code|all`（默认code）及可选同项目`workspace_id`；必须本机同源及`Idempotency-Key`。持久接收后202，返回run_id/session_id/project_id/workspace_id/status_url/events_url。同键同内容回放，内容冲突409，配置变化或项目构建繁忙409，队列满429。确定未接收的失败附`X-Eventide-Mutation-Outcome: rejected`，未知保持原身份重试。
- `GET /api/projects/{project_id}/indexes?limit=20&before=<run_id>`：按身份分页，limit最大100。`GET /api/indexes/{run_id}`：运行状态、固定配置/选项、发布证据及错误；`published=null`表示本次无已保存发布证据，不能伪造为0个文件。
- `POST /api/indexes/{run_id}/cancel`：同源及幂等键，等待精确任务清理；重复停止或终态调用返回同一任务现状。取消排队任务不请求模型、不生成索引。

持久`index.accepted`、`index.published`、`index.cancel_requested`沿用Run SSE；没有逐片段伪进度或第二日志源。重启未完成任务为interrupted，普通Continue不适用于索引控制Session。发布证据含generation和files/rebuilt_files/deleted_files/chunks/skipped/duration_ms/structured_files/structure_fallbacks/chunking_fingerprint；代次与统计同事务绑定。远程embedding用量通过既有Run/project成本接口查询，索引终态不重复累计。

### 有界交付内部事实

Python `RuntimeHost.manage_delivery("repair", delivery_id, goal, checks, image?, max_rounds=3, observed_token_limit=60000)` 只接受1–3轮、1–4个明确验收命令；`observed_token_limit`为1–1,000,000的严格整数，默认60,000，不支持0禁用。`show`新增repair（固定条件）、repair_result（停止原因/用量）、repair_status（running/passed/stopped/interrupted）和rounds。repair开始后普通run/verify被拒绝，apply要求修复通过且原有双方checkpoint/patch校验仍有效。

`delivery.repair_started`含control_run_id/deadline/limits；`delivery.round_started/round_finished`含round和候选证据；`delivery.repair_finished`含reason、verified_candidates及usage投影。常见reason为passed、round_limit、no_source_progress、repeated_acceptance_failure、writer_stopped、unknown_tool_outcome、cancelled、delivery_time_limit、delivery_token_limit、delivery_step_limit、repair_failed。终态usage只是派生展示，不能再次入账。API消费者不能把控制Run的completed等同候选验收通过，应读取repair_status/reason。

### Web 交付接收和人工采纳

- `POST /api/projects/{project_id}/deliveries`：`{name}`，1–48个小写字母/数字/连字符，创建独立副本。
- `POST /api/deliveries/{delivery_id}/repair`：`{goal,checks,image?,max_rounds?,observed_token_limit?,knowledge_index?,code_structure?,source_scope?}`，目标1–20000字符，1–4个最多2000字符的明确验收命令，最多1–3轮（默认3）；已报告Token软限为1–1,000,000的严格整数，默认60,000。接收后冻结到原`limits.observed_tokens`，36次模型尝试及900秒保持固定。固定后不允许改验收条件、额度或重启同一修复流程。
- `POST /api/deliveries/{delivery_id}/apply`：`{patch_sha256}`，必须与当前通过验收的补丁一致。持久接收和执行时均检查，源码漂移仍由原采纳检查拒绝。
- 以上写入严格禁止额外字段，要求本机同源和`Idempotency-Key`；202返回run_id/delivery_id/project_id/status_url/events_url。同键同正文返回原身份，不重放；同键不同正文409。确定未接收的繁忙/冲突409或队列满429附`X-Eventide-Mutation-Outcome: rejected`，未知结果保留原键。
- `GET /api/projects/{project_id}/deliveries`、`GET /api/deliveries/{delivery_id}`：已准备交付、固定修复条件、轮次、独立验收与采纳状态。
- `GET /api/projects/{project_id}/delivery-operations?limit=20&before=<run_id>`：按提交顺序逆序查询，包括准备失败/排队/中断，limit最大100。`GET /api/delivery-operations/{run_id}`返回精确操作状态、接受正文和错误。
- `GET /api/deliveries/{delivery_id}/patch`：返回delivery_id/patch_sha256/patch；没有验收产物或产物变化409。
- `POST /api/delivery-operations/{run_id}/cancel`：同源/幂等键；精确取消并等待清理，重复调用返回现状。排队取消不创建副本、不调用模型。

持久`delivery.operation_accepted`/`delivery.cancel_requested`与既有交付事实沿用Run SSE。父控制Run与Writer共享根名额，Writer为子Run；usage汇总通过既有include_children口径，不能再加repair_result.usage。重启停驻，普通Agent Continue不能恢复控制操作。`completed`不等于`repair_status=passed`。

兼容既有幂等请求：新增`observed_token_limit`省略或显式为60,000时，不加入请求指纹正文，也不额外写入accepted事实；其余既有默认字段保持原样。旧accepted缺少此字段时按60,000执行。同键更换为非默认额度返回409；非默认额度从accepted事实派发，排队及重启不改写它。


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

`GET /api/projects/{project_id}/evaluations/quote?split=development|holdout|all`只读返回quote_hash、任务指纹/数量、provider/model/generation、fact_response_format、planning_tokens、estimate_method、assumptions、limits和按币种分列的estimates。fact_response_format为json_object或null，仅已声明支持的Chat事实任务启用；随规划固定到evaluation.accepted及报告provenance，各次实际参数仍记录在原attempt账本。修复及辅助调用不自动继承，offline不冒造能力。每币种包含known_amount、complete、unknown_categories及固定price_version的lines；缺价格返回空estimates且complete=false。规划不是实测或账单上限。

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


### 评测 Skill preset

quote查询新增可选`preset=none|engineering`，默认none。返回profile为
`{preset,sha256,skills:[{name,version,manifest}]}`，manifest沿用Skill资源路径、大小、
SHA-256清单。engineering严格包含code-review、fault-localization、test-repair；
profile参与quote_hash，确认后变更preset或内容需重新读取规划。quote保持只读。

创建正文采用相同可选preset，未知值422。显式none在幂等正文规范化中省略，保持
历史省略字段请求的身份；engineering是不同意图，同键改变返回409。旧已接收记录
没有profile时按none读取，重试原成功回执不重新核对当前包。详情新增preset及profile，
报告provenance.profile记录固定输入；逐题evidence.runs[].skill_snapshot记录选中目录，
loaded_skills仅来自实际skill.loaded事件，可以为空。profile不把正文放入HTTP规划，
也不添加工具调用、消费或完成证据。队列等待和刷新不改变固定内容。

Python可选引用提取只增加retrieval.answer_result可读事件，不新增产品请求路由。结果报告的result_missing区分已开始却缺少结果事实，unattempted仅指确认未开始；权威读取覆盖未知时对应计数和remaining为null并标evidence.read_errors。Token/费用仍来自同一Host模型尝试，不能把两臂投影再次累计。


## 系统目录选择

`POST /api/directories/choose` 无请求体，只允许 loopback 连接且要求与服务匹配的 Origin；缺失或跨源返回 403。响应 HTTP 200 为 `{ "status": "selected", "path": "/absolute/path" }`，或只含 `status` 的 `cancelled`、`unavailable`、`busy`、`timed_out`。路径保留空格与换行，不返回其他目录信息、命令输出或异常文本。窗口属于服务宿主机；浏览器必须显式点击，不自动重试，不需要持久幂等键。选择仅回填表单，注册仍走原有项目接口及其幂等校验。取消、断开及 120 秒超时等待进程回收，重复请求在收尾期间仍返回 busy；不可用时只提供显式手动输入。
## 长期偏好记忆

全部写入要求本机/同源、`Idempotency-Key`（1–128 可见 ASCII）。成功变更与 `http.mutation_completed` 在一笔 SQLite 事务内提交；同键同体返回原结果，异体 409。已明确拒绝的错误有 `X-Eventide-Mutation-Outcome: rejected`；503 未知不能换键自动重放。标准错误 envelope 与其他路由一致。请求严格拒绝额外字段，revision 必须整数（不接受 bool）。

| 方法和路径 | 请求/响应 |
|---|---|
| GET /api/memory | 可选 project_id/include_revoked=false；返回 settings、memories、suggestions、warning。不带 project_id 只有全局，带则全局加精确项目；无效项目404。建议包含历史状态，UI 可筛 pending |
| POST /api/memory | `{scope:"global"或"project",project_id:null或ID,key,content}`；201 `{memory}`。global 不允许 project_id；project 必须真实ID |
| PATCH /api/memory/settings | `{expected_revision:0或当前版本,enabled:bool}`；`{settings}` |
| PATCH /api/memory/{id} | `{expected_revision,content?,status?:"active"或"paused"或"revoked"}`；`{memory}`。不能改 scope/key；revoked 不可恢复 |
| DELETE /api/memory/{id} | body `{expected_revision}`；`{memory}` 状态 revoked，不是硬删除 |
| POST /api/memory/suggestions/{id}/resolve | `{expected_revision,action:"accept"或"dismiss"}`；`{suggestion,memory?}`。accept 复核来源和同主题冲突 |
| GET /api/runs/{run_id}/memory | `{snapshotted,snapshot,injections,receipts}`；不存在Run404。排队未开始 snapshotted=false；没有实际请求 injections=[] |

Memory record：`id,revision,scope,project_id,key,content,status,source,created_at,updated_at`，source 为 `{type:"manual"|"explicit"|"suggestion",event_ids:string[],session_id:null|string}`；时间为 epoch 秒。主题 key `[a-z0-9_.-]{1,64}`，正文去除首尾空白后 1–500 字符，禁止换行/控制字符及已识别秘密和权限指令。Suggestion 字段相同但状态 pending/accepted/dismissed；独立 revision。Settings 为 `{enabled,revision}`，默认 true/0。

Injection 为 `{request_id,attempt_id,memories:[{id,revision,scope,project_id,key}],digest}`。空 memories 表示该次请求无长期偏好注入。digest 固定实际注入块，不表示完整消息或答案因果。Receipt 为 `{type:"memory.saved"|"memory.suggested"|"memory.rejected",memory_id?,suggestion_id?,reason?,revision?,key?,content?,scope?,scope_defaulted?}`。MK-18 自然语言 intake 的 saved/suggested 回执带内容、作用域与版本；`scope_defaulted=true` 表示原话未指定范围、已默认当前项目。记忆记录 source 可含 `span:{start,end}`，绑定原用户消息中内容片段的精确偏移。SSE 同名事件以及 `memory.injected` 可触发刷新；`memory.snapshot` 仅表示选择，不等于模型使用。建议/设置修改在控制Session留事实，管理页 mutation完成后应重新读取列表；运行页读取具体Run的注入事实。

状态冲突/同作用域同key冲突409，记录不存在404，无效内容/来源422；同键绑定冲突沿原协议409。关闭总开关停止注入与新建议，但不拒绝主动保存或接受已展示建议，且不重新开启。普通编辑下一Run生效，暂停/撤销下一实际请求生效，重新启用条目下一Run生效。UI 必须提示撤销不擦除历史聊天、摘要、事件、导出和备份，已派发请求不可撤回。

`snapshot` 为该Run开始执行时的 `{memories:[{id,revision,scope,project_id,key}]}`，尚未选定时为 null；它是选择证据，不表示已派发。

## 可选 Docker 执行配置

`POST /api/sessions/{session_id}/runs` / Python `RunRequest` 新增 `execution_profile: "local" | "sandbox" = "local"`、`sandbox_image: string | null = null`（非空时最长 256 字符）。幂等指纹包含这些字段。sandbox 首次接收要求显式本地镜像和 `execution_target="source"`；它会创建自己的候选副本，不能同时选 snapshot/worktree。选择 Skills、local 请求携带 sandbox_image、无镜像/本机 Docker、远程 Docker daemon 或无效镜像时接收前拒绝。已运行 Session 固定边界，不能在 local/sandbox 间切换；后续 sandbox 请求可将 image 省略或设 null，继承固定镜像；不同镜像拒绝。Continue 没有配置覆盖字段。

Session 响应新增 `execution_profile: "local" | "sandbox" | null`（null 表示尚无 Run）和 `sandbox_profile: object | null`。Run 的既有 `execution` 响应在沙箱时为 `{kind:"sandbox", sandbox_id, working_directory, skipped, profile}`。profile 固定字段为 `kind:"sandbox", image:"sha256:…", requested_image, network:"none", memory_mb:512, cpus:1, pids_limit:128, timeout:120, output_limit:25000, disable_mcp:true`。镜像检查仅证明本地镜像存在，不是 Python/项目依赖或完整 Docker Desktop 验收声明。清理未知须继续展示失败/停驻状态。

`GET /api/runs/{run_id}/sandbox-diff` 返回 `text/plain` 和 `Content-Disposition: attachment; filename="candidate.patch"`，`Cache-Control: no-store`。要求本机调用，显式跨站 Origin/Sec-Fetch-Site 拒绝；不存在 Run 为404，非沙箱/产物不安全/超过2MiB/二进制或不支持文件名为422，仍在运行或清理未知为409。补丁对应该 Session 当前共享候选与首次过滤 baseline，可能包含后续 Run 的修改，不能当成所查历史 Run 的固定产物。接口不应用补丁、不改源文件；必须由用户审阅后手动处理源漂移与冲突。


### 独立验收的长输出证据 —— 已实现

独立验收check沿用原 `content/is_error/metadata`，metadata新增 `output_evidence={version:1,artifact_id,source_run_id,call_id,streams:{stdout,stderr}}`；Session级 `acceptance.output_started/recorded/expired/cleanup_failed` 通过source_run_id关联控制Run，不重开已终态Run。每流字段为 received_bytes（崩溃恢复为null）、stored_bytes、stored_chars、sha256（无可读快照为null）、sealed、encoding_loss、capture_state、reason；恢复/删除失败可带storage_errno。完整性只描述日志采集，不表示检查通过。

现有模型工具 `read_tool_result(run_id,call_id,offset=0,limit=4000,stream?)` 接受可选stdout/stderr；省略或null沿用原已保存content字符坐标，不隐式解析旧日志分流。新stream坐标是脱敏保存文本的字符数，单页limit最多12,000，页头返回next_offset或end_of_stream、availability、capture_state/reason及encoding_loss。只能读取同Session或控制器冻结的精确Writer Run/call白名单；不提供任意路径或新增下载HTTP入口。

capture_state为complete、quota_exceeded、write_failed、redaction_unavailable、interrupted或not_started；reason细分stream/run/global配额、写入/最终化失败、取消/超时/进程重启等。Docker管理诊断继续原content预览，未派发attach的命令归档为not_started。availability区分available、not_saved、expired、missing、corrupt与corrupt_or_unreadable；available仍可能只含不完整前缀，旧记录显式请求stream返回无分流证据。归档全文不自动灌入事件、SSE或JSONL；原短预览及主动回读的有界页仍按现有工具结果记录与导出。原12,000字符修正反馈和命令退出/人工采纳规则保持。


### 副本索引、任务准备与可信检索（RG）

普通Run与交付repair请求新增 `knowledge_index: existing|prepare`，默认existing。省略与显式existing保持同一幂等内容；prepare与existing是不同意图，同键不同意图409。接收时冻结选择和embedding配置摘要；Continue继承并复验，历史缺字段不补造。snapshot/sandbox的prepare在接收前拒绝；只读worker不提供索引工具。每轮Writer准备属于自身Run，无额外根任务。准备失败不进入模型循环，取消/预算/未知沿既有语义，重启不自动重放。

Run、交付repair与手动索引请求共用 `code_structure: boolean=false`、`source_scope: code|all=code`。布尔值严格校验；省略与显式默认值保持原幂等内容，非默认选项参与请求身份。canonical admission冻结 `index_configuration.chunking_profile/source_selection_profile`，启动及Continue复核规则版本；规则改变或历史prepare缺少策略身份时明确拒绝并要求新任务，历史existing不补造身份。交付每轮Writer继承相同选项。手动构建详情source_scope及旧统计新增字段允许null；新build含source_scope、source_selection_fingerprint及skipped_reasons（原因→文件数）。原索引可查询，构建策略不一致必须显式force。

手动 `POST /api/projects/{project_id}/indexes` 新增可选workspace_id；省略为源Workspace，只接受同项目注册且存在的副本。回执和详情新增workspace_id，项目历史聚合副本构建。新 `GET /api/workspaces/{workspace_id}/index-status` 返回status=available|unavailable、workspace_id、location=managed|legacy，以及可选reason/detail/model/generation/embedding_fingerprint/config_hash。字段可为null；available表示匹配索引可用，不代表全部源码都最新。损坏的新位置不回退旧位置。

手动索引幂等身份固定在项目范围；同键改变workspace_id也返回409，不能通过更换控制Session启动第二个构建。执行和查询结果仍归属精确副本；省略目标的旧请求沿用原源目录身份算法。

持久 `retrieval.status` payload含status=preparing|available|unavailable|failed、实际workspace_id、可选location/model/generation/embedding_fingerprint/reason/detail；准备成功可附build统计。Run投影的retrieval直接取最新事件，旧历史为null，SSE重放不能触发准备或消费。`tool.completed.metadata.retrieval` 含workspace_id/index_generation/embedding_fingerprint/mode/diagnostics和hits；命中含file/start_line/end_line/indexed_content_hash/freshness，freshness区分fresh/changed/deleted/unsafe/unreadable/too_large。旧调用缺metadata显示未记录；无命中与索引不可用分别表达。

全文正文仍有界且经统一脱敏。过期命中不携带旧正文；索引中的行号只用于定位，不承诺当前文件同坐标。新鲜度只表示检查时文件哈希一致，不能推断答案充分性或未来稳定性。远程embedding与查询改写继续使用原attempt账本；没有新增证据数据库或响应缓存。

## 单次本地文件操作

- `GET /api/sessions/{session_id}/file-operations` 返回 `{operations:[...]}`。每条含 `operation_id/session_id/run_id/call_id/workspace_id/cwd/path/tool/before/after/status/change_kind`；before/after 是存在性、SHA-256、字节数与基本属性，没有正文。status 为 `prepared|completed|no_change|failed|needs_review|undo_prepared|undone`，change_kind 为 `added|modified|unchanged`。旧历史不补造操作。
- `GET /api/sessions/{session_id}/file-operations/{operation_id}` 复核原 Session/Run 与实际 Workspace/cwd、根身份和当前路径后返回同一记录，加 `patch/diff_status/diff_message`。只从该次加密完整 before/after 生成 unified diff，不比较 Git HEAD。合计超过200,000字符/4,000换行则 `patch=null,diff_status=too_large`，完整备份保留；正常为 available。不可读取或不安全路径明确报错，不通过历史记录绕开边界。
- `POST /api/sessions/{session_id}/file-operations/{operation_id}/undo` 无正文，要求 `Idempotency-Key`，返回新的记录。沿用本机、严格同源、内容绑定 mutation 回执；无模型工具入口。只允许 completed，当前存在性/内容/权限指纹须等于该次 after，原文件恢复 before，新文件只删除自身。相同操作已 undone 直接返回既有状态，不重执行；无变化、待核验、活动 Workspace 或后续修改冲突返回409，明确拒绝时带 `X-Eventide-Mutation-Outcome: rejected`。不可授权403；恢复结果未知503，回执故障可能500，同键未知不重放，用GET核对当前状态。

新 `file.operation` 为 Session 级审计事实，payload.run_id/call_id 指向原工具；tool.completed.metadata.file_operation_id 仅是小引用。大备份不在 SSE/UI 事件、JSONL 或模型消息中；人工详情 diff 是有界的受控本机读取。服务重启将 prepared/undo_prepared 标 needs_review，禁止盲目撤销。API 不接受任意备份磁盘路径；本地同 UID Shell 的既有宿主权限不是模型身份隔离。交付 patch/apply 契约不变，没有新增整批交付撤销。


<a id="maka-upgrade-api"></a>
## Maka 升级接口与前端计划（设计草案，全部待实现）

本节对应 [完整方案](architecture.md#maka-upgrade-plan) 的接口/UI 设计。MK-00 契约切片已实现（见上文 `GET /api/runtime/contract` 与 Vue 启动屏障），其余路径、事件名和 DTO 仍为评审草案，实现前不生成 OpenAPI/TypeScript；既有接口继续按上文工作，不能根据本节提前调用。

### 共用协议

- 保留本机同源、内容绑定幂等接收、身份授权和显式错误；运行控制由同一 Host/Store 执行。长动作先持久接收，返回 202 和 operation/run 身份，查询/SSE 跟踪，断线同键查询或重放接收回执，不重复派发副作用。
- 建议新增 `contract_version` 和能力发现；破坏性 API 变更随前后端同版本迁移。未知版本提示升级，不静默套旧语义。旧事件缺字段显示未记录，不能补零或假设成功。
- 对可变控制意图使用 `revision/expected_revision`，对恢复/验收使用 `evidence_version` 和源码指纹；重复键同内容返回原结果，不同内容/过期版本 409。冲突响应给当前版本和稳定原因，不回显秘密。
- 核心身份：`project_id/session_id/run_id/attempt_id/call_id/workspace_id`；新增对象按需带 `recovery_id/graph_id/node_id/activation_id/question_id/message_id/side_session_id/artifact_id`。这些是关联字段，不把同名 ID 当跨项目读取授权。
- 持久事件继续有序号/稳定身份，预览另带 attempt/revision 且不推进持久游标。SSE 断开按最近提交游标续订，支持 Last-Event-ID；服务代次变化或事件缺口走一致快照+补读。不能靠重新发送 POST“找回连接”。
- 统一交互响应包含 `status/reason_code/message/evidence_refs/available_actions`（按对象适用）；UI 不通过错误字符串决定能否执行。非允许状态的动作后端再次拒绝。
- 所有待实现动作覆盖 loading/accepted/queued/running/waiting/retrying/needs_review/terminal，采用适合各对象的子集；页面关闭不取消，停止为独立幂等动作。密钥、未脱敏内容和原始 Provider 私有数据不进入通用状态 DTO。

### 逐项后端—接口—前端矩阵

表中 GET/POST 是拟议控制面；模型工具仍走 ToolExecutor，不把 HTTP 管理端点直接作为模型权限。复用已有接口时扩展其真实 DTO，不新增重复事实源。

| 计划 | 后端契约与拟议 HTTP 入口 | 事件/字段重点 | 前端交付与浏览器验收 |
|---|---|---|---|
| MK-00 迁移 | 扩展 `GET /api/runtime/settings` 或能力接口，返回契约/数据版本与可用功能；迁移在服务启动维护阶段执行 | `contract_version/schema_version/migration_status`，只读旧历史支持集 | 版本不兼容页、迁移结果和替代入口；旧会话、记忆、diff/undo、账本仍可访问。**契约切片已实现**：`GET /api/runtime/contract`（真实 schema 版本 + 已实现能力表）与 Vue 启动兼容屏障/重试已上线，见上文"MK-00 契约切片"；`migration_status` 与迁移维护页仍随完整迁移底座待实现 |
| MK-01 编辑唯一性 | 扩展 `edit_file` 工具 schema/错误 metadata；沿用现有 file-operations 列表/详情/undo | `ambiguous_match/source_changed`、版本、匹配数量和有界位置；成功关联 operation_id | 工具卡显示为什么没改、可跳源码；现有红绿 diff/撤销保持，歧义时文件和差异列表不新增成功记录 |
| MK-02 文件分页 | `read_file` 返回不透明 continuation_token；受控证据读取接口接同一版本凭据 | Workspace/文件内容版本、行及行内范围、完整性、next_token | 连续读取/文件变化提示/定位；旧页不与新页悄悄拼接 |
| MK-03 摘要 | `GET /api/runs/{run_id}/context/summaries`；只读查看，生成由 Runtime 调度 | covered_seq、来源 digest、schema_version、证据引用、校验结果 | 上下文侧栏显示替换范围、失败原因、原文入口；刷新保持同一版本 |
| MK-04 输入与规则 | `GET /api/runs/{run_id}/attempts/{attempt_id}/context`；项目规则来源查询 | 规则/Skill/记忆/工具目录及裁剪清单、大小、来源/版本；脱敏有效投影 | “本次模型看到了什么”面板，切换尝试不串数据；截断和未加载明显区分 |
| MK-05 长日志 | 切片 1 已实现：`GET /api/runs/{run_id}/calls/{call_id}/command-log`（本机 Shell/Skill）；受管后台进程（MK-10）与容器/沙箱采集属后续，不做宿主降级 | capture_state、availability、偏移/下一页、received/stored 字节、省略量；退出/超时仍在原工具结果 | 工具卡日志展开/分页/缺失提示；中间错误能找到，过期不显示为空成功 |
| MK-06 补充与提问 | 已实现 `POST/GET /api/runs/{id}/supplements`；以下为后续计划：`POST /api/sessions/{id}/messages` 带 current/next 意图；`GET /api/sessions/{id}/questions`；`POST /api/questions/{id}/answer` | `message.accepted/consumed`、question opened/answered/expired、来源 Run/revision、生效 attempt | 输入区选择补充/下一轮；问题卡含选择与自由回答；刷新、重复回答、停止后迟到答复有明确结果 |
| MK-07 审批 | 切片 1 已实现：`GET /api/attention`/`GET /api/projects/{id}/attention` 返回结构化审批卡，动作仍走既有 `POST /api/runs/{run_id}/approvals/{approval_id}`；扩展现有 approval GET/resolve DTO，不另造授权通道 | command/cwd/paths、effect、执行边界、参数 digest、revision、scope | 可读审批卡及原始详情；源/副本/native/Docker 一目了然；过期批准拒绝 |
| MK-08 恢复 | `GET /api/runs/{id}/recovery`；`POST /api/runs/{id}/recovery/actions`；扩展现有 Run 状态/SSE/停止 | recovery_id、classification、检查项、next_retry_at、attempt/max_attempts、claim、successor、核查证据、available_actions | 聊天恢复条+恢复详情；断网接回、倒计时、重启自动续跑、未知核查、停止、双标签去重 |
| MK-09 待处理 | 切片 1 已实现：`GET /api/attention`（全局）与 `GET /api/projects/{id}/attention`（按项目），无 cursor 分页；动作跳对应对象接口，`awaiting_answers` 明示不支持 | attention_id、type、source/version、reason、actions；解决状态由源事实派生 | 项目待办入口和筛选，点击旧卡复核；隐藏不等于解决，不卡住其他会话 |
| MK-10 后台任务 | `GET /api/sessions/{id}/background-tasks`；详情/日志；`POST /api/background-tasks/{id}/stop`；有界 health 查询/探测 | started/ready/exited/unknown、所有者/进程身份、隔离配置、日志引用 | 后台任务面板；进程存活和服务就绪分开，停止子树后刷新状态一致 |
| MK-11 工具并行 | 工具调度接线，扩展 Run/tool 投影，无独立启动 HTTP 必要 | batch_id、call_id、dependency、状态、稳定结果顺序 | 并行活动可展开，某项失败不会把整批显示成功；取消结果不丢 |
| MK-12 产物 | `GET /api/sessions/{id}/artifacts`；详情/content/download，仅受控身份 | 来源 Run/tool/Workspace、版本/MIME、可用性、证据保护/保留策略 | 本次产出列表、Markdown/图片/隔离 HTML 预览、下载；恶意 HTML 无宿主权限 |
| MK-13 比较评测 | 扩展已有 evaluation plan/confirm/results API，新增比较 manifest 和 arm | model/config/tool/budget/grader/commit 指纹、逐题结果、未尝试/失败/未知 | 比较页面列条件及逐题证据；不同预算显式分组，不做虚假同条件排名 |
| MK-14 模型目录 | 切片 1 已实现：`GET /api/generation/defaults` 与 `GET /api/sessions/{id}/generation` 响应附 `capability`（规范端点身份、来源/预设引用/目录版本、有效能力、覆盖差异、`config_saved`/`connection`/`tool_verification` 三状态）；`run.admitted` 持久化 `capability_ref` 并在 Continue 复用时冻结校验。未实现：真实工具/流式验证状态写入、`GET /api/models` 目录视图扩展 | 目录来源/版本、端点身份、声明能力、连接/工具/流式验证分别记录 | 已验收：异端点不继承、规范端点身份、未知 `null` 不猜、显式覆盖可追溯、原 Run 冻结不被新配置改写、探测失败/通过不冒充工具验证、前端模型切换迟到响应不串；后续完整验收按 MK-22 |
| MK-15 搜索 | 后端切片已实现（2026-10-26）：`search_text` schema 新增 `mode`（literal/regex）、`case_sensitive`、`context_lines`（0–10）；结果为 `path:line:` 命中、`path:line-` 上下文及每来源实际搜索字节 sha256（可用作 edit_file expected_sha256）；头部报告 mode/backend/扫描/跳过/超时/核对排除计数。regex 需本机 rg，缺失返回能力错误不降级；沙箱任务副本仍不提供 search_text。受控证据读取复用 MK-02；源码定位 UI 待后续 | literal/regex、context_lines、backend、文件版本、扫描/省略计数 | 已验收：真实 rg 接线、截止 kill/reap、输出上限、伪造输出核对排除、路径注入拒绝、sha256 与 edit_file 绑定；正则不支持/扫描不完整明确呈现；UI 预览与整体效果按后续 MK-22 |
| MK-16 发现 | 已实现（切片 2）：执行目录组装时冻结 `tool.catalog.frozen`、加载事实 `tool.loaded`/`tool.load_failed` 落同 Run EventLog（MCP metadata 不作可信来源）；`GET /api/runs/{run_id}/tools?query=&source=&status=all\|loaded\|unloaded\|failed&offset=&limit=`（默认 20，≤50）与 `GET /api/runs/{run_id}/tools/{name}` 只读投影冻结目录：loaded_count/failed_loads/stale_receipts/notes，回执须绑定目录版本+schema digest；无冻结事实返回 `catalog_frozen=false` 加说明，404 对未冻结工具。列表不内联 schema；另保留 `GET /api/workspaces/{id}/tools` 常备基础目录。Vue 聊天流 `RunToolsPanel` 搜索/来源/状态、精确契约、失败展示，无执行入口 | 加载不自动授权，执行仍过 Policy；目录版本变更需重新 discover；真实 MCP 旅程与浏览器验收待集成 |
| MK-17 旁枝 | `POST /api/sessions/{id}/side-questions`；复用子 Session 查询/SSE；`POST /api/side-questions/{id}/return` | source highwater、snapshot、引用、只读边界、回传身份及 consumed 状态 | 聊天旁栏、引用来源、刷新背景、回传预览；主任务继续、回传只消费一次 |
| MK-18 记忆 | 复用 memory records/suggestions/settings CRUD；扩展自然语言来源和保存回执；不开放任意模型授权写入口 | explicit/suggested、canonical source/span、scope、revision、撤销/冲突、抽取 attempt | “已记住+撤销”、建议确认卡和管理页；来源可查，“这次”不会存成长期偏好 |
| MK-19 报告 | 切片一已实现：`GET /api/runs/{id}/diagnostics`（可选 `high_water`）与 `GET /api/runs/{id}/diagnostics/download?format=json\|markdown`；完整计划保留 `POST /api/runs/{id}/diagnostic-exports` 产物化导出（未实现，见下方小节） | 确定/推测原因、失败点、已完成工作、未知项、脱敏字段、证据/缺失、report_version | 人话摘要、诊断详情、导出预览/选择内容；不自动上传，不夹带密钥/整仓源码 |
| MK-20 Graph | `POST /api/sessions/{id}/graphs`；`GET /api/graphs/{id}`及 events；图/节点 stop、revise、retry；整合/验收/采纳复用 delivery | graph/node/work_revision/activation、依赖、claims、结果 refs、预算、workspace、verification | 图+列表、节点会话、等待原因、修改/停止、整合 diff 和人工采纳；循环依赖/过期修订拒绝 |
| MK-21 隔离 | `GET /api/runtime/execution-backends`；扩展 Run 接收配置与 Session binding；边界扩展走现有审批 | backend/probe/能力矩阵、profile revision、paths/network、限制与不支持原因 | 环境设置、会话环境徽标及审批差异；native 不可用不能暗中切 local |
| MK-22 收口 | 所有上列契约实际 OpenAPI 生成；静态资源/服务契约版本同批发行 | 数据迁移、能力/平台验证记录和旧 API 替代清单 | 全流程真实 API 浏览器、320px、刷新/停止/重复请求/版本不匹配及实际包分发 |

### 恢复动作与错误明细草案

恢复 GET 至少包含：`source_run_id/recovery_id/revision/classification/reason_code/checks/affected_resources/reconciliation/next_retry_at/successor_run_id/evidence_refs/available_actions`。checks 每项区分 passed/failed/unknown/not_applicable，不把未知当失败或通过。

动作 POST 只接收枚举动作、expected_revision 和必要的用户输入，内容绑定幂等键；动作可包含停止自动恢复、重新检查、回答待核查问题、授权建立新工作基线。不能提供可直接改写历史 outcome 的自由布尔字段。自动核查使用同一内部动作语义并记录系统来源，不伪造用户操作。

模型重试相关持久事实建议：`model.retry_scheduled/model.retry_started/model.retry_exhausted`；恢复建议：`recovery.assessed/claimed/reconciliation_started/reconciliation_recorded/continued/blocked`。计时显示可本地推算，必须以持久 next_retry_at 为准；周期倒计时不逐秒写日志。

拟议稳定错误包括 `retry_exhausted/recovery_not_safe/evidence_stale/tool_outcome_unresolved/interaction_expired/catalog_changed/execution_backend_unavailable/unsupported_profile/graph_revision_conflict`。保持原 HTTP 语义，错误带可行动作和受控证据引用，不用 200 包装所有失败。原未知写请求同键核对与新恢复动作是不同对象，不把“恢复”当改变原键强行重放。

### 页面组织与接线记录规范

现有 ChatView/Pinia API 层承接状态，按功能拆组件；计划页面包括恢复详情、Graph、工具目录与执行环境、记忆管理、待办、产物/报告，旁枝及上下文优先侧栏。保持现有项目/会话、交付、评测和用量入口可达，避免新增功能后旧功能隐藏失效。

每个 MK 项在 AGENTS 记录：后端领域与权限、HTTP/OpenAPI、SSE/投影、Vue、定向验证、最终真实 API 旅程、分发资源七个完成证据。只有全部适用项具备证据才能标功能完成；纯工具项没有新增 HTTP 时明确“复用某接口”，不能空着当已完成。

前端必须验证：首次进入与刷新同样可用、迟到响应不串项目、重复点击只产生一次意图、事件乱序/重连按事实修复、停止等待真实收尾、关闭面板不取消、操作过期明确提示、错误可读、键盘可操作、窄屏可访问。真实模型效果与这些接线测试分别记录。

### MK-08A 第一批实现：浏览器事件续接

`GET /api/runs/{id}/events` 接收非负整数查询 `after` 和可选请求头 `Last-Event-ID`，取较大者后严格返回新事件；格式非法返回 422。响应禁止缓存与代理缓冲。重连只查询已持久事实，不创建 Run、不调用模型或重放工具。

Vue 使用最后成功交给状态层的持久事件序号重建连接，忽略旧连接回调和重复事件；临时预览不推进游标，断线清除。错误后从 500ms 指数退避至 30s 基值，附加最多 25% 正向抖动，稳定连接 10s 后重置计数；离线暂停，联网或页面重新可见时唤醒。终态停止订阅；切换会话和卸载清理订阅及重试。页面区分连接中、重连等待、离线并显示倒计时，连接恢复触发会话状态刷新。

本批尚未实现服务启动代次、原子快照与事件缺口协议、按模型尝试隔离预览、写入回执统一核查；完整 MK-08A 仍进行中，后端自动恢复另按 MK-08B/C/D 实施。

### MK-08B 模型重试事实

model.retry保留request_id/attempt_id/attempt，增加category；retryable表示实际上是否还有下一次。计划重试时给delay_seconds和next_attempt，可包含retry_after_seconds；服务商活动边界为server_side_activity=true，累计等待超限为retry_wait_limit_exceeded=true。终止分类也留此事实，但retryable=false不表示已经重试。

model.preview增加可选attempt_id，不拼接不同尝试草稿。attempt_started/finished、预算和最终response仍是唯一持久事实。取消和半截工具不产生完整响应。完整恢复页面和动作矩阵仍待MK-08F，不新增隐藏执行入口。

MK-00复核补充：兼容响应必须有有效schema_version、runtime_version及完整字符串capabilities列表，不补默认值冒充兼容。响应体读取/解析失败、字段残缺显示“无法确认兼容性”，不推断版本不匹配。capabilities表示此版本已实现的能力，不表示本机已安装对应依赖或已授权执行。页面离开后，迟到的兼容查询不会再启动工作台轮询。

### MK-01/02 文件工具增量契约

read_file新增可选continuation，带凭据时不能同时传非零offset；每页头返回sha256，尚有内容时给下一continuation。凭据属于模型工具接口，HTTP工具结果仍通过原持久tool.completed查询；界面不能把不同版本的页拼为同一证据。edit_file新增可选expected_sha256，版本不符、空目标、未命中和多处（含重叠）匹配均返回错误且不修改文件。大范围歧义只展示前5处并明确数量下界。具体行内偏移以返回凭据为准，不依赖固定分页长度。

### MK-19 第一批实现：可读故障报告切片

`GET /api/runs/{id}/diagnostics` 返回事实派生的中文报告，可选查询 `high_water`（非负整数）复现任意旧版本；Run 不存在返回 404，非法参数返回 422。`GET /api/runs/{id}/diagnostics/download?format=json|markdown&high_water=N` 以附件下载 JSON 或 Markdown，两格式渲染自同一份事实，内容一致。报告固定 `report_version=1`，绑定 `(run_id, high_water)`；`high_water` 为纳入事件的最大 `session_seq`（无事件为 0），其后提交的事件不会混入已生成版本，重新生成才纳入。生成过程只读已保存事件：不调用模型、不执行工具、不追加事件、不自动上传或外发。

报告包含：状态与执行身份（mode/provider/model）、五段中文摘要（发生什么/停在哪/完成了什么/未知项/下一步）、已知原因与待查事项分列、有界工具与 attempt 明细、缺失与不可用清单及固定说明（当前版本不含任务图/恢复关系与恢复动作入口）。事件文本经既有 `redact` 有界截断（约 300 字符）并在响应层替换配置密钥；对话正文、源码、完整工具输出与提示不进入报告。缺事件时 `facts_available=false` 并明确列出不可用，不编造已恢复或已验证。

Vue 新增 `/runs/:runId/diagnostics` 报告页（历史页运行详情提供入口），含加载、错误（保留后端 message）、缺事实、重新生成与高水位绑定展示；下载链接始终携带当前报告的 `high_water`。切换 Run 丢弃迟到响应。本批尚未实现恢复动作入口、证据跳转、Graph/恢复关系、`POST diagnostic-exports` 产物化导出与用户选择内容预览；完整 MK-19 仍进行中。

MK-08B等待状态：Run投影新增可空`model_retry`，含原始attempt/next_attempt/category和绝对`retry_at`（事件ts加delay_seconds）。GET恢复当前等待，不以浏览器重新打开时间重置倒计时；下一次attempt开始、完整response或Run终态清除。旧历史未记录等待时返回null，不猜时间。Vue区分模型请求重试与页面连接重试，仍可停止原任务。

MK-08A快照与服务代次：新增只读`GET /api/runs/{id}/snapshot`，返回`run_id/epoch/high_water/state`；Store同一锁内读取状态与事件高水位。SSE首次发送无事件游标的`stream.state`帧，结构相同并附`reset`；客户端游标大于现存高水位时reset=true，页面重建当前状态并从该位置续接，绝不重发Run写请求。正常重连仍重放after之后的事件，不用快照跳过未处理事实。epoch为本次API实例身份，临时preview带epoch，旧代次预览丢弃。当前事件日志不裁剪；未凭数字间隔臆测缺失（同Session不同Run的seq可以不连续）。历史API、SSE持久事实不因重连更改。

MK-18回执当前状态：run memory receipts保留原content/revision，附可选current_status/current_revision用于区分历史保存与当前撤销/编辑；旧历史无关联事实不编造。source.span使用Unicode字符起止（左闭右开）。

MK06当前接线：`POST /api/runs/{id}/supplements`正文含body(1–8000字符)/body_version(1–9)，要求同源本机与幂等键；GET返回supplements/pending。pending只代表已接收，consumed代表已持久加入当前Run消息，不保证请求已发送或模型采纳；终态前未加入消息者投影为late。终态后收据保留在Session事实并绑定原Run，不启动新任务。Web启动的自动恢复后继使用相同ApprovalBroker，新的审批仍通过原审批端点操作。

MK17当前接线：旁枝创建在共享Git协调下冻结源Workspace快照与主Run事件高水位，独立Session/Run以`readonly_profile=side_question`执行，只读工具仍经ToolExecutor/Policy，共享父Run预算与模型槽；`continuation_of/recovery_of`不复制到旁枝intent。旁枝提示中的证据只来自高水位内的已保存事件（用户目标、最近助手文本、最近工具活动，均脱敏截断），并明示之后事件数量被排除；不读取创建后的文件当当时事实。快照临时目录在旁枝Run终态（完成/失败/取消/Host关闭）即删除，有文件级测试。回传经既有supplement接收路径：绑定用户所见答案的SHA-256指纹，不符409；同一旁枝只注入一次；主任务终态记late不自动续跑；超限正文显式截断并在收据与响应标注`body_truncated/answer_length`。创建事实为Session级`side_question.created`事件（终态Run不能追加Run级事实），列表路由按`source_run_id`过滤读取。

MK07/09当前注意：attention的counts是本次返回数量，limits声明审批和失败列表上限；项目过滤在截取前执行，包含该项目执行副本。全量翻页、问题等待、精确参数版本和恢复动作仍属于计划，当前响应不能冒充完整待办总量。聊天/待处理实际API旅程验证了补充刷新、精确拒绝和同一Run后续消费。

### 源码读取证据投影（MK-02/15，2026-10-26）

既有 Run 的 `tools[call_id].metadata.source_evidence` 与 `tool.completed` 事件保存相同对象，无新增写接口。仅已实现的内置 read_file/search_text 生产处理器输出它，旧记录可缺省。

- `kind=file`：`path`、`status=read|error`；成功页含原始字节 `sha256`、`total_lines`，有正文时含一基 `start_line/end_line`、零基 Unicode 字符 `start_character/end_character`（结束偏移不包含）、`complete` 与可空 `next_token`。空文件无正文坐标。越界范围无正文坐标，不虚构已读范围。错误保留明确文本及 `metadata.code`，即使返回当前 SHA 也不能解释为已读到请求页；旧凭据不能继续拼接。
- `kind=search`：`mode/case_sensitive/context_lines/scanned/skipped/unverified/timed_out/output_capped/partial/next_offset`；`sources[]` 含 path、实际搜索字节 sha256、已呈现的 `lines[{line,match,text}]`。`match=false` 是上下文，text 可包含明确省略标记。`partial` 是扫描不完整，非空 next_offset 是还有结果页；任一成立都不能宣称全仓完整搜索。搜索失败的旧文本错误路径可不含此对象。

刷新读取的是已保存事实；不重新访问现在的源码来改写历史版本。接口不提供该对象对应的额外文件权限。

### MK-04 输入清单 API（2026-10-26）

`GET /api/runs/{run_id}/input-manifests?before=<exclusive session_seq>&limit=50`：只读，limit为1–100；未知Run404、无效分页422。返回run_id、entries（页内时间正序）、next_before（可空，供较早页查询）、recorded_count（当前before范围内条数）、high_water（读取的Run事件上界）。不读取当前源码重建旧清单，不触发模型或工具。

每条带manifest_version=1、request_id/attempt_id/kind/model、seq/at、system_hash/messages_hash/tools_hash、message_count/tool_count、tool_names、tool_result_ids、composition（serialized_unicode_characters_v1字符计数）、可空instructions与instruction_hash。instructions只对agent调用关联同Run该事件之前最近的context.configured规则加载记录；摘要等辅助请求不继承此记录，不凭当前配置填补旧历史。清单在适配器入口落库，不能当成网络到达回执；使用量仍以原attempt账本为准。OpenAPI与前端类型同步生成。
### MK-08F/MK-19 接线：恢复详情投影（2026-10-26，切片）

`GET /api/runs/{id}/recovery` 返回只读恢复详情，Run 不存在返回 404。内容来自 `store.recovery_snapshot` 的同一锁原子切面（身份、Run 事件、该 Run 为 source 的 `recovery.blocked`/`recovery.claimed` session 事实）加显式传入的当前调度事实：`projection_version`、`status`、`is_terminal`、`high_water`、`continuation_of`、已记录执行进度摘要（steps/tool_calls/checkpoint/usage）、`model_retry` 当前等待、停止分类 `stop.kind`（模型重试等待/服务自动续跑/用户停止/预算/未知结果/连续失败/服务重启停驻/失败/完成等）、未知工具与待审批清单、恢复关系（冻结 `recovery_policy`、`recovery.claimed` 唯一后继及其当前实时状态、`recovery.blocked` 列表、`recovery_of` 原运行）。 admission 早于持久恢复策略的旧历史返回 `history_available=false` 并附注，不推断其可恢复性。

`available_actions` 由服务端按当前事实计算：`stop` 仅非终态且非控制会话可用（复用既有 `POST /api/runs/{id}/cancel`）；`continue` 仅该 Run 是会话最新运行、状态为 interrupted、会话空闲且无未知工具结果时可用（复用既有 `POST /api/sessions/{id}/continue`，端点自身复核）。没有自动重试动作，也没有"忽略风险重跑"；失败与未知结果不会被告知可恢复。本端点纯查询，不写事件、不领取恢复、不派发模型。

Vue 新增 `/runs/:runId/recovery` 页面与 `RecoveryPanel`（历史 cut 与后继实时状态分开呈现，切换 Run 丢弃迟到响应），Chat 停驻卡片新增"查看恢复详情"入口；停止/继续按钮只在服务端判定可用时出现，动作后重新读取事实。页面纯查询 + 导航到既有受控操作，旧事实页不触发模型、文件或审批。`POST recovery/actions` 未实现，不伪造。

恢复详情的 high_water 包含按 source_run_id 绑定的 Session 级恢复事实；当前后继状态单独标明实时读取。claim 只表示领取后继，不能当作已派发或已成功。恢复中心继续操作发送 `POST /api/sessions/{id}/continue`、`{"expected_run_id":"原运行ID"}`，服务在源/副本实际接收锁内核对最新Run，过期返回409 rejected；省略body仍兼容既有开发接口。相同内容的未知请求保留持久幂等键，已接收回执先回放，不因后续最新Run改变而二次派发；变更内容不能复用旧键。available_actions只给出当前动作入口，执行时仍复核源码、配置、预算及审批条件。

MK05集成补充：现有read_tool_result新增普通命令stdout/stderr分流回读，仍须同Session的精确Run/call身份；交付验收沿用原固定allowlist，不扩展任意跨会话读取。模型预览发生截断时附回读提示；日志过期/缺失返回明确错误，不用预览补成完整流。日志起始/回执存储故障在工具metadata中明确可见，取消时回执落库失败写错误诊断而不吞掉。

正常关服恢复新增事实：Run级host.shutdown_started载明run_id/shutdown_id；run.interrupted.reason=service_shutdown及resumable=true表示待按恢复条件核查，并非跳过检查承诺。关服后的recovery.claimed为Session事实，绑定source_run_id/resumed_as；旧终态原样保留。用户停止记录Session级run.cancel_requested，其run_id精确对应原运行；GET recovery将该事实纳入高水位并优先显示用户停止（未知工具仍不伪装已解决）。若旧运行已有后继，POST旧Run/cancel返回409，调用者须选择后继Run，不能误报已停止新任务。

旁枝切片复核：创建响应增加`source_execution`（主Run真实执行身份）与`files_captured_at`；文件取证和source_high_water为两次独立观察。source/worktree/Plan可用，其他执行环境明确422；全服务最多4活动旁枝，容量不足接收前422。列表包含可选cleanup_error，清理故障不静默隐藏。source_context提供有界tool_results与latest_failure；用户回传只接受completed答案，指纹必填；读取/回传不重新运行主任务。追问链、选中文字及刷新背景尚待实现。



### 摘要版本与原始证据（MK03，已实现切片）

- `GET /api/sessions/{id}/context-summaries?before=&limit=`：新到旧checkpoint元数据，limit 1–50（默认20），next_before游标；未知Session404。没有摘要返回空列表，不补造。
- `GET /api/sessions/{id}/context-summaries/{checkpoint_id}`：严格同Session；返回覆盖位置、原历史/摘要指纹、policy/provider/model，结构化摘要或保留的旧文本。`source_verified`仅代表事件前缀/终态/策略能核对，`input_verification=matched|claim_missing|projection_changed|unavailable`单独说明原请求输入。`citations_problem`保留不匹配原因，不把失败解析为正常摘要。summary最多24,000字符，summary_truncated和原字符数显式；不改变数据库原文。
- `GET /api/sessions/{id}/context-summaries/{checkpoint_id}/messages/{index}?offset=&limit=`：原摘要消息投影的JSON文本分页，offset≥0，limit 1–12,000（默认8,000）；返回history_hash/输入核对状态和next_offset。不是原文件正文，也可能包含生成摘要前已裁剪的工具预览。来源不可核对409，越Session/未知消息404，分页越界422。GET不写事件、不调用模型、不读取当前源码。

MK09页面范围接线（2026-10-26）：待处理页`#/attention?project=<project_id>`调用既有项目attention端点，无query时调用全局端点。刷新/直接打开保持范围；切换中不展示旧项目动作，404/网络失败不自动改成全局结果。未新增或修改HTTP契约。


### MK06结构化问题API（2026-10-26）

- `GET /api/runs/{run_id}/questions`：返回run_id、questions、waiting。每组含question_id、session_id、run_id、call_id、revision、冻结questions以及waiting/answered/closed/expired/late；已接收答案含answers。读取不生成问题，不自动续跑；进程内等待不存在时不呈现为可提交等待。
- `POST /api/runs/{run_id}/questions/{question_id}/answers`：本机同源+Idempotency-Key，严格body `{answers:[{choice?:string|null,text?:string|null}],question_revision:1}`；1–3条与冻结问题逐项对应。返回question_id/run_id/status(delivered或late)/duplicate；delivered仅表示接收，不证明已进入下一模型。相同内容可重放，不同内容/旧版本/关闭/明确停止/活跃事实却无等待者409，跨Run404、无效选项/字段/长度422。关闭问题不能答，已终态且未关闭的孤立问题仅存late，不重新派发。
- `GET /api/attention`与项目attention新增questions和counts.questions，limits.questions与approvals_limit相同。每项稳定question:<id>身份、原Run/Session/问题版本、可操作状态与对话链接。回答页只调用上述端点，普通工具审批保持原独立协议。

历史问题来自唯一事件记录；本阶段服务重启后不自动重建等待，不把问题卡的存在当仍可回答。OpenAPI与TS类型已同步。


### MK20 只读任务图 API（2026-10-26）

- `GET /api/runs/{run_id}/graphs`返回仅精确root Run定义的图；`GET /api/graphs/{graph_id}`返回事实投影。
- `POST /api/runs/{run_id}/graphs`接收`{title,nodes:[{id,kind:"readonly",goal,depends_on:[]}]}`，201仅表示保存定义；最多16节点，重复id/未知依赖/环/写入kind均422。
- `POST /api/graphs/{graph_id}/advance`显式开始，依赖完成后自动派发；`POST .../cancel`冻结新增派发并等待真实收尾。图有active节点时显示cancelling，终态取消不改写已完成图。
- 写接口要求本机同源和内容绑定Idempotency-Key，绑定精确root/graph路径身份；重复创建保留原图、同键异内容409。未知Run/图404。结果包含nodes[].claim.run_id及已完成result.child_session_id，不能在准备期猜Session；output_truncated/output_length说明节选。unknown请求保留原键，查询图不引发执行。

任务图OpenAPI/TS类型与网页产物已统一生成，12项HTTP/契约/分发及built真实API页面旅程通过；配置不可用在派发前明确409 rejected，不留下未知接收。此证据不等于完整MK20或真实模型效果验收。

### MK-11 只读批次事件与队列增量（2026-10-26）

无新增写HTTP入口。原Run详情/快照tools及SSE复用同call_id，tool.prepared/completed/abandoned/outcome_unknown可附`batch:{batch_id:"s1b1",position:0,size:2,parallel:true}`；batch_id只在本Run内有意义，position为原请求内该连续组的零基位置。parallel表示使用可信读取调度，同组单项也共用4槽，不保证每项在同一时刻运行。非可信工具组使用parallel=false并单项顺序执行。新增tool.started事实绑定call_id/name/batch，表示该项已获读取槽、进入派发；投影合并原prepared参数并显示running。prepared仍可能排队，abandoned+dispatched=false明确没有执行；单项is_error和unknown独立呈现，不能把整组统一显示成功。结果按原请求顺序回填；停止后已执行结果仍可在终态之前补齐。

`GET /api/queue`及项目队列同一limits增加`readonly_tools:4`，counts增加active_readonly_tools/waiting_readonly_tools，均为当前进程实际所有权；不新建持久任务或第二事件库，原根任务/模型槽字段含义保持。Vue事件订阅识别tool.started，工具活动显示等待/执行中/未执行和批次位置。


### MK-21 原生文件执行切片（已接线，平台待验证）

`POST /api/sessions/{id}/runs`增加execution_profile=native、native_profile=read-only|workspace-write（首轮默认workspace-write）、native_backend=seatbelt|bubblewrap（首轮省略则冻结本机后端）；execution_target必须worktree。原生字段与local/sandbox混用、原生请求附sandbox_image/Skills、Session后续改变权限或后端、不可用后端均在接收前拒绝，不回退local。普通本地请求未指定的新增字段从幂等请求体剔除，历史指纹保持可重试。Session响应execution_profile=native及native_profile对象包含kind/profile/backend，sandbox_profile仍为空。

`GET /api/runtime/native-capabilities`返回platform、backends（kind/available/binary/detail/support）、default_backend、profiles及verification=unverified/verification_detail；仅探测程序存在，不执行工具，也不证明隔离有效。该端点承接本切片环境选择；完整execution-backends矩阵、策略修订/扩权审批、网络与命令支持仍按原MK-21计划后续实现。

原生工具目录只开放文件与Runtime元数据操作，readonly目录无write_file/edit_file；未知写入沿原outcome_unknown投影停驻；清理失败增加Session事实native.cleanup_failed，载荷run_id/error。文件读取source_evidence沿用原结构并带native_worker=true。页面需展示未知/不可用原因、冻结权限和当前支持范围；不得用探测passed代替平台验收。



原生文件工具回执补充：write_file发布后可观察到目标被替换、原inode内容变化或无法确认时，worker内部path_changed映射为现有ToolResult metadata.outcome_unknown，Host保存tool.outcome_unknown并停驻；不是普通可重试失败。正常回执仅代表本次观察点核对成功，不保证之后外部编辑不会改变文件。HTTP/OpenAPI字段不变。


### MK08D：只读核查内置文件结果（2026-10-26）

`POST /api/runs/{run_id}/recovery/reconcile`要求本机同源和Idempotency-Key（空JSON正文可用）。只接受已终态、当前无执行所有者的本机源/工作副本Run，持有原会话与实际执行副本协调后只读核查；不存在404、仍运行/忙碌/不支持环境409、来源不符403。返回`{run_id,checks,unresolved}`，不会创建后继或派发模型。相同请求键回放原回执，未知回执仍保留原键；成功后的新请求可按新观察重新核查。

checks记录包含`source_run_id/call_id/unknown_event_id/name/operation_id/source_high_water/status/reason/observation_digest`及可用的`observed_file`（exists/sha256/mode/size）、`observed_checkpoint/detail`。status只有verified_after或requires_review；current_matches_before明确不能证明原调用没执行，旧记录缺写前workspace证据、目标外变化、路径/权限/备份失效或非内置写入均保留待判断。原unknown事实和终态不修改。`GET /api/runs/{id}/recovery`只读原子切面增加`outcome_checks`（附at），unknown_tools只含尚未核清的调用；核清后显式Continue仍重新核对当前checkpoint、原Run身份与现有预算/权限，用户停止不因核查变成自动恢复。

恢复页新增显式核查按钮与逐条证据，不自动调用核查/Continue，跨Run迟到响应丢弃。结果含范围说明：证明的是当前目标字节和Git可见工作区状态，不保证忽略文件、外部服务、作者身份或测试成功。Shell/MCP不因核查入口而被执行或重放。



MK08D范围修正：`target_not_git_visible`与`target_hidden_in_index`均为requires_review，页面显示具体限制。新workspace checkpoint附index_visibility，当前源/副本接收和派发沿已有比较拒绝索引隐藏标志变化；旧checkpoint缺此字段不补造，保持停驻等待明确重新建基线。本文的“目标after核清”只覆盖Git真实可见目标；任意ignored文件的持久目标指纹与接收/派发方案仍是后续工作，不能从一次读取推断为已支持。

