# Eventide 用户指南

本指南承接工作台的详细操作、配置、部署和开发说明。首次安装见 [README](../README.md#快速开始)；运行结构见 [架构](architecture.md)，HTTP/SSE 字段见 [API 契约](api-contract.md)。

- [安装与服务启动](#安装与服务启动)
- [页面与常用操作](#页面与常用操作)
- [模型与状态目录](#模型与状态目录)
- [并发、执行位置与恢复](#并发执行位置与恢复)
- [Skills、MCP 与上下文](#skillsmcp-与上下文)
- [长期偏好记忆](#长期偏好记忆)
- [代码调查与知识检索](#代码调查与知识检索)
- [修复交付](#修复交付)
- [用量、预算与评测](#用量预算与评测)
- [历史与开发接口](#历史与开发接口)
- [Docker 部署与执行沙箱](#docker-部署与执行沙箱)
- [开发与验证](#开发与验证)

## 安装与服务启动

本机推荐使用 README 的 uv 启动方式。也可在虚拟环境中执行 `python -m pip install -e .`，然后运行 `eventide-server --host 127.0.0.1 --port 8000`；开发者额外安装 `.[dev]`。

`uv run python -m eventide --port 8000` 启动同一服务。两个部署入口接受 host、port 和可选初始项目 `--workspace /absolute/project`，没有终端聊天、任务、检索或评测子命令。

前端资源随 Python 包提供，最终用户不需要 Node；资源缺失时工作台返回 503，API 与健康检查仍可用。核心依赖范围由 [pyproject.toml](../pyproject.toml) 和锁文件管理，MCP 使用官方 SDK 2，不支持旧 SDK 1。

## 页面与常用操作

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

对话中的运行模式与执行位置分开选择：Auto 对风险操作询问，Plan 用于只读分析、不开放内置 Shell 和文件写入，Agent 自动批准策略中的风险操作；工具契约、路径和执行边界仍由 Runtime 检查。需要并发写入时选择独立 worktree，需要固定只读源码时选择 Plan 快照；详见[并发与恢复](#并发执行位置与恢复)。

请求接收、执行终态和验收结果分别展示：202只表示任务已经持久接收，管理操作完成不等于补丁通过验收。所有写入要求本机来源、浏览器同源及持久幂等身份；明确拒绝才能另发新意图，未知结果保留原键并显式重试。配置和元数据更新带版本检查，旧标签页不能覆盖新设置。

运行在持久接收后的附件缓存或后台派发失败时，仍返回原Run身份并供查询真实失败状态；同键回放不追加用户消息、不重新执行。Skill发布与成功回执发生故障时保留未知，原请求不重新发布。附件发布也遵守相同边界，已删除附件不会因旧上传回放而复活。

项目目录选择会打开**运行 Eventide 服务的电脑**上的系统窗口：macOS 使用系统 AppleScript，Linux 使用系统 Python 3 与已有 GTK 3。无桌面会话、缺少 GTK、容器或不支持的平台会明确提示，可显式选择手动输入服务电脑上的绝对路径；不会安装依赖或自动浏览目录。选择与取消都不会注册项目或启动任务，选中后仍需确认添加。

### 导航、关闭与输入

非对话页面提供“‹ 返回”，仅使用已成功访问的工作台历史；直接进入页面时返回对话，不跳到外站。详情和预览右上角“×”只关闭展示，轮询及迟到响应不会重新打开，也不会停止后台任务或清除结果未知请求的重试身份。需要停止时使用明确的停止按钮。

模型连接、生成默认值、预算、价格和编辑表单离开页面或关闭标签时提示未保存修改；候选 API Key 仅留在内存，不写入浏览器存储。预算范围和生成设置范围切换也先确认放弃草稿。

工作台支持可折叠侧栏与浅色/深色/跟随系统外观。Enter 换行，Cmd/Ctrl + Enter 发送；输入法组合阶段不发送。运行中草稿不会自动排队。向上阅读暂停自动跟随，可用“回到最新”恢复。

## 模型与状态目录

在设置页填写 Provider、模型、Base URL 和 API Key，可先显式检查连接再保存。连接测试使用真实控制Run和独立调用账本，不覆盖候选以外的已保存连接；只有点击后才发请求。模型列表也只在显式联网刷新时获取。

支持 Anthropic Messages、OpenAI-compatible Chat Completions 和 OpenAI Responses，使用官方SDK。具体模型的工具、采样及思考参数支持情况由能力描述校验；未知能力不静默丢参数。优先级为本次覆盖、会话、模型默认、服务端默认；省略继承、null恢复默认、0保留。接收Run时冻结，Continue继承并重验，Skill不能修改用户模型设置或预算。

也可参考 [.env.example](../.env.example) 配置 `EVENTIDE_PROVIDER`、`EVENTIDE_MODEL`、`EVENTIDE_BASE_URL`、`EVENTIDE_API_KEY`、超时和可选检索参数。页面保存的连接优先于环境配置；密钥以Fernet加密写入状态库，不进入代码、响应和报告。不要把密钥放进命令行参数或Git。

默认状态根：macOS 为 `~/Library/Application Support/Eventide`，Windows 为 `%LOCALAPPDATA%\Eventide`，Linux 为 `$XDG_STATE_HOME/eventide` 或 `~/.local/state/eventide`。`EVENTIDE_STATE_DIR` 可覆盖，相对路径相对于服务初始项目解析。状态根保存 `runtime.sqlite`、`secret.key`、租约和受管产物，需要一并备份；`EVENTIDE_SECRET_KEY` 可提供固定Fernet主密钥。同状态根只允许一个Host，开发脚本不能在服务运行时另开第二个Owner。

0.5.0 使用 schema v5，为偏好事件新增索引并自动迁移旧版状态。升级前停止服务并备份完整状态根；旧版程序不能直接打开升级后的 v5 库，回退需使用升级前备份。本次发布验收仅使用隔离临时状态，没有迁移用户的实际状态目录。

流式预览是临时内容，不逐Token落库、不推进SSE持久游标。只有完整模型响应才形成可执行工具调用；断流或半成品不会自动重发。刷新后从持久事件恢复，页面切换和观察连接断开不停止后台任务。

生成设置按模型能力提供 text／json_object 等最终输出格式。JSON 模式先使用工具调查，再以一次无工具请求整理对象；两阶段共用原步数和预算。非法最终响应保留并明确失败，格式合法仍需核对事实和引用。具体供应商端点与参数范围请按账户实际资源及模型能力配置；连接成功不等于工具协议或修复效果验证通过。

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

项目的 `mcp.json` 可参考 [mcp.example.json](../mcp.example.json)，支持官方MCP SDK的stdio与Streamable HTTP；工具名固定为 `mcp__server__tool`，stdio仅传配置中列出的环境变量。动态schema校验禁用网络引用解析，通过参数校验后仍检查权限。

任务计划由模型通过 `todo_write` 更新，页面只读展示服务端计划、summary和工具证据。计划标记完成只说明模型提交了该状态，不代替独立验收。`EVENTIDE_WORKERS=1` 可启用有界Researcher/Reviewer只读子Run；它们使用父任务固定快照、自己的消息与工具/步数额度，不能写入源码。

同一Run及其显式Continue链中，相同角色、精确目标和可信源码快照的重复委派会在整批派发前被拒绝，避免重复调查。返回原父工具的Run/调用身份，可用`read_tool_result`读取原结果；不缓存或伪造子任务回答。重启不清除已接收证据，源码进展、角色或目标变化和普通新Run仍可委派；每Run最多四个子任务的上限不变。不同Session不共享这些记录。

## 长期偏好记忆

记忆属于当前 Eventide 状态目录内的单用户安装；项目记忆使用稳定 `project_id`，源 checkout 与同仓 worktree 共享，独立 clone 不混用。它是偏好数据，不能改变 Auto/Agent/Plan、审批、Continue 条件或补丁采纳权限。

- 在记忆设置中创建、编辑、暂停或撤销；主题键例如 `response_language`、`answer_style`、`code_style`，同作用域同主题已存在时要求人工处理冲突，不静默覆盖。
- 对话内支持明确的单行格式：`记住：项目：response_language=用中文回答`，或 `remember: global: answer_style=Start with the conclusion`。必须明确项目/全局；不明确、引述、多行、附件及工具文本不会直接保存。格式外请使用设置页确认，不宣称任意自然语言自动理解。
- 同一会话两次独立用户消息 `偏好：answer_style=先结论后解释` 会产生项目级待确认建议，确认后才加入记忆。建议绑定两条真实来源，模型、工具、委派任务、旧导入及摘要不能生成保存授权。重复来源在拒绝/撤销后不被重新利用；新的独立用户陈述仍可重新提出建议。
- 当前明确请求优先，其次项目、全局同主题偏好。每个 Run 开始执行时确定最多 12 条偏好，内容最多每条 500 字符，原文中的否定与条件不做摘要改写。普通编辑下个 Run 生效；暂停、撤销和总开关关闭在下次实际模型请求（含重试）前复核，已发送请求无法撤回。暂停后重新启用的条目下个 Run 才恢复，Continue 新 Run 读取当前有效版本。
- 总开关关闭停止注入和新建议；此时仍可主动保存，但不会悄悄重新开启。子 Worker、交付 Writer、控制会话及冻结离线/真实评测不自动读写偏好，避免提示词被后台任务改写或评测被个性化。
- 请求记录展示“已加入请求上下文”的具体 ID/版本和实际 attempt，不代表模型一定采用，也不构成答案因果证明。排队取消、预算拒绝或未派发时没有注入记录。
- **撤销不是彻底擦除**：它停止未来偏好注入，但原聊天、历史事件、既有摘要、导出、备份及已发送供应商请求可能仍包含原内容。不要保存密钥、密码或其他秘密；服务拒绝已知凭据和常见敏感模式，不将检测器当作完整 DLP。

实现无额外 LLM/embedding 调用，使用唯一 Runtime Event Log 的版本化事实；同源管理写入带幂等键和乐观版本比较。HTTP 详细格式见 [API 契约](api-contract.md#长期偏好记忆)。

## 代码调查与知识检索

文件读取显示一基源码行号，分页后仍保持原文件坐标，空行也计数；`offset` 继续从0开始，行号前缀只用于显示，不属于待编辑的原文。默认页为10,000显示字符，减少中等源码文件的分页往返，超长结果仍受请求预览和上下文上限约束。`glob` 的 `**/` 匹配根目录及多层路径，隐藏路径需要显式点号。只读模式遵循用户要求的回答格式，不强制将事实问答改成计划。

本地执行的 Plan 和只读 worker 可用 `search_text` 无需索引搜索 UTF-8 文件中的字面文本，返回路径、一基行号及有界片段。`pattern` 支持路径 glob（包括零层 `**/`），`offset` 按匹配行分页，每页最多50行／约9,000正文字符。目录枚举最多2,000项，读取合计10MiB、单文件1MiB；二进制、非UTF-8和过大文件计数显示，隐藏路径须显式点号，目录链接不递归。达到上限会明确标出未搜索范围，需缩小路径模式，不能把局部无匹配当全仓不存在。当前可选 Docker 沙箱不提供此工具，不会回退宿主搜索。

### 可选知识索引

知识索引由页面显式构建，保存于项目 `.eventide/index.sqlite`，是可重建派生数据。内容哈希增删改、FTS5、embedding与RRF共用源码/模型/切块指纹；计算在写事务外，发布前重验源码和索引代次，查询只见旧或新完整版本。

默认Hash embedding用于离线机制验证；远程embedding可用兼容接口并通过Host唯一预算/用量主链派发。每批最多64片段，未变化不发请求，失败保留既有消费并维持旧索引。Agent的 `search_knowledge` 返回带文件/行号的有界片段，遵循权限及实际执行副本。

可选 `uv sync --extra retrieval` 安装固定版本Sentence Transformers依赖。模型需预先在本机准备，使用 `eventide.rag.local_models.prepare_local_model` 固定revision及内容清单；配置 `EVENTIDE_EMBEDDING_PROVIDER=local_semantic`、`EVENTIDE_EMBEDDING_MODEL_PATH` 和可选 `EVENTIDE_RERANKER_MODEL_PATH`。运行时不下载，禁用远程代码、链接和pickle权重，CPU惰性加载。CrossEncoder最多重排20候选、返回5条；缺失或失败明确降级，不伪造分数。`uv sync --extra code` 可增加tree-sitter结构切块。

`EVENTIDE_RETRIEVAL_QUERY_REWRITE=1` 是默认关闭的实验开关：每个根任务与worker共用一次改写、最多一次补检索，不重试、不清零，原候选保留。改写和额外embedding均归原预算；无效输出或索引漂移保留原结果，预算拒绝或取消正常停驻。

检索方案的质量、延迟与默认配置取舍见 [架构中的冻结检索实验](architecture.md#冻结检索实验入口)。语义检索、重排和查询改写为显式可选能力，尚未据此晋升默认方案。

## 修复交付

修复交付页面先创建独立Writer副本，再填写目标、1–4条固定验收命令和1–3轮上限；可选已准备的Docker镜像，留空使用本地命令。最多初次修改加两轮修正、三次候选验收；整条交付固定最多36次模型尝试、15分钟，已报告Token软限默认60,000，可在开始前设置为1–1,000,000的整数，不支持禁用。摘要、worker、改写和embedding跨Run累计，恢复不清零；提高本次额度后取得的结果会按新预算条件记录，不能算作原60k条件下的改善。

开始前可点击“追加验收覆盖清单模板”，在原目标后填写预期行为、关键异常与验收命令编号（按输入顺序从1开始），没有对应证据的要求明确写“未验证”、原因及后续核查方式。模板仅为待填写提示，不表示已覆盖；也可直接在目标中自行填写。清单随现有目标一起冻结，详情显示原文与编号命令。命令全部通过仍不证明覆盖全部目标，采纳前应核对未覆盖项和补丁。

验收条件及保护范围开始时固定，证据绑定候选patch和checkpoint。通过、取消、超限、无源码进展、重复失败、保护变化或未知工具结果都会停止；失败保留副本和证据。每轮由独立命令判断，模型最终文本不能作为验收通过证据。

Writer开轮收到固定验收命令、轮次和累计执行额度；每次模型请求还会刷新当前步、剩余交付调用/已报告Token额度、未知次数和剩余时间，帮助在限额内完成连贯修改并交还独立验收。未知消费不当作零，显示剩余额度不改变控制器的实际拦截规则；未完成或停驻的Writer仍不能获得通过证据。

验收失败后，下一轮收到上一候选的轮次、补丁标识及全部检查的状态、退出码和调用来源；失败日志按项保留头尾，标明省略量，成功项省略日志。反馈 JSON 最多 12,000 字符，长输出不会挤掉后续检查。Writer 被要求先检查当前源码，简述失败依据、待验证假设和修正点，再进行必要修改；这些说明不代替独立验收，也不增加专门的反思模型调用。失败反馈机制已有离线验证；真实修复效果仍需同条件对照，不宣称成功率提高。

修正轮次可通过 `read_tool_result`，按反馈的 `run_id/call_id` 分页回查上一失败候选已保存的验收输出；页头标出原交付、轮次和补丁。权限只属于获得授权的 Writer Run，其他会话或同副本后续运行不能沿用；执行器未保留的原始输出无法补回，旧候选日志也不证明新修改已通过。

通过后查看补丁并确认采纳，服务再次复核补丁摘要和双方源码；变化、并发采纳冲突或过时验收均拒绝。不会自动commit、merge或push。受管副本有历史引用、脏文件、ignored内容、新提交或不可信索引标记时禁止清理，分支保留。

## 用量、预算与评测

用量页可按 Run、会话、项目、交付和 UTC 日查看全部历史与每次调用，区分主任务、摘要、子任务、查询改写和 embedding。实际用量采用 Provider usage；缺失用量保留 unknown，估算与字符组成另列。缓存读取和推理细项可能已包含在输入/输出内，不重复累加。流式生成时用量待确认，刷新不会重复计数。

预算支持 Run、交付、项目及当日范围。模型请求前共享原子预留，返回后结算；未知消费保留未结算，重试和 Continue 不清空项目或交付累计。达到控制阈值禁止新请求；这是本地预算控制，不能保证供应商账单硬上限。

价格按供应商、模型、计费类别、币种和生效日期版本化，历史估算固定原价格依据。无价格显示费用未知，不同币种不直接相加；套餐额度不套用普通 API 单价。用量核对与价格编辑见 [API 契约](api-contract.md#预算价格和费用控制面u2u3-后端已接入)。

评测页区分离线机制自检与真实模型任务。真实任务先规划并确认，再通过同一 Runtime 队列、模型额度和预算派发；每题使用独立临时 Git 项目及状态，关闭外部 MCP。冻结工程集有 20 题，开发 12、留出 8，修复由冻结测试独立判分，理解/定位按事实和引用判断。失败、超时、预算停止、中断和未知消费全部保留，服务重启不自动重发。

评测可明确选择 none 或 engineering Skills 预设，安装选择与实际按需加载分别记录。报告保存代码、任务、模型参数、Skill/工具指纹、唯一调用及价格版本；离线机制通过不能当作模型成功率。

Python 统计入口 `eventide.evaluation_metrics.summarize_experiment([report, ...])` 只读汇总全部尝试；`compare_experiments(baseline_reports, candidate_reports)` 核对冻结任务、参数、重复和预声明差异，不发模型请求。无完整价格或无成功任务时，每成功任务费用不可计算；结果和比较身份详见 [架构中的实验统计](architecture.md#只读实验投影与比较身份)。

历史模型结果与局限见 [架构中的真实实验记录](architecture.md#真实模型实验记录)；这些记录对应各自冻结版本，不代表当前 master 的最新模型效果。Token/费用优化目前暂缓，没有已证实的降本结论。

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

## Docker 部署与执行沙箱

容器部署服务与在对话中选择 Docker 执行沙箱是两项不同配置。

### Compose 部署

```bash
docker build -t eventide .
docker compose up --build
```

Docker多阶段构建只在构建Vue时使用Node，运行镜像为Python/uv、非root UID1000，入口为 `eventide-server --host 0.0.0.0 --port 8000`。宿主仅映射 `127.0.0.1:8000`；状态卷 `/data/eventide` 与项目卷 `/workspaces/demo` 持久化，初始工作目录可写且不注册程序 `/app`。

真实项目可把项目卷改为bind mount，确保容器用户权限。模型主密钥和状态目录需要稳定保留；丢失主密钥后需重新填写密钥。容器部署不将本地Shell变成操作系统沙箱。

端口转发可能呈现非回环来源。启动配置 `EVENTIDE_LOCAL_PROXY_PEERS` 只接受实际测得的精确转发IP，默认空、不支持网段或通配符；额外来源仍必须使用回环Host与完全匹配Origin，不信任转发头。可先访问 `/healthz` 并看 `docker compose logs eventide` 的实际来源，再设置环境变量重建；不同网络不能照抄网关地址，也不用于公网认证。

分发验证脚本使用上面构建的镜像，只清理自己的临时容器/卷：

```bash
uv run python tests/container_smoke.py --image eventide
```

### 可选离线执行沙箱

普通对话可独立选择“本地”或“Docker 沙箱”；Auto / Plan / Agent 的权限含义不变，默认仍为本地。沙箱面向安装 Docker Desktop 的 Mac 或本地 Linux Docker：先准备可信的本地 Linux 镜像，镜像必须包含 `/bin/sh`、`python3` 和项目所需依赖，再在新会话中填写镜像名。Eventide 不拉取镜像、不在线安装依赖；接收时固定本地 image ID，后续消息和 Continue 使用同一个候选目录和镜像。首次执行之后不能切换该会话的边界，切换需新建会话。

文件读写、查找及 Shell 命令均通过容器执行；MCP、Skills、只读子任务、知识索引检索和自定义工具在此模式禁用。Plan 仍不提供 Shell/写入工具。前提是已注册的本地项目及其 Git 配置可信；准备阶段会在 Host 读取项目，不支持安全导入任意恶意仓库。源项目以有界 UTF-8 文本快照进入独立任务目录，排除 `.git`、`.env` / `.env.*`、常见凭据/状态目录、链接及二进制文件，不挂载源 checkout、Git 共享元数据、HOME 或 Docker socket。文件名过滤不是完整的秘密扫描：源代码中硬编码的秘密仍可能进入快照，不应向不可信镜像提供它们。

容器工具网络固定关闭，使用只读根文件系统、非 root UID、无 capabilities/no-new-privileges、512 MiB 内存、1 CPU、128 PIDs、120 秒单命令时限和 25,000 字节输出保留上限。模型 Provider 仍在 Host 上联网，这不是整个 Agent/操作系统隔离；Docker daemon 和镜像仍需信任，任务目录的可写绑定挂载没有硬磁盘配额。源码快照最多 2,000 文件/10 MiB、单文件 1 MiB；候选检查沿用这些上限，Shell 超限后会拒绝继续/导出，但不会保证阻止磁盘耗尽。

沙箱修改仅是候选，不自动写回项目。停止执行后，可从 Run 详情导出当前会话候选的完整文本补丁（最多 2 MiB），审阅后自行应用到源项目；这不是历史 Run 快照，也不包含被过滤文件或二进制变化。暂不提供应用内采纳、二进制导出和带空白/引号文件名的补丁。源项目可在沙箱运行后改变，手动应用前需自行检查冲突。Docker/镜像不可用、候选损坏或容器清理结果未知时明确失败，不回退本地；清理未知的会话禁止继续、追加运行和导出，保留证据供人工检查 Docker 后新建会话。

## 开发与验证

帮助、导入模块和默认离线门禁不需要API Key或模型网络：

```bash
uv sync --locked --extra dev
uv run python -m eventide --help
uv run python tests/check_service.py
uv run pytest -q --cov=eventide --cov-fail-under=85
uv run ruff check eventide tests examples
uv run mypy eventide
uv run python -m tests.check_offline_evaluations evals/smoke.yaml
uv run python -m tests.check_offline_evaluations evals/tasks.yaml evals/engineering.yaml evals/workers.yaml
uv run python -m examples.rag_repair
```

`tests/check_service.py` 启动真实临时服务，检查健康、Vue跳转、独立状态与零模型调用；`tests.check_offline_evaluations` 仅是CI对Python接口的固定离线检查，不提供付费模式或终端Agent产品。

实现期间先跑受影响的定向测试，收口一次性检查全量功能与覆盖率；失败先定位并复验相关范围，没有实现变化不重复完整门禁。模型评测的固定三次重复用于观察生成波动，不代表离线测试也要重复三次。

GitHub CI 覆盖 Python 3.10 和 3.14，每个版本运行一次完整后端测试；3.14 在同次运行采集覆盖率并检查 85% 门槛，quality job 只执行 Ruff/Mypy。可选扩展、容器与前端门禁独立保留。测试代码同样遵守最低版本：UTC 时间使用 `timezone.utc`，异步轮询使用原 10 秒截止，避免在测试中依赖 Python 3.11 才增加的接口。临时 Git 仓库在自身配置中固定测试作者，后续提交不依赖开发者的全局 Git 身份。

前端开发需要 Node.js 22.12 以上。先按 README 启动后端，再开一个终端启动 Vite：

```bash
cd frontend
npm ci --ignore-scripts
npm run dev
```

检查与构建在另一个终端的 `frontend/` 目录执行：

```bash
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

构建会更新 `eventide/workbench/` 的源码指纹和资源SHA-256；修改前端后必须一起提交新产物。`uv build --wheel --out-dir .task_outputs/dist` 后，用 `uv run python frontend/scripts/check_wheel.py .task_outputs/dist/eventide-0.5.0-py3-none-any.whl` 隔离检查资源、服务入口和旧入口退役情况。

项目采用 [MIT License](../LICENSE)。维护步骤与失败记录要求见 [AGENTS.md](../AGENTS.md)。
