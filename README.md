# Eventide

[![CI](https://github.com/Zesadv7/eventide/actions/workflows/ci.yml/badge.svg)](https://github.com/Zesadv7/eventide/actions/workflows/ci.yml)

Eventide 是以 Web 为唯一用户操作入口的软件工程 Agent 工作台。添加项目、配置模型和 Skills、创建并发对话，在隔离副本中修改、独立验收，再由用户查看补丁并采纳。运行事实写入同一 SQLite Event Log，页面刷新与服务重启都能查询原记录。

后端使用 Python、asyncio、FastAPI 和 Pydantic 2；前端使用 Vue 3、TypeScript、Vite、Router 和 Pinia。单服务进程支持持久任务队列、流式预览、权限审批、Token 与费用治理。实现进度和验收证据以 [AGENTS.md](AGENTS.md) 为准；真实模型效果与离线机制验证分开报告。

## 安装与启动

需要 Python 3.10 以上，推荐 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --locked --extra dev
uv run eventide-server --host 127.0.0.1 --port 8000
```

也可以使用 `uv run python -m eventide --port 8000`。两者都是服务部署入口，仅接受 host、port 和可选初始项目 `--workspace /absolute/project`；不提供终端聊天、任务、检索或评测子命令。

浏览器打开 `http://127.0.0.1:8000`，会跳转到同源 `/workbench/`。页面、JavaScript 和样式随 Python 安装包提供，用户运行时不需要 Node。资源缺失时工作台明确返回503，API和健康检查仍可用。

核心依赖要求MCP SDK≥2.1.1（<3）、httpx2≥2.12（<3）及httpx≥0.27（<1），与实际使用的Client/HTTP接口一致；不再声明支持旧mcp1.x。[官方SDK 2.1.1接口](https://github.com/modelcontextprotocol/python-sdk/blob/v2.1.1/src/mcp/__init__.py)提供当前采用的Client API。

不用 uv 时可创建虚拟环境并执行 `python -m pip install -e ".[dev]"`，然后运行 `eventide-server`。

## 页面操作

| 页面 | 操作 |
|---|---|
| 项目 | 注册本机已有目录、选择项目、确认移除空注册；不删除源码文件 |
| 对话 | 新建会话、运行模式/位置、附件、模型参数、临时流式预览、审批、停止、Continue、放弃恢复 |
| 会话管理 | 重命名、归档/恢复、删除没有运行历史的空会话 |
| Skills | 目录/ZIP/内置包预检导入、启用、固定版本、差异、回退、资源及实际加载记录 |
| 知识索引 | 增量/强制/结构构建、排队、停止、历史、索引代次与调用用量 |
| 工程评测 | 离线自检、真实模型费用规划与确认、历史进度、停止、逐题报告 |
| 修复交付 | 独立副本、有界修复、固定验收、轮次/失败原因、补丁核对和人工采纳 |
| 工作副本 | 创建、标记保留、确认清理、持久操作历史 |
| 历史运行 | 身份游标分页、输出/工具证据、JSONL和脱敏Trace下载 |
| 历史导入 | 旧库只读预检、固定脱敏档案、明确确认、归档消息和导入历史 |
| 用量与成本 | Run/会话/项目/交付/UTC日汇总、每次调用、预算、价格版本及未知消费核对 |
| 任务队列/设置 | 全服务容量与精确任务停止；模型连接、参数与设置 |

请求接收、执行终态和验收结果分别展示：202只表示任务已经持久接收，管理操作完成不等于补丁通过验收。所有写入要求本机来源、浏览器同源及持久幂等身份；明确拒绝才能另发新意图，未知结果保留原键并显式重试。配置和元数据更新带版本检查，旧标签页不能覆盖新设置。

运行在持久接收后的附件缓存或后台派发失败时，仍返回原Run身份并供查询真实失败状态；同键回放不追加用户消息、不重新执行。Skill发布与成功回执发生故障时保留未知，原请求不重新发布。附件发布也遵守相同边界，已删除附件不会因旧上传回放而复活。

## 模型与状态目录

在设置页填写 Provider、模型、Base URL 和 API Key，可先显式检查连接再保存。连接测试使用真实控制Run和独立调用账本，不覆盖候选以外的已保存连接；只有点击后才发请求。模型列表也只在显式联网刷新时获取。

支持 Anthropic Messages、OpenAI-compatible Chat Completions 和 OpenAI Responses，使用官方SDK。具体模型的工具、采样及思考参数支持情况由能力描述校验；未知能力不静默丢参数。优先级为本次覆盖、会话、模型默认、服务端默认；省略继承、null恢复默认、0保留。接收Run时冻结，Continue继承并重验，Skill不能修改用户模型设置或预算。

也可参考 [.env.example](.env.example) 配置 `EVENTIDE_PROVIDER`、`EVENTIDE_MODEL`、`EVENTIDE_BASE_URL`、`EVENTIDE_API_KEY`、超时和可选检索参数。页面保存的连接优先于环境配置；密钥以Fernet加密写入状态库，不进入代码、响应和报告。不要把密钥放进命令行参数或Git。

默认状态根：macOS 为 `~/Library/Application Support/Eventide`，Windows 为 `%LOCALAPPDATA%\Eventide`，Linux 为 `$XDG_STATE_HOME/eventide` 或 `~/.local/state/eventide`。`EVENTIDE_STATE_DIR` 可覆盖，相对路径相对于服务初始项目解析。状态根保存 `runtime.sqlite`、`secret.key`、租约和受管产物，需要一并备份；`EVENTIDE_SECRET_KEY` 可提供固定Fernet主密钥。同状态根只允许一个Host，开发脚本不能在服务运行时另开第二个Owner。

流式预览是临时内容，不逐Token落库、不推进SSE持久游标。只有完整模型响应才形成可执行工具调用；断流或半成品不会自动重发。刷新后从持久事件恢复，页面切换和观察连接断开不停止后台任务。

文件读取显示一基源码行号，分页后仍保持原文件坐标，空行也计数；`offset` 继续从0开始，行号前缀只用于显示，不属于待编辑的原文。默认页为10,000显示字符，减少中等源码文件的分页往返，超长结果仍受请求预览和上下文上限约束。`glob` 的 `**/` 匹配根目录及多层路径，隐藏路径需要显式点号。只读模式遵循用户要求的回答格式，不强制将事实问答改成计划。

## 并发、执行位置与恢复

同一Session始终串行。项目聚合其源目录与Git worktree执行副本；独立clone即使远程地址相同也不混为一个项目。

- `source` 直接操作源目录，独占该源码Workspace。
- `snapshot` 配合Plan模式，接收时固定Git可见文本、参数和Skills，可与同项目其他只读快照并发。快照最多2,000文件/10MiB，单文件最多1MiB，排除链接、敏感或非文本内容；不连接MCP/源索引/嵌套worker，不开放写工具或Shell。
- `worktree` 为写入对话建立独立Git副本。首次要求源目录干净、有可信HEAD，后续消息和Continue复用该会话的脏副本；不同会话可并发修改。准备与采纳短暂协调源项目，模型执行不整轮占住源锁。

服务默认4个活跃根Run、2个并发模型请求、20个等待任务。队列持久可查、可取消；等待时不消耗模型预留。派发前复核源码和配置，漂移就拒绝。父任务等待worker不占模型槽，摘要、检索改写和embedding也使用同一模型额度与预算。

停止等待真实工具、子进程或线程收尾再释放所有权。服务重启将未结束任务停驻，不重放副作用；普通对话由用户检查后Continue。恢复检查Git checkpoint与未知工具结果，源码变化或结果未知时拒绝，用户可显式放弃旧恢复但不能消除未知副作用。非Git、缺可信提交或含submodule的源码不支持安全Continue。

Worktree不提供操作系统沙箱。本地Shell以当前用户权限运行；PolicyEngine是执行策略，所有生产工具仍经过ToolExecutor、参数契约和路径校验。ASK没有审批处理器、超时或被拒绝时默认拒绝。

## Skills、MCP 与上下文

Skill包采用 `SKILL.md`、`references/`、`scripts/` 和 `assets/`。支持本地主机目录、ZIP和三份内置包：代码审查、故障定位、测试修复。安装、项目启用、主动选择、实际加载分别记录；用户包不可变版本支持差异与回退，项目包由Git维护。

导入先检查格式、依赖、链接、路径穿越、数量和大小，不执行脚本、不自动装依赖。Run固定正文和资源清单，在途任务不受更新影响。正文与资源按需加载、资源有界分页；脚本必须经过原Executor、Policy和执行后端，Plan不能获得脚本能力。

项目的 `mcp.json` 可参考 [mcp.example.json](mcp.example.json)，支持官方MCP SDK的stdio与Streamable HTTP；工具名固定为 `mcp__server__tool`，stdio仅传配置中列出的环境变量。动态schema校验禁用网络引用解析，通过参数校验后仍检查权限。

任务计划由模型通过 `todo_write` 更新，页面只读展示服务端计划、summary和工具证据。计划标记完成只说明模型提交了该状态，不代替独立验收。`EVENTIDE_WORKERS=1` 可启用有界Researcher/Reviewer只读子Run；它们使用父任务固定快照、自己的消息与工具/步数额度，不能写入源码。

同一Run及其显式Continue链中，相同角色、精确目标和可信源码快照的重复委派会在整批派发前被拒绝，避免重复调查。返回原父工具的Run/调用身份，可用`read_tool_result`读取原结果；不缓存或伪造子任务回答。重启不清除已接收证据，源码进展、角色或目标变化和普通新Run仍可委派；每Run最多四个子任务的上限不变。不同Session不共享这些记录。

## 知识检索

知识索引由页面显式构建，保存于项目 `.eventide/index.sqlite`，是可重建派生数据。内容哈希增删改、FTS5、embedding与RRF共用源码/模型/切块指纹；计算在写事务外，发布前重验源码和索引代次，查询只见旧或新完整版本。

默认Hash embedding用于离线机制验证；远程embedding可用兼容接口并通过Host唯一预算/用量主链派发。每批最多64片段，未变化不发请求，失败保留既有消费并维持旧索引。Agent的 `search_knowledge` 返回带文件/行号的有界片段，遵循权限及实际执行副本。

可选 `uv sync --extra retrieval` 安装固定版本Sentence Transformers依赖。模型需预先在本机准备，使用 `eventide.rag.local_models.prepare_local_model` 固定revision及内容清单；配置 `EVENTIDE_EMBEDDING_PROVIDER=local_semantic`、`EVENTIDE_EMBEDDING_MODEL_PATH` 和可选 `EVENTIDE_RERANKER_MODEL_PATH`。运行时不下载，禁用远程代码、链接和pickle权重，CPU惰性加载。CrossEncoder最多重排20候选、返回5条；缺失或失败明确降级，不伪造分数。`uv sync --extra code` 可增加tree-sitter结构切块。

`EVENTIDE_RETRIEVAL_QUERY_REWRITE=1` 是默认关闭的实验开关：每个根任务与worker共用一次改写、最多一次补检索，不重试、不清零，原候选保留。改写和额外embedding均归原预算；无效输出或索引漂移保留原结果，预算拒绝或取消正常停驻。

冻结 `retrieval-v1` 含16语料、40查询，开发/留出各20。本机固定MiniLM/TinyBERT、禁网、四方法各题3次共480查询完成；留出词面Recall@5/MRR为0.80/0.75，语义0.85/0.85，混合0.85/0.80，重排0.85/0.7292。p95依次15.03/108.35/121.10/145.75ms。计时含模型校验且与其他测试并行，不能外推服务器容量；新配置未满足默认晋升条件，尤其延迟超过基线两倍，重排MRR下降。真实40-query改写排名及下游引用配对已完成，见后文；套餐价格未知，不宣称费用优化。

## 修复交付

修复交付页面先创建独立Writer副本，再填写目标、1–4条固定验收命令和1–3轮上限；可选已准备的Docker镜像，留空使用本地命令。最多初次修改加两轮修正、三次候选验收；整条交付默认36次模型尝试、60,000个已报告Token、15分钟，摘要、worker、改写和embedding跨Run累计，恢复不清零。

验收条件及保护范围开始时固定，证据绑定候选patch和checkpoint。通过、取消、超限、无源码进展、重复失败、保护变化或未知工具结果都会停止；失败保留副本和证据。每轮由独立命令判断，模型最终文本不能作为验收通过证据。

Writer开轮收到固定验收命令、轮次和累计执行额度；每次模型请求还会刷新当前步、剩余交付调用/已报告Token额度、未知次数和剩余时间，帮助在限额内完成连贯修改并交还独立验收。未知消费不当作零，显示剩余额度不改变控制器的实际拦截规则；未完成或停驻的Writer仍不能获得通过证据。

通过后查看补丁并确认采纳，服务再次复核补丁摘要和双方源码；变化、并发采纳冲突或过时验收均拒绝。不会自动commit、merge或push。受管副本有历史引用、脏文件、ignored内容、新提交或不可信索引标记时禁止清理，分支保留。

## Token、费用与评测

每次真实请求尝试都有独立身份，区分主Agent、摘要、worker、改写及embedding。用量标记reported、estimated或unknown；未知不当零，缓存/推理细项不重复加入总量，父子和终态汇总不双计。流式生成中显示待确认，输入组成仅是字符统计或估算，不冒充Provider实测Token。

预算可按Run、交付、项目、UTC日配置。请求前原子预留、返回后结算；80%提示，达到控制阈值拒绝新请求，已派发的请求按真实结果收尾。未知消费保留未结算预留，人工核对须记录原因。Continue不会绕过项目/交付/日预算；预算控制不保证供应商账单硬上限。

价格按模型、计费类别、币种和生效时间版本化，历史费用固定原价格。没价格就显示未知，各币种单列，不自动换汇。费用优化需要冻结集成功数不降，并同时报告全部失败消费、未知、重试和延迟；目前不预先声称节省比例。

工程评测冻结20题：理解定位4、故障定位4、单文件修复6、跨文件修复4、恢复交付2；12开发、8留出。Web默认离线自检，也可选择真实模型、先读取价格/配置规划再明确确认。规划按每题20k Token和90/10输入输出估算，不是账单上限；未知价格不视作免费。

评测页可显式选择“不启用评测 Skills”（默认）或“三项内置工程 Skills”。工程预设固定代码审查、故障定位、测试修复三个用户包的正文、资源清单与版本；规划摘要绑定这些内容，接收后更新内置包不改变排队批次。每题在独立状态中通过原导入/启用接口安装，恢复交付的 Writer 也固定相同版本，不向题目源码添加 Skill 文件。选择仅提供目录，正文和资源仍由模型按需加载；逐题报告分别保存选中快照和真实加载记录，未加载不会补造。该入口供同任务、同模型的 Skills 对照，不表示预设已经提高成功率或降低 Token。

用例在各自临时Git项目和状态库中执行，外部MCP关闭，判分看冻结测试、标注事实和引用。受控live调用由主Host统一模型槽与唯一账本派发，报告保留代码、任务、模型/Skill/工具指纹和价格版本；失败、超时、预算停止与中断都保留，重启不自动重发。原始产物位于忽略目录，文档只放摘要。

已有DeepSeek普通/只读工具及3个工程fixture各两次共10次基线：7次完整结束，3次Token软限停驻；6次修复独立验收通过，累计输入131,359/输出7,630 Token，48次尝试、未知0。它们是少量基线，不能外推完整冻结集成功率。后续完整冻结集、Skills及检索对照、两个实际项目任务已完成验证，结果和局限见最终汇总。

2026-10-16 模型接入补验：隔离状态中使用 `glm-5.3-flash`，普通 BigModel API 返回余额/资源包不足（1113），该次用量未知；切换 `https://open.bigmodel.cn/api/coding/paas/v4/` 后一次最小请求成功，供应商报告输入897、输出44 Token（其中推理41属于输出子集）。这仅证明单次连接成功，不是工具/修复效果验收。官方 Coding Plan 文档限定支持的工具范围，Eventide 不在当前名单；不能由连接成功推断订阅授权或长期可用性，也不把普通API人民币单价用于套餐费用。凭据仅在隔离状态加密保存，原始产物保留忽略目录，均未提交Git。

BigModel普通Chat接口为 `https://open.bigmodel.cn/api/paas/v4/`，Coding Plan专用接口为 `https://open.bigmodel.cn/api/coding/paas/v4/`；按实际账户资源和官方支持范围选择，两个端点的计费口径分开。

GLM-5.3-Flash 生成设置：`openai_compatible` 在官方普通与 Coding Plan Chat 端点下识别精确模型 `glm-5.3-flash`。按[官方模型说明](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)和[Chat 参数契约](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)，温度0–1、top_p 0.01–1且最多两位小数，推理强度low/high/max，思考仅开启，最大输出131,072。页面按能力限制选项；超范围配置在发请求前拒绝，不自动取整。Chat 使用顶层 `reasoning_effort`，Responses仍使用 `reasoning.effort`，共用原有请求和用量链路。

GLM补验使用low推理、温度1、输出上限8,000：普通响应及只读文件工具均完成，共3次请求、输入2,746/输出49 Token、缓存读取832（输入子集），未知用量0。保留思考上下文供工具后续响应使用；未修改用户项目。以上为连通和协议验证，冻结开发集效果另行报告。

2026-10-16完整GLM开发集单次为7/12，72次真实请求，reported输入115,783/输出3,943 Token，费用未知。先前3批中断实验另保留16次请求、已报告输入15,709/输出516及3次unknown；这些失败和未知消费不从总成本中抹去。开发集结果不等于候选每题三次或留出集验收，B3/U5继续执行。

## 历史与开发接口

历史页覆盖当前项目全部会话，按Run身份分页，每页100条；可查看旧Run，不切换当前执行会话。JSONL导出来自canonical事件，Trace是最多64个相关Run的脱敏派生投影，不含提示、工具正文、路径或密钥。可选 `uv sync --extra telemetry`；OTLP只有显式Python开发接口调用时才发送，不自动连接服务。

旧版库仅通过历史导入页的有界只读预检和固定脱敏档案确认导入。源库/WAL最多64MiB、5万行/32MiB文本、单字段1MiB、JSON深度32、读取10秒。旧消息归档为只读历史，旧事件不能伪装执行授权，不导入旧Provider地址/模型/密钥，不恢复旧任务。

Python开发和CI接口保留：

| 能力 | 接口 |
|---|---|
| 任务与恢复 | `RuntimeHost.run`、`continue_session`、`abandon_interruption`、`session_status` |
| 项目与副本 | `resolve_or_register_workspace`、`manage_worktree` |
| 手动交付与独立验收 | `RuntimeHost.manage_delivery` |
| 工程评测 | `eventide.evaluation.run_evaluations`，任意明确的suite路径 |
| 检索评测 | `eventide.rag.evaluation.run_retrieval_evaluation`、`rag.benchmark.run_benchmark` |
| 历史导入 | `eventide.migration.import_v02_database` |
| 导出与观测 | `RuntimeStore.export_jsonl`、`telemetry.collect_trace_events/build_run_trace/export_run_trace` |

这些接口复用原Runtime/Store，属于维护及CI能力。生产远程请求必须使用Host预算链；离线检索评测只用本地显式模型，不通过独立同步适配器绕过费用治理。

## 容器部署

```bash
docker build -t eventide .
docker compose up --build
```

Docker多阶段构建只在构建Vue时使用Node，运行镜像为Python/uv、非root UID1000，入口为 `eventide-server --host 0.0.0.0 --port 8000`。宿主仅映射 `127.0.0.1:8000`；状态卷 `/data/eventide` 与项目卷 `/workspaces/demo` 持久化，初始工作目录可写且不注册程序 `/app`。

真实项目可把项目卷改为bind mount，确保容器用户权限。模型主密钥和状态目录需要稳定保留；丢失主密钥后需重新填写密钥。容器部署不将本地Shell变成操作系统沙箱。

端口转发可能呈现非回环来源。启动配置 `EVENTIDE_LOCAL_PROXY_PEERS` 只接受实际测得的精确转发IP，默认空、不支持网段或通配符；额外来源仍必须使用回环Host与完全匹配Origin，不信任转发头。可先访问 `/healthz` 并看 `docker compose logs eventide` 的实际来源，再设置环境变量重建；不同网络不能照抄网关地址，也不用于公网认证。

Apple Silicon Docker Desktop已验证既有镜像/Compose、资源hash、非root/无需Node和持久恢复；每次入口或资源变化须重新构建验收。分发验证脚本只清理自己的临时容器/卷：

```bash
uv run python tests/container_smoke.py --image eventide:validation
```

## 开发与验证

帮助、导入模块和默认离线门禁不需要API Key或模型网络：

```bash
uv run python -m eventide --help
uv run python tests/check_service.py
uv run pytest -q
uv run pytest --cov=eventide --cov-fail-under=85
uv run ruff check eventide tests examples
uv run mypy eventide
uv run python -m tests.check_offline_evaluations evals/smoke.yaml
uv run python -m tests.check_offline_evaluations evals/tasks.yaml evals/engineering.yaml evals/workers.yaml
uv run python -m examples.rag_repair
```

`tests/check_service.py` 启动真实临时服务，检查健康、Vue跳转、独立状态与零模型调用；`tests.check_offline_evaluations` 仅是CI对Python接口的固定离线检查，不提供付费模式或终端Agent产品。

前端开发需要Node.js22.12以上：

```bash
cd frontend
npm ci --ignore-scripts
npm run dev
npm run typecheck
npm test
npm run format:check
uv run --project .. npm run api:generate
npm run build
npx playwright install chromium --only-shell
uv run --project .. npm run test:e2e
```

Vite默认同源代理到 `http://127.0.0.1:8000`，`EVENTIDE_API_URL` 可覆盖。浏览器测试使用临时真实API/Git项目与假Provider，缺浏览器明确失败，不自动下载。`EVENTIDE_CHROMIUM` 可指定已有浏览器，`EVENTIDE_E2E_BUILT=1` 直接验证FastAPI托管产物。

发布构建使用干净构建目录；setuptools可能保留旧`build/`缓存，确认其中只有可丢弃构建输出后再清理。wheel门禁拒绝已退役入口及清单以外的页面资源，不能只凭构建命令成功判断可分发。

构建会更新 `eventide/workbench/` 的源码指纹和资源SHA-256；修改前端后必须一起提交新产物。`uv build --wheel --out-dir .task_outputs/dist` 后，用 `uv run python frontend/scripts/check_wheel.py .task_outputs/dist/eventide-0.4.0-py3-none-any.whl` 隔离检查资源、服务入口和旧入口退役情况。

更多实现说明见 [architecture.md](docs/architecture.md)、[api-contract.md](docs/api-contract.md)、[decisions.md](docs/decisions.md) 和 [AGENTS.md](AGENTS.md)。项目采用 [MIT License](LICENSE)。

### 实验统计与优化对照（Python）

`eventide.evaluation_metrics.summarize_experiment([report, ...])` 只读汇总 schema v2
评测报告中的逐尝试证据：全部成功、失败、重试、截断与未知调用，输入/输出和
摘要、worker、查询改写、embedding 分类开销，任务和请求的平均、p50/p95 耗时，
缓存读取命中率及其覆盖范围，以及按币种固定价格快照的费用。父子汇总不再累计，
缓存与推理细项不再加入 Token 总量；费用或用量不完整时保留已知部分，完整总量及
“全部尝试费用÷成功任务数”返回 `null`。没有成功任务也返回 `null`，没有价格不表示免费。

`compare_experiments(baseline_reports, candidate_reports)` 要求相同冻结任务、重复次数、
模型、根Run及逐用途请求参数、工具/Skill 身份和实验输入。运行代码或上下文等优化差异须提前保存
`ExperimentVariant(name, recorded_at, differences)`；`differences` 使用精确 JSON pointer
和前后值，不接受通配豁免，声明时间不得晚于任一候选模型请求。已有开发基线可先
采集，再据开发结果预声明优化；留出结果不能用于选择优化或调参。任务和模型生成
参数不允许以 variant 名义更换。比较会公开平均/p50/p95 延迟变化，逐题通过次数下降
不能被其他题增益掩盖。只有完整真实模型批次的通过数不降、没有逐题退化，且完整
Token 或同币种、同价格版本及费率依据的完整费用下降，才给出 `improvement_demonstrated=true`。
离线脚本始终只证明机制，不能据此宣称成本优化有效。该接口不发起模型调用，不写
数据库，也不增加另一份账本；真实消融实验仍按 AGENTS.md 的 B3/U5 验收。

GLM完整开发首轮随后完成：冻结12题7通过，72次请求reported输入115,783/输出3,943，总119,726 Token，unknown0；缓存读取99,136为输入子集，覆盖72/72、命中率85.62%。任务p50/p95为30.58/52.95秒，请求p50/p95为4.56/6.98秒。5个失败分别为严格格式3、引用1和Token上限停驻1，全部保留。72次均缺套餐价格依据，费用未知，不能计算费用÷成功数。该结果仅是单次开发集实验；该首轮不外推留出表现，最终重复结果见后文。


摘要采用持久前缀认领；相同已完成内容不重复摘要，后续需新增4,000个序列化字符并间隔60秒，节流不会等待或绕过硬上下文上限。检索最多5片段、每片800及合计4,000源字符，明确提示截断并保留回读引用；格式与引用开销另计，不冒充Token。交付页链接使用delivery范围查看所有轮次、摘要和worker的用量/费用/预算，刷新不累加。

有实际异常、超时或非零退出码依据的工具连续三次以相同参数和错误失败，且可验证的源码 checkpoint 没有变化时，Run 会以 `repeated_tool_failure` 停驻，供用户检查后明确 Continue。实际调用结果和模型用量全部保留，不缓存或重放副作用；同一模型响应剩余工具标为未派发。成功调用、不同参数／错误及源码进展都会打断失败窗口。Continue（包括重启后）沿用原失败链证据，不清空历史；普通新 Run 独立计数。只读工具仅在即将触发阈值时额外复核源码，成功读取不增加扫描。旧字符串 `Error:` 前缀判错缺乏结构化依据时不纳入窗口，避免把日志正文当成失败循环；其原有展示保留。非 Git 目录或无法取得有效 checkpoint 时继续使用原步数上限，不宣称已经证明源码无进展；预算拒绝、未知结果、参数纠错和用户取消仍使用原控制规则。

两项Eventide真实历史问题的修复重放均保留为失败：HTTP任务2次请求后超时（1次用量未知），GLM参数任务10次请求后达到交付Token软限，均未形成待验收候选。已报告输入65,317/输出1,718；费用未知，不称为模型修复成功率。源码和固定验收未改，补齐了SDK传输超时配置与Host截止的一致性；冻结集效果继续独立报告。

查询改写的结构化 `query_rewrite_outcome` 与生产工具共用一次认领、原候选、补检索和RRF规则；评测直接使用排名对象，不解析展示文本。检索 benchmark 可用 `stop_requested` 保留部分结果，未尝试数独立于失败数，空样本的质量/延迟为未知且不得晋升默认。


受控改写对照使用 Python 接口 `rag.benchmark.run_query_rewrite_benchmark(host, project_id=..., embedder=..., profile=..., confirmed=True)`，默认只跑开发查询；`split="all"` 才包含全部40条。接口要求已有主Host和固定本地索引模型，不新建Provider或独立模型账本。每条查询和每次重复获得自己的归档控制Run，复用4根/20等待队列、2模型槽、项目/当日预算及一次改写认领。辅助请求继续采用生产的256输出上限和参数规则，不为评测偷偷放宽。

报告保留原排名、实际融合排名、返回字符、纯检索/模型/总耗时及唯一尝试用量；排名的 `passed` 包含生产回退结果，`successful_rewrites` 才表示实际完成有效改写。空值、相同查询、截断或无效格式记为 `degraded`/`invalid_outputs` 并继续对照；Provider故障、索引漂移、预算拒绝及取消停止扩批，分别保留已尝试和未尝试数量。停止等待真实请求清理，重启不重放，普通对话不能Continue这些控制Run。JSON只是主事件账本的可重建导出；没有下游质量和完整费用证据，不晋升默认配置。离线机制和40查询真实排名对照已完成，排名结果不代替下游质量或费用收益。

真实查询改写对照已完成固定40查询各1次：22次有效，18次无效/不变/截断后沿用原排名，reported输入2,853/输出7,877，其中推理7,589为输出子集，未知0、费用未知。原/融合Recall@5为0.925/0.950、MRR为0.900/0.9083（含降级排名）；纯检索p95为139.90/260.80ms，含模型总p95为9,155.67ms。留出20题Recall为0.85/0.90、MRR为0.800/0.8417。小型冻结语料的排名改善不能外推工程修复收益，默认仍关闭，下游引用回答见后文独立配对。


可选 `answer_comparison=True` 在同一受控实验中增加两臂下游检查，默认关闭。它只要求模型选出相关文件、逐字摘录已提供的源行并返回固定JSON引用，使用现有40条查询及expect_files/expect_contains标签；标签不发给模型，独立代码验证真实文件、行号、原文、相关文件集合及既有关键词；JSON拒重复键与非有限常量，判分后整行脱敏再保存。它不验证开放式回答的语义正确性，也不代表工程修复成功率。模板、schema、标签数据指纹和顺序规则在模型调用前固定进接收事实；两臂同模型、空采样覆盖、512输出上限，各只请求一次。原结果/融合结果的先后顺序由查询ID和重复编号的固定hash平衡。

两臂展示回答通过数、无效结构/引用数、实际attempt_id、Provider用量与固定价格成本。候选管线累计查询改写及候选回答，原管线只累计原片段回答；全部调用的总账只计一次，不能再次把两臂明细与总账相加。费用不完整或没有成功时，每成功任务费用为未知/不可计算。片段字符与请求字符单列，不冒充精确输入Token；纯检索、改写和两臂回答延迟分别展示。格式或引用失败保留并继续，网络故障、用量缺失、预算或取消停止扩批；每臂派发前也检查该根任务已观测Token软限，摘要/改写等已发生消费不能绕过。结果事实写入故障不隐藏已发生用量，已开始但缺结果单列result_missing；权威读取失败显式不完整，不能显示完整的零次调用或把未知查询当未尝试。离线机制和40条真实配对效果已完成，结果见后文；默认配置保持关闭。

2026-10-16集成复验：后端全量1,742项通过、10项可选路径跳过；Ruff、Mypy（117模块）、无密钥服务启动及smoke 12/12通过。前端121项测试、24条浏览器检查（23条真实API、1条布局）及类型/构建/格式检查通过。独立覆盖率门禁通过，语句覆盖率93.63%；真实模型重复已完成，结果独立报告，不将机制通过当作模型效果。

当前wheel隔离检查及21个冻结任务资源通过。Apple Silicon真实Docker镜像通过3个静态资源校验、非root/无需Node、同源管理、索引与交付准备幂等重启、队列容量及12题离线报告恢复；另54项真实容器执行测试通过。镜像分发自检外部模型调用为0，临时容器/卷已清理。

当前源码的直连开发对照完整完成12题、7题通过，71请求输入118,006/输出4,436 Token、unknown 0，任务p50/p95为33.25/56.33秒。旧基线同为7/12，但逐题有进有退且总Token由119,726增至122,442，不能宣称质量保持或Token优化。套餐价格未知，失败和此前中断批次分别保留；Skills、生成参数及最终重复效果分别报告，见最终汇总。

同源码、同low/温度1/输出8000的Skills单项开发对照完成：none为7/12，engineering为8/12，逐题无退化；8个修复任务实际按需加载固定test-repair正文。68请求输入147,106/输出3,969、unknown 0，总Token151,075，较none的122,442增加，不能宣称降本；单轮样本也不能外推普遍效果。另已预声明仅low→high的开发参数实验，冻结原预算，随后固定配置完成最终留出与重复。

high参数探索在第3题90秒请求超时后按规则中止，尝试3/12题、通过0题，9题未尝试；12请求中1次用量未知，已报告输入18,363/输出3,922。不能将不完整批次算作0/12或作完整参数优劣结论；原预算和超时不放宽。最终候选仅依据完整开发结果固定为engineering Skills＋low/温度1/输出8000，随后完成20题各3次的完整重复，留出题未用于选配置。

C5固定40查询的真实配对已完成，全部120请求用量已报告：原片段引用通过15/40（开发10/20、留出5/20），改写管线通过20/40（13/20、7/20）。原管线输入18,615/输出15,771，候选含改写输入21,529/输出22,387，总Token分别34,386/43,916；价格未知，每成功任务费用不可计算。管线p50/p95分别9.32/13.89秒与13.67/17.58秒，包含各自检索、改写及回答，不包含另一臂回答。

本轮Recall/MRR两路均0.925/0.900；17次有效改写、23次降级，27/40查询的有序源片段相同，回答仍有随机差异。因此15→20仅是这次窄引用样本的观测，不证明工程修复收益或因果效果。原/候选输入片段共27,689/28,038字符，字符不冒充Token；截断与引用失败全保留。默认不晋升，旧网络中断批次的8次请求另计入全尝试账本。

分发依赖补验：保留锁定SDK 2.1.1/httpx2 2.12的既有实现，全新无dev wheel安装还验证了SDK 2.3.0/httpx2 2.13.1，真实stdio/Streamable HTTP调用和服务工作台启动均通过，零模型调用。当前Python实现hash及已安装实验依赖未变；元数据只修正允许范围和生产直接依赖声明。10项MCP/分发专项与重建Docker完整分发自检通过。

两个历史问题的直连定向重试仍未成功：各10请求达到60k已观测Token软限，HTTP输入61,863/输出1,658，GLM输入62,093/输出3,165，用量未知0；保存了2,513/5,405字节的部分补丁，未进入候选验收、未采纳。事后使用原冻结测试作离线诊断：HTTP仍2失败；GLM为1失败/7通过。它们不是成功候选，不能用部分改动或局部通过冒充交付完成。

优化结论按实际暴露区分：开发none的71次调用全部为主Agent，摘要/只读worker/检索输入/结果裁剪/三次重复失败窗口均未触发；这些机制有专项离线测试，但本批次不能估计其生产收益。Skills有实际按需加载，检索有独立配对，参数有预声明且按失败规则中止的探索；尚无可宣称的Token或费用改善。缓存命中率只表示供应商报告的输入子集比例，不能说明输入Token减少。

最终重复的前两轮完整为10/20、12/20；第三轮在第6题Connection error后停止，14题未尝试，该轮26请求中1次用量未知。保持配置、预算及判分不变，已预声明补一轮完整20题，仅依据完成状态决定补轮；原中断批次保留在全尝试质量/用量中，完整轮次结果与包含中断的总尝试分开报告。

## 最终实测汇总（2026-10-16）

最终配置固定为GLM-5.3-Flash、engineering Skills、low、温度1、输出8000；每题12步、20,000已观测Token、300秒。三个完整轮次为10/20、12/20、12/20，合计**34/60**；10/20题三次均通过，12/20题至少通过一次。开发23/36，留出11/24，留出未用于调参。

| 冻结任务类型 | 三轮通过 / 尝试 |
|---|---:|
| 理解 | 0/12 |
| 故障定位 | 0/12 |
| 单文件修复 | 16/18 |
| 跨文件修复 | 12/12 |
| 恢复交付 | 6/6 |

理解/定位未达到严格结构、事实与引用要求，其中既有围栏格式错误，也有事实或行号错误；不能将这24次失败改算成功。小型修改任务34/36不代表复杂项目稳定交付：两个真实历史问题的四次任务尝试均失败，部分补丁和原验收全部保留。

353次完整轮调用reported输入708,682/输出18,909，总727,591 Token；摘要、只读worker、改写额外调用均未在这组工程任务中发生。任务均值/中位数/p95为25.09/22.56/42.49秒（nearest-rank p50为22.51秒），请求为3.84/4.12/7.80秒；缓存读取623,616是输入子集，覆盖353/353，命中比例88.00%，不能称输入减少。

原第三轮在第6题连接错误中断后停止，14题未尝试。补充轮在调用前声明，仅按完成状态补足完整重复，配置及判分不变。**包含中断，实际任务尝试34/66（51.52%），调用379次，1次用量未知**；已报告输入748,507/输出19,732，不显示完整总Token。80个计划槽中14个未尝试，34/80=42.5%是计划槽通过比例，不能冒充实际尝试成功率。含中断任务均值/中位数/p95为25.35/22.18/45.34秒（nearest-rank p50为22.08秒）。

本轮保留的全部GLM验证、诊断、中断及历史项目调用共855次，reported输入1,445,039/输出93,365，9次用量未知。这是运行总账，不能将不同任务和探测合成一个质量成功率。套餐价格均未知，每成功任务费用不可计算；本轮没有证实Token或费用改善。真实负载中未触发的优化也不宣称已有收益。实现、工程门禁及实验工作完成，不意味着当前模型已经稳定完成复杂项目任务。
