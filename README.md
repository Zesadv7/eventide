# Eventide

[![CI](https://github.com/Zesadv7/eventide/actions/workflows/ci.yml/badge.svg)](https://github.com/Zesadv7/eventide/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**一个本地运行的代码 Agent 工作台：理解项目、隔离修复、独立验收、人工采纳。**

Eventide 通过 Web 对话协助你阅读代码和定位问题，支持在独立 Git 工作副本中修改代码。
修复交付使用开始时固定的检查验收候选补丁，最后由你审阅并决定是否采纳。
执行过程、工具结果和验收证据保存在本地，可在页面刷新后继续查看。
进度连接断开时，页面显示重连倒计时；联网或返回页面后会自动尝试接回最新进度，不会重复提交已经接收的任务。

[快速开始](#快速开始) · [用户指南](docs/user-guide.md) · [架构设计](docs/architecture.md) · [发布版本](https://github.com/Zesadv7/eventide/releases)

## 工作台预览

![Eventide 工作台：项目与会话侧栏、只读代码调查及工具活动](docs/images/workbench.png)

*真实工作台界面，使用临时示例项目和 ScriptedProvider 演示数据，未调用真实模型；不作为模型效果证据。*

## 核心能力

| 能力 | 你可以做什么 |
|---|---|
| 理解与定位代码 | 带行号读取、直接/正则文本搜索（正则依赖本机 ripgrep）、上下文行与来源内容哈希；可选副本独立索引、结构切分与命中源码核对 |
| 并发与隔离修复 | 同项目创建多个会话，使用只读快照或会话独立 worktree，查看排队与运行状态 |
| 验收与人工采纳 | 固定验收条件，失败后有界修正，回查失败证据，查看补丁并明确采纳 |
| 文件改动与撤销 | 精确唯一匹配编辑，歧义不改文件；版本绑定续页；红绿逐行对比与按次撤销，冲突时拒绝覆盖 |
| 扩展与执行追溯 | 按需加载固定版本 Skills、接入 MCP 工具，处理权限审批，查询事件和恢复检查点 |
| 兼容性核对 | 启动时核对前后端契约版本；不匹配时明确提示升级并阻止写入，网络故障不误报为版本问题 |

| 可读故障报告 | 从已保存事件事实生成中文摘要：发生什么、停在哪、完成了什么、哪些结果未知、下一步；JSON/Markdown 可下载，不自动外发 |

此外支持项目/安装级偏好记忆、调用用量与预算记录，以及可选 Docker 执行沙箱。
模型遇到可恢复的网络错误、限流或不完整流时，主任务同一步内最多尝试 10 次；等待释放模型名额，每次消费单独记录并受原预算约束。页面显示原因、下一尝试与倒计时，刷新不会重置等待。认证、余额不足和协议错误保留诊断并停止，工具不会因半截回答而执行。
功能的启用方式和组合限制见 [用户指南](docs/user-guide.md)。

## 快速开始

需要 **Python 3.10+、Git 和 [uv](https://docs.astral.sh/uv/)**，并准备可用模型的 API Key。
用户运行时不需要 Node.js；默认本地执行也不需要 Docker。

```bash
git clone https://github.com/Zesadv7/eventide.git
cd eventide
uv sync --locked
uv run eventide-server --host 127.0.0.1 --port 8000
```

打开 **http://127.0.0.1:8000**，进入工作台：

1. 在设置中填写 Provider、模型、Base URL 和 API Key；可显式检查连接后保存。
2. 添加服务电脑上的本地项目目录，创建对话。
3. 首次体验建议选择 **Plan** 模式，发送一个只读任务，例如：

   > 请阅读 README 和入口代码，说明项目如何启动，并引用相关文件与行号。不要修改文件。

需要自动修改时，可选择独立 worktree；需要固定检查和补丁采纳流程时，进入“修复交付”。
独立 worktree 首次创建要求 Git 项目有可信 HEAD、源目录干净。

支持 Anthropic Messages、OpenAI-compatible Chat Completions 和 OpenAI Responses。
具体模型的工具与参数支持按能力校验；页面保存的密钥在本地状态中加密存储，不要写入 Git。

其他安装方式、状态备份及 [Docker 部署](docs/user-guide.md#docker-部署与执行沙箱) 见用户指南。

## 从调查到补丁采纳

添加项目 → 配置模型 / Skills → 代码调查 → 独立副本修改 → 固定检查验收 → 审阅补丁 → 人工采纳。

修复交付最多验收三次候选；达到轮次上限仍未通过、超限或出现未知工具结果时停止并保留证据。
验收绑定对应补丁和源码检查点，不能用修改前的结果证明新候选通过。
独立验收的长日志在配额内脱敏保存，可按 stdout/stderr 分页回查；超额或过期会明确提示。
采纳前再次检查源项目与候选，不自动提交、合并或推送。

## 设计概览

```mermaid
flowchart LR
    Web[Vue 工作台] --> API[FastAPI · HTTP / SSE]
    API --> Host[RuntimeHost]
    Host --> Model[Provider adapters]
    Host --> Executor[ToolExecutor / PolicyEngine]
    Executor --> Workspace[源码 / 快照 / worktree]
    Host --> Store[SQLite Event Log]
```

- **执行**：Python / asyncio / FastAPI / Pydantic 2；工具调用统一经过契约校验和权限策略。
- **界面**：Vue 3 / TypeScript / Vite / Router / Pinia，由 FastAPI 同源托管构建产物。
- **状态**：SQLite WAL 与追加式事件记录；队列、审批、用量和恢复复用同一 Runtime。
- **扩展**：Skills、官方 MCP SDK；FTS5 / RRF 检索，可选本地语义模型与重排。

MCP 调用中超时、断连或取消导致结果未知时，页面明确提示并停驻，不自动重发可能已执行的操作。

详细的所有权、并发、恢复及取舍见 [架构](docs/architecture.md) 和 [设计决策](docs/decisions.md)。

## 验证与当前边界

工程验证覆盖后端、类型与静态检查、Vue 组件、真实 API 浏览器旅程、真实 Docker 和安装包分发。
最近一次实现验收见 [GitHub Actions](https://github.com/Zesadv7/eventide/actions/runs/39644102821)，
覆盖率为 **93.03%**；工程门禁与真实模型效果分别报告。

历史 GLM 冻结任务回归为 **42/60**，另一组独立理解/定位任务为 **22/24**。
两者对应不同任务集与冻结条件，不能合并成通用成功率，也不代表后续增量提高了模型效果。
完整条件、历史失败和提高预算后的真实项目结果见 [实验记录](docs/architecture.md#真实模型实验记录)。

使用前需要了解：

- 当前面向本机、单用户、单服务进程。新接收任务在进程意外退出后，可从已确认的执行边界自动续做，保留原模型、工作副本与预算；旧历史、待审批、未知工具结果和源码变化仍停驻。正常关闭服务已记为取消的任务暂不自动恢复。
- 模型仍可能给出错误事实、引用或不完整修复，固定测试的覆盖范围也需要人工核对。
- worktree 隔离代码修改；本地 Shell 仍以当前用户权限运行。
- 可选 Docker 沙箱关闭工具网络，禁用 Skills / MCP / worker / 索引；候选通过文本补丁导出后手动处理。
- 用量区分 reported / estimated / unknown；没有价格时费用未知，目前没有已证实的 Token 或费用改善。

当前 master 包含 0.5.0 发布后的文本搜索、验收证据回查、红绿对比与内置文件撤销等增量。
下载 [0.5.0 Release](https://github.com/Zesadv7/eventide/releases/tag/v0.5.0) 时以该版本说明为准。

## 深入文档

| 文档 | 内容 |
|---|---|
| [用户指南](docs/user-guide.md) | 页面操作、模型与状态、Skills、恢复、部署和开发命令 |
| [架构](docs/architecture.md) | 执行链、事件存储、并发隔离、交付及实验依据 |
| [API 契约](docs/api-contract.md) | HTTP / SSE、请求身份、错误和前后端约定 |
| [设计决策](docs/decisions.md) | 长期取舍及已被替代的设计 |
| [失败案例](docs/failure-cases.md) | 可追溯的失败、原因、修复和复验 |

开发评测和交付示例见 [examples/](examples/)，维护约定见 [AGENTS.md](AGENTS.md)。

## License

[MIT](LICENSE)

本次 Maka 借鉴升级在独立候选分支实施，完整进度与尚未交付项见 [升级状态](AGENTS.md#maka-upgrade-status)。目前聊天支持运行中补充（在下一次模型请求前加入，迟到只留记录），并提供“待处理”页面汇总可读审批、最近失败和待采纳候选；记忆保存回执可直接在聊天中查看和撤销。自动恢复、工具发现、原生隔离等仍有后续切片，不能把已通过的定向测试当整套升级验收。

工具活动现在能直接展开本地源码读取和文本搜索证据：文件版本、实际读取行范围、是否还有下一页，以及搜索命中和上下文、未搜索或跳过的文件数量。文件后来发生变化不会改写历史证据，页面会提醒它仅代表当时的读取；没有结构化信息的旧记录保留原始结果。此切片已验证实际 API、刷新和窄屏展示，完整升级仍按 AGENTS.md 的进行中事项推进。
