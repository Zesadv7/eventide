# 前端接口契约（Web UI ↔ Runtime API）

本文是 Web 前端依赖的全部 HTTP 接口契约与实现状态。前端按此开发并对缺失接口优雅降级；后端按此补齐。实现状态：`已实现` / `预留（待后端）`。

## 现有接口（前端已依赖）

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
{"context_limit": 50000, "approval_timeout": 60.0, "max_steps": 30, "task_max_steps": 12}
```

前端：上下文指示器分母、审批超时文案、停驻文案。请求失败前端使用默认值。

### Run summary 增加 `usage` —— 已实现

`GET /api/sessions/{id}/runs` 与 `latest_run` 每条 Run 增加 `usage: {"input_tokens": n, "output_tokens": n}`，供用量分区的会话累计。

### `model.request` 事件 payload 增加 `request_chars`、`tool_catalog_chars` —— 已实现

每次模型请求的序列化大小（与上下文预算同一度量）与工具目录大小。前端：composer 上下文指示器 `request_chars / context_limit`；缺失时显示 `--`。

### RunBody 扩展 `mode` —— 已实现

`"auto"`（默认）| `"plan"` | `"agent"`；未知值按 `auto` 处理，不拒绝。

- `plan`：工具目录收敛为只读白名单（`read_file`/`glob`/`read_tool_result`/`compact`/`todo_write`/`load_skill`），system 追加只读声明；写/bash/MCP 不在目录中，模型无法调用，executor handlers 同步受限，幻觉出的写调用不会命中处理器。注意：plan 模式仍会按 Workspace 配置连接 MCP 服务，只是其工具不进入目录。
- `agent`：该 Run 内 ASK 决策自动允许，不再等待用户；审批审计事件照常写入，`approval.resolved` payload 带 `"auto": true` 且 `author="runtime"`（前端时间线显示“agent 模式自动批准”）。
- `auto`：现状行为。

### RunBody 扩展 `attachment_ids` + `POST /api/sessions/{id}/attachments` —— 已实现

上传（JSON，不引入 multipart 依赖）：

```json
POST /api/sessions/{id}/attachments
{"name": "notes.py", "content_base64": "..."}
→ 201 {"attachment_id": "att_<12hex>", "name": "notes.py", "size": 1234}
```

约束：单文件 ≤ 10MB（413 语义，返回 422 与说明）；仅 UTF-8 文本（二进制 422）；base64 非法 422；会话不存在 404。存储于状态根 `attachments/{session_id}/{attachment_id}`。

提交：RunBody 带 `attachment_ids: ["att_..."]`；校验归属（未知/不属于该会话 → 422）；Run 启动时内容内联进用户消息（分隔符 `--- 附件：{name} ---`），单附件超过 512KB 截断并标注。前端：「＋」按钮上传并把 chips 随消息发送；接口 404/失败时「＋」置灰并提示。

### `GET /api/models` —— 已实现

按当前 Provider 配置探测可用模型列表（`eventide/model_catalog.py`）：

```json
{"models": ["model-a", "model-b"], "error": null}
```

失败语义：未配置 API Key 或探测失败时返回空列表与 `error` 说明，不返回 5xx；响应不含任何密钥。前端：模型按钮当前仍打开配置弹窗（探测结果可用于后续把按钮升级为逐消息模型下拉）。

## 预留（前端未依赖，按需补齐）

| 接口 | 说明 |
|---|---|
| `GET /api/sessions/{id}/attachments` | 附件清单，用于跨刷新恢复 chips；当前 POST 已占用同一路径，未实现的 GET 返回 405（Method Not Allowed），E2E 以此断言预留状态 |
| `DELETE /api/sessions/{id}/attachments/{aid}` | 删除附件 |
| 图片/二进制附件 | 需运行时多模态支持后开放 |
