# Eventide

Eventide v0.3 是面向软件工程项目的 Workspace Agent Runtime。RuntimeHost 管理 Workspace 与 Session，运行事实统一写入 Event Log；消息、运行状态和模型上下文都从日志投影生成。CLI、HTTP API 和现有 Web 控制台使用同一执行主链。

主要能力：

- async-first Agent loop，同一 Workspace 串行、不同 Workspace 并发；
- Anthropic Messages、OpenAI-compatible Chat Completions 和 OpenAI Responses API；
- `ALLOW / ASK / DENY` 权限决策与 CLI、Web 一次性审批；
- MCP stdio 与 Streamable HTTP 工具发现和调用；
- Workspace 级 Skill 发现与按需加载；
- SQLite 追加式 Event Log、持久上下文 checkpoint 与 JSONL 导出；
- 中断检测、停驻与用户主动 Continue，不自动重放工具副作用；
- scripted provider 离线评测与真实模型 live eval；
- FastAPI、SSE 事件流和原生中文 Web 控制台。

![Eventide Web 工作记录页（离线示例）](docs/assets/web-console.png)

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
uv run python -m eventide --help
uv run pytest -q
uv run eventide eval evals/smoke.yaml
```

启动 Web 控制台：

```bash
uv run eventide serve --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。左侧“添加工作区”可注册这台电脑上的已有项目目录，成功后直接进入该 Workspace；工作记录可通过右侧“⋯”或右键重命名、归档/恢复，只有没有运行历史的空记录可硬删除。“显示已归档”用于找回归档记录。通过右上角“设置”填写 Provider、模型名称、Base URL 和 API Key，可以先检查连接，再保存并应用。模型配置由同一 Host 的所有工作区共用。配置写入状态根的 `runtime.sqlite`，API Key 使用同目录 `secret.key` 或 `EVENTIDE_SECRET_KEY` 加密。

默认状态根：Windows 为 `%LOCALAPPDATA%\Eventide`，macOS 为 `~/Library/Application Support/Eventide`，Linux 为 `$XDG_STATE_HOME/eventide`（未设置时 `~/.local/state/eventide`）。`EVENTIDE_STATE_DIR` 可覆盖；相对路径相对于启动工作目录解析。状态目录独占：`serve` 运行期间，使用同一状态根的另一 CLI/Host 会明确报错，首版没有跨进程客户端协议。

v0.2 历史可在关闭旧进程后显式导入；源数据库保持只读，导入在新库中要么全部成功、要么不写入：

```bash
uv run eventide migrate-v02 /path/to/project/.nexus/nexus.db --workspace /path/to/project
```

省略数据库路径时，默认读取目标 Workspace 下的 `.nexus/nexus.db`。导入保留 Session、消息、Run、事件以及未含密钥的 Provider 配置；旧密文依赖旧状态根的密钥，出于安全原因不会复制，需要在 Web 设置中重新填写 API Key。重复 Session/Run 身份会在导入前拒绝，不覆盖现有历史。测试和离线评测不需要 API Key。

## Workspace 与 Continue

```bash
uv run eventide workspace add /path/to/project
uv run eventide workspace list
uv run eventide workspace show ws_example
uv run eventide run "检查项目测试" --workspace /path/to/project --cwd packages/api --json
uv run eventide chat --workspace ws_example
uv run eventide serve --workspace /path/to/project
uv run eventide continue session_example --json
uv run eventide abandon session_example --json
uv run eventide plan show session_example --json
uv run eventide export run_example ./eventide-run.jsonl
```

省略 `--workspace` 时使用当前目录；Git 仓库内的子目录归一化到仓库根，非 Git 目录也可运行。每个 Session 固定绑定一个 Workspace 和其中的 working directory；`run/chat --cwd` 可显式选择，若 `--workspace` 本身传入仓库子目录则默认把该子目录作为 cwd。Shell 与文件工具从 cwd 运行，并把它作为文件访问边界；Git checkpoint 仍覆盖整个 Workspace。根目录 `AGENTS.md` 加入模型上下文（最多 4,000 字符），`mcp.json` 和 `skills/` 按 Workspace 加载。生产基础工具为 `bash/read_file/write_file/edit_file/glob/compact/read_tool_result/todo_write`；发现有效 Skill 时再增加只读的 `load_skill`。旧 task graph、worktree、teammate 等接口仍可导入，但不进入新 Runtime 默认工具目录。`workspace remove <id>` 只移除没有 Session 的注册记录，不删除项目文件。

Host 重启会把未结束的 run 标记为 `interrupted`，Session 显示为 `parked`；单次 run 用尽步数预算（默认 30 步，`EVENTIDE_MAX_STEPS` 可调）时同样停驻，可由用户 Continue 继续，不会整轮失败；运行中被用户停止时也会先记录 checkpoint 再停驻，因此同样可以继续。存在任务计划时，同一条进行中的任务连续占用步数（默认 12 步，`EVENTIDE_TASK_MAX_STEPS` 可调，设为 `0` 关闭）也会停驻并点名该任务，避免一条卡住的任务烧光整个 run 的预算；这是 run 内的软上限，不改变 `max_steps` 这个硬上限，Continue 后重新计数。Continue 必须由用户发起；它检查 Git HEAD、暂存/未暂存改动及未跟踪文件是否匹配最后可信 checkpoint，并拒绝存在未知 Bash、MCP 或写工具结果的历史。通过检查后创建新 turn/run，旧用户消息不会重复写入，未完成的只读调用会标记为 abandoned。非 Git、未提交过的 Git 仓库、包含 submodule 的项目或缺少可信 checkpoint 时不支持 Continue。检查范围是 Git 可见源码，不覆盖 ignored 文件或外部系统状态。Continue 无法通过时，可以使用 Web 的“放弃恢复”或 `eventide abandon` 保留历史并解除停驻；结果未知的工具不会被视为成功或重新执行。

上下文摘要只覆盖已结束的 turn，原始事件不变；达到模型输出上限的残缺摘要不会保存为 checkpoint。Prompt、模型正文和工具结果不会因日志展示限长而在执行前被静默截断；工具始终使用 Provider 返回的原始参数，审计事件中的敏感字段使用脱敏副本。预算按 system、messages 和完整工具目录的序列化请求计算。超过 12,000 字符的单条工具结果会先在模型请求中变成带 run/call 引用的头尾预览，模型可用只读 `read_tool_result(run_id, call_id, offset, limit)` 分页取回任意区间；TodoWrite 的历史整表参数也会在请求侧省略，只保留下方当前计划。canonical Event Log、审计导出和工作记录仍保留完整结果与计划。摘要之后仍超预算时，当前 turn 内较早的工具结果会继续折叠为占位串（保留最近三条预览或原文），并记录 `context.trimmed`。仍无法在预算内保留时返回 `context_overflow`，不会静默丢弃当前交互。预算单位保持为序列化请求的字符数。模型输出达到 `max_tokens` 被截断且没有工具调用时，run 以 `failed` 结束并提示提高 `EVENTIDE_MAX_TOKENS`，不会静默报成功。

HTTP 保留原有 session/run/approval/SSE 路由，新增 Workspace 注册、查询、无会话记录移除、项目会话列表、session/messages 和 Session Run History 查询。`POST /api/sessions` 可传 `workspace_id` 和 Workspace 内的 `working_directory`；空请求继续绑定启动项目根目录。`PATCH /api/sessions/{id}` 可设置显式标题或归档状态；默认列表隐藏已归档 Session，传 `include_archived=true` 可查看。`DELETE /api/sessions/{id}` 只删除没有 Run 或事件历史的空 Session，有历史的工作记录必须归档。Run History 默认返回最近 100 条，可用 `limit`（最多 200）和 `before=<run_id>` 向前翻页；Web 提供“加载更早记录”。`GET /api/runs/{id}/export` 流式下载 canonical JSONL；CLI `export` 默认拒绝覆盖已有文件，显式 `--force` 才替换。`POST /api/sessions/{id}/continue` 先完成恢复校验并持久化新 Run，再返回 `202`、`run_id` 和 `status=accepted`；验证失败返回 409，执行继续通过 Run 查询与 SSE 观察。同 Workspace 的并发 HTTP 请求返回 409。Web 长任务状态显示已观察到的模型步数、正在执行的任务、运行时长和距最近持久事件的时间，并提供按 Run 停止；不虚构无法由事件证明的完成百分比。

## Web 工作记录页

左侧选择 Workspace 和持续存在的工作（Session），正文自上而下展示工作目标与状态、任务计划、最近成果与工作经过，任务计划仅在该工作存在计划时出现；“本次执行结束”不代表 Session 被关闭。首次浏览不自动创建空 Session，还没有任何工作区时提示先添加工作区，点击“新建工作”或首次提交目标时才创建。刷新会恢复该 Workspace 上次选择的工作。

工作经过按用户意图及其 Continue 链分章，较早章节按需加载，工具操作默认折叠。工具请求与结果配对后展示对象和执行状态；“操作完成”不额外宣称测试全部通过。任务计划面板展示当前清单与每项的证据，completed 默认折叠，证据措辞始终是"发生过什么"。原始 Events、模型协议、工具参数和 checkpoint 通过临时右侧详情查看；整章入口在“工作经过”标题处，只有审批和需要注意的块（中断、放弃恢复、MCP 异常等）自带“查看详情”按钮。结果支持标题、列表、代码和安全链接的 Markdown 子集，以及复制；原始 HTML 不执行。

停驻时状态行与恢复区会说明原因（如“步数预算用尽”、“执行已停止”、“服务中断”），原始错误文本仍通过“查看中断详情”查看。输入区切换为 Continue 或放弃恢复操作，通过后立即展示接续过程，工作经过中的接续说明会点名正在继续的任务；校验失败保留停驻状态与原因。仅最新停驻工作提供恢复操作。审批直接在正文提供“本次允许”和“拒绝”。运行中的工作可以单独停止，停止后按中断语义停驻并保留记录。同 Workspace 忙碌时不能再次提交；历史仍可浏览。正文独立滚动，底部操作区保持可达，小屏也可切换 Workspace、新建工作和查看详情。

Web 运行不依赖 Node 或前端构建。开发时可用 Node.js 22+ 运行 `node --test tests/web.test.mjs`；pytest 检测到合适的 Node 时也会执行这些离线投影和传输检查。另有 `node tests/web_browser.cjs` 浏览器验收，需预先提供 Playwright 与 Chromium；`EVENTIDE_PLAYWRIGHT_MODULE` 可指定已安装的包路径，`EVENTIDE_BROWSER_CHANNEL=msedge` 可使用已安装的 Edge。浏览器测试使用内存 HTTP/SSE 样例，不读取用户数据库或连接模型，截图写入忽略目录 `.task_outputs/`。

## 配置真实模型

可以通过 Web 控制台保存配置，也可以复制环境变量模板：

```bash
cp .env.example .env
```

核心变量：

```dotenv
EVENTIDE_PROVIDER=openai_compatible
EVENTIDE_API_KEY=replace-me
EVENTIDE_BASE_URL=https://provider.example/v1
EVENTIDE_MODEL=tool-capable-model
EVENTIDE_MODEL_TIMEOUT=300
EVENTIDE_MCP_TIMEOUT=30
EVENTIDE_COMMAND_TIMEOUT=120
```

`EVENTIDE_PROVIDER` 支持：

- `anthropic`：Anthropic Messages API；
- `openai_compatible`：兼容 OpenAI Chat Completions tool calling 的服务；
- `openai_responses`：OpenAI Responses API。

运行一次任务或进入交互模式：

```bash
uv run eventide run "概括当前仓库的结构" --json
uv run eventide chat
```

使用真实 Provider 运行评测：

```bash
uv run eventide eval evals/smoke.yaml --live
```

## 使用 MCP

复制示例配置并启动一次任务：

```bash
cp mcp.example.json mcp.json
uv run eventide run "调用 demo MCP echo 工具"
```

`mcp.json` 支持 stdio 和 Streamable HTTP。stdio 服务只会收到配置中显式列出的环境变量，发现的工具统一命名为 `mcp__server__tool`。每个服务独立连接；单个服务离线或超时会记录在工作经过中，但不会阻断其他 MCP 或内置工具。本地 `mcp.json` 默认不会提交到 Git。

## 使用任务计划

生产 Runtime 会提示模型只为至少三步的复杂工作调用 `todo_write`。每次调用提交完整当前清单，最多 20 项；状态支持 `pending`、`in_progress`、`completed`、`blocked`，且同时最多一项为 `in_progress`。更新写入不可变的 `task.plan_updated` 事件，Session 状态返回最新完整计划；模型请求只注入未完成项和已完成数量。因此计划可以跨 step、Host 重启和 Continue 恢复，但不会把每次历史整表重复占用上下文。

每个计划项带稳定 `id`（`t1`、`t2`……），由 Runtime 分配、随事件持久化，**序号只增不回收**。模型更新计划时回传它保留项的 `id`，因此改写措辞不会改变身份；省略 `id` 的项按内容匹配复用旧身份，仍是新任务才分配新 `id`。引用不存在的 `id`、或在同一份计划里重复使用 `id`，都会得到一条工具错误而不会静默新建。旧 Session 中不含 `id` 的历史计划保持原样，在模型下一次提交计划时自然补齐，历史事件永不改写。

当前进行中的那一项由 Runtime 显式投影为 `active_task_id`（Session 状态与 `run.started.resumed_task_id` 都可见）；计划里没有任何 `in_progress` 时，system 会提示模型标出正在做的一项。停驻后 Continue 的 run 会在 system 中写明正在接续哪一条任务。

标记某项 `completed` 时必须同时给出 `summary`（≤300 字符），说明**做了什么**；后续重复提交计划时可以省略，Runtime 会沿用已保存的那一条。Session 状态另提供 `task_state`：每项任务带首次出现序号、结算序号，以及该任务存续期间真实发生过的工具调用证据（`run_id`、`call_id`、工具名、是否出错，最多 8 条并报告被省略的数量）。

**任务证据只记录"发生过什么"，不表示验证通过**：出错的调用同样计入证据，`completed` 只是调度状态，不替代测试、文件或任何外部验证。system 中只注入最近一条完成摘要，因此计划再长也不会线性占用上下文。

计划是只读展示的：CLI 提供 `eventide plan show <session_id>`，默认人类可读输出（各项状态、active 任务、完成 summary、证据列表），`--json` 直接返回与 Session 状态同构的 `task_plan`、`active_task_id` 和 `task_state`。Web 工作记录页在“已有成果”上方展示任务计划面板：计数为总进度式“N / M 已完成”，仅当存在时追加“进行中”和“受阻”数量，不单独显示“待开始”（待开始任务仍逐条可见）；active 任务高亮，`completed` 项默认折叠进“已完成的任务”分组，summary 和工具证据可按需展开。前端收到 `task.plan_updated` SSE 后重新读取 Session 状态刷新面板，不在前端复制投影规则；计划没有任何人工编辑入口，唯一写入路径仍是 `todo_write`。

这只是当前 Session 的执行计划，不会启动 Subagent、后台任务或依赖图；旧文件式 task graph 仍属于 compatibility layer。

## 使用 Skills

在 Workspace 根目录下创建 `skills/<目录名>/SKILL.md`。文件可使用 YAML frontmatter 提供稳定名称和简介：

```markdown
---
name: code-review
description: Review code for correctness, security and maintainability.
---

# Code review

这里写完整工作说明。
```

每次 Run 开始时，Eventide 会快照有效 Skill：只把名称和简介加入 system prompt，并向模型提供只读的 `load_skill` 工具；模型判断 Skill 相关后才加载完整 `SKILL.md`。工具调用和完整结果继续写入 canonical Event Log，`context.configured` 同时记录 Skill 数量和目录 hash。Skill 始终从 Workspace 根目录解析，不随 Session working directory 改变；运行中修改文件只会影响下一次 Run。

## 安全提醒

- 不要把 API Key 写入命令行、`mcp.json`、代码或提交记录；
- Web 模型配置的写入、清除和连接检查只接受本机回环请求；
- 本地 Shell 执行器会在宿主机运行命令，权限策略不等于操作系统沙箱；
- 状态根包含本地配置、数据库和 trace，不应提交或公开；项目内的 `.eventide/` 仍受 Git 忽略。

## 进一步阅读

- [系统架构](docs/architecture.md)
- [架构决策](docs/decisions.md)

## License

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
