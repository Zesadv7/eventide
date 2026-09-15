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

### RunBody 扩展 `attachment_ids`、附件生命周期与多模态 —— 已实现

上传（JSON，不引入 multipart 依赖）：

```json
POST /api/sessions/{id}/attachments
{"name": "notes.py", "media_type": "text/x-python", "content_base64": "..."}
→ 201 {"attachment_id": "att_<12hex>", "name": "notes.py", "size": 1234,
       "media_type": "text/x-python", "kind": "text", "used": false}
```

约束：单文件 ≤10MB；base64 或 media type 非法、声明为文本但不是 UTF-8 时返回 422；会话不存在返回 404。`media_type` 可省略并按文件名推断。内容存储于状态根 `attachments/{session_id}/{attachment_id}`，不进入 Workspace 或 Git。

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
