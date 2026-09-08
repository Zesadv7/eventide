# Nexus Agent

Nexus Agent 是一个面向工具调用场景的 Agent Runtime。它负责组织模型调用、工具执行、权限审批、MCP 连接、会话状态、运行追踪与评测，让同一套 Agent 能力同时运行在 CLI、HTTP API 和 Web 控制台中。

主要能力：

- async-first Agent loop，同一会话串行、不同会话并发；
- Anthropic Messages、OpenAI-compatible Chat Completions 和 OpenAI Responses API；
- `ALLOW / ASK / DENY` 权限决策与 CLI、Web 一次性审批；
- MCP stdio 与 Streamable HTTP 工具发现和调用；
- SQLite session、run、event 持久化与 JSONL trace 导出；
- scripted provider 离线评测与真实模型 live eval；
- FastAPI、SSE 事件流和原生中文 Web 控制台。

![Nexus Agent Web 运行控制台](docs/assets/web-console.png)

## 环境要求

- Python 3.10–3.14；
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境；
- 只有连接真实模型时才需要 API Key，帮助命令、单元测试和离线评测不需要网络或密钥。

## 安装

使用 uv：

```bash
uv sync --extra dev
```

不使用 uv：

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate

python -m pip install -e ".[dev]"
```

## 五分钟运行

无需 API Key 即可检查 CLI、运行测试和离线评测：

```bash
uv run python -m nexus_agent --help
uv run pytest -q
uv run nexus-agent eval evals/smoke.yaml
```

启动 Web 控制台：

```bash
uv run nexus-agent serve --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。在右上角的模型配置窗口中填写 Provider、模型名称、Base URL 和 API Key，可以先检查连接，再保存并应用。配置写入 `.nexus/nexus.db`，API Key 使用 `.nexus/secret.key` 或 `NEXUS_SECRET_KEY` 加密；这些文件默认不会提交到 Git。

## 配置真实模型

可以通过 Web 控制台保存配置，也可以复制环境变量模板：

```bash
cp .env.example .env
```

核心变量：

```dotenv
NEXUS_PROVIDER=openai_compatible
NEXUS_API_KEY=replace-me
NEXUS_BASE_URL=https://provider.example/v1
NEXUS_MODEL=tool-capable-model
```

`NEXUS_PROVIDER` 支持：

- `anthropic`：Anthropic Messages API；
- `openai_compatible`：兼容 OpenAI Chat Completions tool calling 的服务；
- `openai_responses`：OpenAI Responses API。

运行一次任务或进入交互模式：

```bash
uv run nexus-agent run "概括当前仓库的结构" --json
uv run nexus-agent chat
```

使用真实 Provider 运行评测：

```bash
uv run nexus-agent eval evals/smoke.yaml --live
```

## 使用 MCP

复制示例配置并启动一次任务：

```bash
cp mcp.example.json mcp.json
uv run nexus-agent run "调用 demo MCP echo 工具"
```

`mcp.json` 支持 stdio 和 Streamable HTTP。stdio 服务只会收到配置中显式列出的环境变量，发现的工具统一命名为 `mcp__server__tool`。本地 `mcp.json` 默认不会提交到 Git。

## 安全提醒

- 不要把 API Key 写入命令行、`mcp.json`、代码或提交记录；
- Web 模型配置的写入、清除和连接检查只接受本机回环请求；
- 本地 Shell 执行器会在宿主机运行命令，权限策略不等于操作系统沙箱；
- `.nexus/` 包含本地配置、数据库和 trace，不应提交或公开。

## 进一步阅读

- [系统架构](docs/architecture.md)
- [架构决策](docs/decisions.md)

## License

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
