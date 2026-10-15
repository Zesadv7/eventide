# 前端接口契约（Web UI ↔ Runtime API）

本文是 Web 前端依赖的全部 HTTP 接口契约与实现状态。前端按此开发并对缺失接口优雅降级；后端按此补齐。实现状态：`已实现` / `预留（待后端）`。

## 现有接口（前端已依赖）

### HTTP 统一错误 —— 已实现

HTTP 状态码保持原义，JSON 错误包含 `code`、可读 `message` 与兼容的 `detail`。例如不存在的 Run 返回 404 和 `{"code":"not_found","message":"Run not found","detail":"Run not found"}`。已有客户端可以继续读取 detail，新客户端优先用 code 判断类别并展示 message。405 保留 Allow 等协议响应头。

常用 code：400 bad_request、401 unauthorized、403 forbidden、404 not_found、405 method_not_allowed、409 conflict、413 payload_too_large、422 validation_error、429 rate_limited、500 internal_error、503 service_unavailable；来源守卫仍使用 invalid_origin。结构化 detail 的 message 为通用说明，字段级信息保留在 detail 列表的 loc/msg/type 等项中。为避免密钥或附件内容泄漏，校验错误不回显 input 和 ctx；已知模型密钥继续脱敏。内部异常不返回原始异常内容。OpenAPI 统一声明 ErrorResponse。

模型配置连接检查仍返回 HTTP 200 及 success/error_type/message/detail，不属于 HTTP 错误包裹。SSE 与导出成功响应保持原媒体类型；在流已经开始后的传输失败不能改写成新的 JSON HTTP 响应。

### 浏览器写请求与运行幂等 —— 已实现

工具调用错误的canonical metadata含稳定code（invalid_arguments、invalid_tool_contract、unknown_tool、permission_denied、timeout、execution_failed、outcome_unknown）。第一次参数错误交回模型修正，再次参数错误结束该Run；错误结果仍保存，不自动重放已执行工具。截断响应的调用不会派发，其未执行事实补齐历史消息。

写请求的Origin必须匹配本机服务源，cross-site/null/异常端口和DNS-rebinding来源拒绝；本机非浏览器客户端可省略Origin。没有放开CORS，开发前端应使用同源代理。

Run与Continue接受可选`Idempotency-Key`（1–128个可见ASCII字符）。同Session/操作/键确定一个持久Run身份，内容hash写入admission。重试相同内容返回原202接收凭据，不新增消息、调用或turn；相同键不同内容409。接收尚未持久化的竞争请求可返回busy，客户端保留相同键稍后重试。幂等接收不等于自动重放工具，未知结果仍停驻。Skills 导入和配置的幂等契约见下文；Delivery 随对应控制面接入。

### 模型生成参数 —— 后端及 Vue 已实现

- `GET /api/generation/defaults?model=...` 返回 `model_identity/defaults/session/capabilities`，身份绑定Provider、endpoint和模型。
- `PUT /api/generation/defaults` 接受 `{model, generation, capabilities?}`；显式能力声明标为declared，不冒充已验证。
- `GET/PUT /api/sessions/{id}/generation` 读取/替换该模型下的会话覆盖，PUT不允许重新声明模型能力。
- `POST /api/sessions/{id}/runs` 新增 `generation`：temperature、top_p、max_output_tokens、reasoning_effort、thinking。省略继承，null恢复默认，0有效；额外字段/非有限值/范围错误422。
- 写配置仅本机来源；支持情况未知时拒绝显式采样参数。接收时冻结，Continue相同接入继承；已有调用不受后续设置影响。尚无自动运行探测或按任务预设。

### 请求用量账本 —— 后端及 Vue 已实现，费用与预算待U2/U3

`GET /api/runs/{id}/usage`、`GET /api/sessions/{id}/usage` 返回独立模型尝试的 `calls/reported_totals/estimated_totals/unknown_attempts/incomplete_attempts/complete`。默认包含持久parent_run_id关联的子Run；`include_children=false`只取所选自身。Session汇总覆盖全部历史，不受Run列表分页限制；每个真实调用只计一次，不重复加入终态或worker汇总。

无usage是unknown，不是零；缓存与推理Token为细项，不能再加到总量。账本暂不报价，原`usage`字段保留兼容；需要完整性判断时使用新账本。历史响应只能回退统计已观察响应，complete保持false。取消和失败请求无返回usage时保留未知。

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

普通 Run 与 Continue 返回 202 前，Host 在同一事务写入 Run/turn 身份、`run.admitted`、用户消息或恢复事实及 checkpoint；返回后 GET 可立即查询，取消可以早于后台执行。接收和执行均占用 Workspace；同 Workspace 并发请求或停驻冲突返回 409，模式/模型/附件校验失败返回 422（Continue 恢复校验失败为 409）。重启只将未结束 Run 停驻，不自动执行。

Continue 保持原 Plan，只在相同 Provider 和 Base URL 下沿用选定模型；连接变化则使用当前默认模型。原 Agent 模式恢复为 Auto，自动审批不跨 Run。附件的归属以完整 Session ID 为准，DELETE 对 canonical 引用返回 409，不仅依赖文件 used 标记。

`reason=token_budget` 表示已观测的输入+输出用量达到 `run_token_limit`，默认 0 关闭。摘要成功响应也计入，摘要事件不作为用户消息显示。单次响应可越过上限，Continue 重置预算；没有用量的失败请求不估算。前端用量继续消费 Run 的 usage，停驻区显示 Token 软上限原因。

### 受控只读子任务 —— 已实现（显式开启）

Workspace capabilities 增加 readonly_workers（enabled/roles/max_concurrency/max_depth/max_tasks_per_run）。父 Run 的 worker.started/completed 事件关联 child_run_id，子 Run 查询和 history 返回 parent_run_id/worker_role/snapshot_id。子Session默认归档且不能接收普通Run、Continue或附件。父usage含子消费，子usage仅自身；跨父子汇总需按exclusive模型事件计算。

### 交付、执行配置与未知结果 —— 已实现

交付首版由CLI deliver控制面提供prepare/run/verify/show/apply，暂不新增HTTP写入接口。原Run/Session/SSE/export可读取Writer与control Run证据；delivery事实与patch artifact hash存同一Event Log。Writer的execution_config在后续Run/Continue生效；图片/PDF契约不变。control Session归档且不能用于普通模型执行。

新增canonical tool.outcome_unknown与终态reason=unknown_tool_outcome；它保留未确认调用，不伪装成tool.completed，相关Workspace阻止普通Run、Continue和交付写入。前端显示“工具结果未确认”，不能将其展示为已停止或允许继续。未知状态不因abandon或重启清除。导出仍包含完整canonical证据，默认脱敏Trace只派生安全元数据。


## Vue 客户端兼容规则

Vue HTTP 客户端规范化列表/详情返回的 Run `id` 为内部 `run_id`；202 接收凭据仍直接使用 `run_id`。Run 列表是最近一页按时间正序排列，最新项位于末尾。工具待审批期间，`/messages` 可能因工具历史尚未配对返回 409；客户端暂留上次可见消息，继续查询 Run/Session，不能因此隐藏审批或停止按钮。

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
