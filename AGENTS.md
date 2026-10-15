# Eventide 维护约定

本文件是新 Codex 对话进入仓库时的统一上下文入口。仓库文件和 Git 状态是事实来源，不依赖其他对话的历史。

## 本轮目标与完成标准

- 将 Eventide 升级为 Web 唯一用户入口的软件工程 Agent 工作台：同项目多对话、Skills 扩展、模型参数与流式、Token 成本治理、隔离修改和独立验收、人工采纳。
- 顺序：0 基线 → A 后端/模型契约 → P 并发 → S Skills → F Vue → B 评测 → C 检索 → D 交付 → X 删除旧入口 → R 收口。U 用量治理贯穿 A/P/F/B/C/D。
- 先完成全部必要验收，再整理简历。不要为了展示提前删除尚未被 Web/API 承接的能力。
- 每个编号步骤开始/完成时更新本文件状态；完成还须更新对应稳定 Markdown、附验证证据并创建小而完整的本地提交。不能把计划或跳过验证标为完成。
- 状态统一为：待开始、进行中、受阻、待外部验证、已完成。原始日志和 live 产物留在忽略目录，文档只放摘要。
- 维护代理须完整阅读本文件；Runtime 当前只加载前 4,000 字符，不能将其截取范围等同维护范围。

## 固定技术边界

- 后端 Python/asyncio/FastAPI/Pydantic 2，SQLite WAL 和唯一追加式 Event Log；ToolExecutor/PolicyEngine 仍是全部生产工具的边界。
- 前端 Vue 3/TypeScript/Vite/Vue Router/Pinia，HTTP/SSE，FastAPI 同源托管产物；Node 只用于开发构建。最终删除用户 CLI，保留部署入口和 Python 开发/CI 接口。
- MCP 官方 SDK；内置严格类型及动态 jsonschema 校验。Skills 使用 SKILL.md/references/scripts/assets，只支持本地目录和压缩包、项目/用户作用域及固定版本。
- 检索 SQLite FTS5/RRF、现有 embedding、可选 tree-sitter/本地语义 embedding/CrossEncoder；可选依赖惰性加载，不在启动/离线测试中下载模型。
- 并发先做单进程：同 Session 串行、只读快照并发、写任务独立 worktree，直接源 checkout 与补丁采纳保持独占。
- MySQL、Redis、分布式 worker、多租户、框架重写、技能市场、微调和自建推理均后置，不顺手扩张范围。
- 用量区分 reported/estimated/unknown；未知不记零、细项不重复相加、父子不双计。预算不是供应商账单硬上限。

## 开始工作

1. 阅读 `README.md`、`docs/architecture.md` 和 `docs/decisions.md`。
2. 运行 `git status --short`，确认当前工作区和用户已有修改。
3. 运行 `git log -5 --oneline`，了解最近完成的工作。
4. 按任务定位实现和测试，不从旧聊天推测当前代码状态。

## 多对话与 worktree

- 只读分析可以共享当前 checkout。
- 可能写入代码或文档的并行任务使用独立 Git worktree 和 `codex/` 前缀分支。
- 同一功能只由一个写入型对话负责；不要在两个 worktree 中同时实现同一修改。
- 不覆盖、还原或整理其他对话和用户的未提交改动。
- 提交保持小而完整；Git commit 是跨对话交接状态的主要载体。

## 生产实现边界

- 新 Runtime 功能优先进入 `host.py`、`store.py`、`providers/`、`executor.py`、`policy.py`、`worktrees.py`、`mcp/client.py`、`api.py` 或 `web/`。
- 生产入口统一为 `RuntimeHost` / `RuntimeStore`；旧教学 Agent、context/hooks、cron、协作和全局注册表已退役，不重新引入第二套执行或状态路径。
- Worktree 是用户控制面的 Git 工作副本；写入协作先隔离 Workspace，再设计调度和合并，不能把它描述为操作系统沙箱。
- import、`--help`、单元测试和离线评测不得依赖 API Key 或网络。
- 所有生产工具调用必须经过 `ToolExecutor` 和 `PolicyEngine`，文件工具继续执行内部路径校验。
- 没有审批处理器、用户拒绝或审批超时时，`ASK` 默认拒绝。
- 同一 session 必须串行，不同 session 不共享消息或审批状态。
- MCP 生产路径使用官方 SDK，工具名保持 `mcp__server__tool`。
- 本地 Shell 不是安全沙箱，不把策略校验描述为操作系统隔离。

## 文档同步规则

- 用户可见能力、配置或运行方式变化：更新 `README.md`。
- 当前结构、接口、数据模型或工程约定变化：更新 `docs/architecture.md`。
- 长期设计取舍变化：在 `docs/decisions.md` 新增 ADR，或把旧 ADR 标记为 `Superseded` 并链接替代项。
- HTTP/SSE/错误契约变化：更新 `docs/api-contract.md`。
- 不新增 progress、handoff、roadmap、简历或面试 Markdown；步骤状态以本文件为入口，Git/测试为证据，对话解释结果。

## 安全与仓库卫生

- 保留 `LICENSE`。
- 不提交 `.env`、`mcp.json`、`.eventide/`、API Key、数据库、trace 原文或 live 运行产物。
- 写文件前检查目标路径和现有改动；删除或移动前确认精确目标。
- 不创建远程、不推送、不公开发布，除非用户明确授权。

## 完成工作

根据修改范围运行相关检查；完整门禁为：

```bash
uv run python -m eventide --help
uv run pytest -q
uv run pytest --cov=eventide --cov-fail-under=85
uv run ruff check eventide tests examples
uv run mypy eventide
uv run eventide eval evals/smoke.yaml
```

完成后：

1. 更新受影响的稳定文档。
2. 运行 `git diff --check` 并复查 `git status --short`。
3. 创建一个说明清楚的本地提交；不要夹带无关修改。
4. 在最终回复中报告修改内容、验证结果、commit hash 和仍需用户处理的阻塞。

## 回复风格

- 回复稍微带点大白话：先说结论，再补细节，少堆术语。

## 升级步骤与状态

以下是待实施契约，不代表当前能力。每行证据包含实际实现、验证摘要、对应文档及遗留事项；子任务在自己的 worktree 更新，集成时核实状态。

| 编号 | 状态 | 实施与验收要求 / 证据 |
|---|---|---|
| 0.1 | 已完成 | 2026-10-15：本计划和逐步状态制度已写入本文件；README 过时 Subagent 表述已修正，架构/ADR 区分目标与现状；文档检查和 git diff --check。 |
| 0.2 | 已完成 | 2026-10-15：Host/live 共用配置优先级；live 只读解析已提交 WAL 配置、隔离前解密且不创建 Host/密钥，错误不回退。88 项定向回归、Ruff、Mypy 50 模块通过；README/architecture/ADR-051 已同步。真实调用另属 0.4。 |
| 0.3 | 已完成 | 2026-10-15：全部用例独立临时 Git 项目/数据库，live/offline 均关闭 MCP；失败产物保留，报告可逐例定位，凭据不写产物、用户源码/状态不变。88 项回归及 smoke12/tasks14/engineering3/workers1 全通过；README/architecture/ADR-051 同步。 |
| 0.4 | 已完成 | 2026-10-16：真实DeepSeek Responses非思考模式，普通/只读工具及3工程fixture各2次，共10次、7次完整完成；3次Token软限停驻且固定测试仍通过，6次修复独立验收均通过。累计131,359输入/7,630输出Token、48次请求、未知0；失败全保留，未改用户状态。README/architecture有摘要，不能外推成功率。 |
| A1 | 进行中 | 2026-10-16：api.py 收敛为 composition root，现有38条路由拆入http_api领域模块，应用独立共享上下文；统一code/message/detail且不回显校验原始输入。94项API/HTTP/接收/流式/生成/usage/工作区回归、Ruff与Mypy67模块通过；README/architecture/api-contract/ADR-056同步。Skills管理已补齐，索引/评测/交付长任务接口仍待实现，未标整体完成。 |
| A2 | 进行中 | Run/Continue持久幂等接收、重启重试、内容冲突及同源/DNS-rebinding防护已实现；43项HTTP/接收/API回归、Ruff/mypy52模块通过。api-contract/architecture同步；Skills导入/配置已接持久幂等与来源检查、配置竞争返回409；Delivery等继续按后续步骤接入。2026-10-16：容器本机转发精确peer启动配置已接管理守卫，额外peer仍强制回环Host/匹配Origin，禁用代理头信任；85项来源/API/Skills/CLI回归及Ruff/Mypy72模块通过，README/architecture/api-contract/ADR-061同步。真实镜像验证仍由F5记录。 |
| A3 | 已完成 | 生产Host全部实际目录/动态MCP/Worker接入严格契约，首次参数错误允许一次修正轮次，再次错误停机且不重放副作用；截断调用不派发。97项core及81项主链回归、Ruff/mypy55模块通过。README/architecture/api-contract/ADR-052同步；真实Docker命令与Skill脚本路径已由S4/F5补验。 |
| A4 | 已完成 | generation严格配置/能力声明/模型与会话API和三adapter映射；17项生成参数测试及59项相关回归通过，Ruff/mypy50模块通过。README/architecture/api-contract/ADR-050同步；页面在F迁移。 |
| A5 | 已完成 | admission冻结、零/null/最大输出/继承、Continue及并发工作区隔离验证通过；最大输出日志字段保留数值；辅助调用不继承采样覆盖。见test_generation及ADR-050。 |
| A6 | 已完成 | 2026-10-16：Host流式主链/有界脱敏预览SSE及3类官方SDK适配完成，完整终态前不派发工具；104项集成回归通过，真实DeepSeek普通/工具调用10次基线完成。预览不落库/不推进游标；页面在F。README/architecture/api-contract/ADR-053同步。 |
| A7 | 已完成 | 2026-10-16：跨分片密钥lookahead脱敏、超长密钥禁预览，取消/断流/超时不重发或执行半成品；未知能力接收前明确拒绝采样参数。104项相关集成覆盖，详见test_stream_runtime/test_provider_streaming/test_generation；最终页面验收在F/R。 |
| P1 | 已完成 | 2026-10-16：schema v4稳定Project/源Workspace/执行副本关联及只读聚合API，迁移保留旧身份与事件原文；接收/直接运行/Continue/Worker/交付固定项目归属。15项身份专项、82项主链相关回归、23项前端测试、build/typecheck、Ruff/Mypy73模块和smoke12/12通过；容器fixture启动期限修复后集成全量880通过/5个显式Docker跳过、覆盖93.78%。README/architecture/api-contract/ADR-060同步；项目身份已完成，并发进展见P2/P3。 |
| P2 | 已完成 | 2026-10-16：Plan持久快照与写Session固定worktree均接Host/HTTP；源逻辑身份不变，后续消息/Continue复用脏副本，同Session串行、不同副本模型与文件工具可重叠。实际副本工具/Skills/索引/worker/恢复及未知隔离、共享Git命令协调、交付准备/采纳短期源锁已落地。19项快照及12项写副本专项、含交付/Skills共52项定向回归，全量937通过/7个显式Docker跳过覆盖93.60%、Ruff/Mypy76模块、smoke12/12；另6项真实Docker全部通过（含Git包装器Skill）。README/architecture/api-contract/ADR-063同步；OpenAPI/Vue已集成，39项前端回归、5条真实API静态页面旅程及wheel隔离资源检查通过，实际文件验证两副本互不修改、源不变及刷新后续写同副本；集成136项后端定向回归、6项真实Docker、Ruff/Mypy79模块通过。持久队列及容量属P3。 |
| P3 | 进行中 | 2026-10-16：canonical队列已接Host/Python/HTTP/Delivery Writer，4根/20等待/2模型请求，新增GET /api/queue及queued状态；可靠冻结后接收、排队停止、控制锁释放补位、模型等待无预留、源码/Continue漂移拒绝和重启停驻均实现。200项相关回归及最终38项调度专项通过，scheduler/run_dispatch两模块100%覆盖、Ruff/Mypy81模块、help/smoke12/12通过；README/architecture/api-contract/ADR-065同步。Vue队列/queued取消/全局额度已接，42项前端回归与6条真实API静态页面旅程、typecheck/build/format、wheel和真实Docker新镜像（额度/重启/资源/非root/同源）通过。全量集成发现内部状态文件误判源码漂移及旧断言需修复，修复后重跑全量再标完成。 |
| P4 | 进行中 | 2026-10-16：随P2/P3落实精确Run/Session/执行副本取消、共享Git协调及根/模型所有权分离；4父任务与8只读worker共用2模型槽、5Delivery控制Run不重复占writer根名额、取消同步写待实际收尾通过。200项相关回归及最终38项调度专项通过；页面与最终生命周期全量由集成任务验收。 |
| P5 | 进行中 | 2026-10-16：每次主/摘要/worker尝试取得全局模型槽后才原子预留与结算，等待取消无attempt/预留，重试backoff释放槽；父子仍共享真实根/项目/交付预算，未知不释放。200项调度/预算/控制面回归及38项最终专项通过；最终并发页面与全量门禁由集成任务验收。 |
| S1 | 已完成 | 2026-10-16：严格元数据、资源布局和依赖诊断已接入Host/API及Vue；损坏固定版本可诊断并禁用，执行仍拒绝。70项Skills定向回归、完整782通过/1跳过且覆盖93.36%、Ruff/Mypy70模块、smoke12/12、23项前端回归及2条真实API浏览器旅程通过；README/architecture/api-contract/ADR-057同步。 |
| S2 | 已完成 | 2026-10-16：本地目录/ZIP/内置包导入与项目/用户作用域已接API和页面，安装不自动授权用户包；同名精确身份、恶意路径/链接/规模限制、持久幂等及重启未知不重放有测试。70项Skills回归及真实API导入旅程通过；稳定文档同步。 |
| S3 | 已完成 | 2026-10-16：admission/Continue/重启固定内容；页面支持用户版本固定、差异及回退，项目更新由Git管理。预览摘要复核防源漂移，配置摘要拒绝旧标签覆盖；资源篡改、恢复与加载记录持久化通过70项Skills回归和23项前端回归。README/architecture/api-contract同步。 |
| S4 | 已完成 | 2026-10-16：ZIP/资源边界、本地权限与真实Docker Python/Shell脚本主链已验收；安装不执行、Plan不开放脚本、参数按字面传递，固定资源跨UID可读且不改用户权限。修复自动删除与显式清理竞态，停止/超时确认清理、未知仍保守保留。67项容器/Skills/交付生命周期回归及全量880通过/5个默认离线跳过、覆盖93.78%，Ruff/Mypy73模块、smoke12/12通过；README/architecture/ADR-047同步。补充复核修复清理子进程启动失败的未知状态丢失；63项相关回归通过/1项显式Docker跳过，保留Skill资源且Host停驻。 |
| S5 | 进行中 | 2026-10-16：3内置包可显式导入；Vue区分安装/启用/选择/实际加载，资源分页、运行固定版本及刷新恢复已通过真实API旅程。完整冻结任务集效果与Token成本对照仍待B/U，未用页面验证替代效果评测。 |
| F1 | 已完成 | 2026-10-16：Vue3/TS/Vite/Router/Pinia及隔离OpenAPI导出/类型生成落地；18项HTTP/组件/SSE/状态回归、typecheck/build及真实API浏览器旅程通过。README/architecture/api-contract/ADR-055同步；静态挂载由F5承接。 |
| F2 | 进行中 | 2026-10-16：项目/会话、流式、审批/停止/Continue、附件、模型参数、导出、Skills及预算/费用已接真实API，Plan固定快照与写Session独立worktree均可在页面并发，固定副本刷新后继续使用。新增全服务队列页面和精确停止/取消，42项前端回归、typecheck/build/格式检查及6条真实API静态页面旅程通过；索引、评测、交付控制面仍待接入。 |
| F3 | 进行中 | 2026-10-16：刷新恢复、会话草稿、切页后台继续、多标签审批、项目快照并发与精确停止已验证；预算编辑不被轮询/旧响应覆盖，慢请求不被轮询饿死。未知请求保留原幂等键，明确拒绝可换键；39项前端回归和5条真实API旅程通过；轮询不重置执行选项，新Session不继承自动批准模式，绑定worktree不可切回源。队列页面已补齐并通过6对话实际旅程：4根/2模型/2等待、刷新、取消等待无usage和预留、停止后自动补位；前端42项回归与6条浏览器旅程通过。 |
| F4 | 进行中 | 2026-10-16：Run/Session/项目/UTC每日全历史用量与费用、独立尝试明细、已知/估算/未知及币种、固定价格依据、共享预算和人工核对已接Vue；35项前端回归及真实API预算阻止→关闭额度→Continue计价旅程通过。输入组成、高消耗聚合及交付页面仍待U3/D。 |
| F5 | 已完成 | 2026-10-16：/workbench/同源产物、源码/文件指纹、wheel资源及Docker多阶段分发完成。已有39项分发/API、23项前端、2条真实API页面旅程及wheel验收；本轮Apple Silicon Docker Desktop真实构建和Compose启动、非root/无需Node/全部资源hash/来源拒绝与同源管理/重建后会话与配置及文件恢复通过，未调用付费模型。CI加入同一容器分发及脚本检查；最新预算/快照页面镜像再次构建，3个资源hash/非root/管理来源/重启持久化通过，90项真实容器及相关回归通过，未调用付费模型。P3队列新镜像再次构建，资源/同源/非root/重启持久化及4根/2模型/20等待配置检查通过；测试容器及卷清理。README/architecture/ADR-058/061同步；旧页保留到X。 |
| B1 | 待开始 | 20 任务：理解4/定位4/单文件6/跨文件4/恢复交付2；开发12/留出8，调参后移出留出并补样本。 |
| B2 | 待开始 | commit/任务版本/模型参数/Skill/工具指纹/usage/价格版本，失败和未知不丢失。 |
| B3 | 待开始 | 开发单次、候选每题3次，相同配置对照，保留失败/预算停止。 |
| B4 | 待开始 | 冻结测试独立验收、标注引用；Web 发起，CI 共用 Python 接口；live 显式调用。 |
| B5 | 待开始 | 并发/参数/Skill/注入/恢复/用量专项回归，严重故障须回归。 |
| C1 | 待开始 | 内容哈希增量增删改，模型/切块缓存指纹；计算在事务外、提交复核代次与源码，失败保留旧索引。 |
| C2 | 待开始 | 可选固定本地模型和 CrossEncoder，最多重排20/返回5；缺模型明确降级、不伪造分数。 |
| C3 | 待开始 | 每根 Run 最多一次改写及一次补检索，统一预算，保留原查询候选。 |
| C4 | 待开始 | 至少40查询、开发/留出各20，词面/语义/混合/重排/改写消融。 |
| C5 | 待开始 | 片段占用/输入/cache/改写开销/下游结果；Recall@5或MRR提升另一不降、p95≤基线2倍、任务通过不降才晋升默认。 |
| D1 | 待开始 | DeliveryController 有界修复：初次+最多2次修正，总计3次候选验收。 |
| D2 | 已完成 | 2026-10-16：随P2落地独立writer checkout和每交付owner/管理Session；prepare、源码比对及apply短期协调source/Git，writer模型期不占源。并发交付、验收期间源漂移拒绝采纳、排队apply重读最新证据、取消后实际副作用收尾及writer重启恢复均通过；52项副本/交付/Skills回归、全量937通过/7跳过及6项真实Docker通过，architecture/README/ADR-063同步。D1/D3-D6有界流程仍待实施。 |
| D3 | 待开始 | 交付默认36步/60,000已观测Token/15分钟，摘要/子任务累计；恢复不清零。 |
| D4 | 待开始 | 通过/取消/超限/无进展/重复失败/保护变化/未知结果停止，等待副作用收尾。 |
| D5 | 待开始 | 固定验收/保护范围，patch/checkpoint绑定，新改动使旧验收失效；显式采纳，不自动提交/合并/推送。 |
| D6 | 待开始 | Vue目标/后端/轮次/失败/补丁/用量/停止/采纳，失败保留产物。 |
| X1 | 待开始 | CLI能力清单逐项承接，包括历史导入、worktree、export/trace/检索/评测/交付。 |
| X2 | 待开始 | 删除用户命令行产品和旧前端/专属死代码，不留转发空壳。 |
| X3 | 待开始 | 打包/Docker/示例/CI/文档同步；help/eval门禁改服务冒烟/Python接口，不降低范围。 |
| U1 | 进行中 | Host尝试身份/SDK禁隐式重试、provider归一化、去重账本、Run/Session全历史及子Run查询已接线；70项相关集成通过，未知/取消/父子/重启回归通过。流式终态已随A6接入，embedding接线待C；README/architecture/api-contract/ADR-049同步。 |
| U2 | 进行中 | 2026-10-16：原子预留/结算和价格快照已接Host主Agent、摘要、worker每次尝试；权威项目/交付归属、Continue共享桶与80%事件可用。新增预算/价格/未知处理HTTP控制面，严格本机同源、持久幂等与配置冲突保护；人工核对保留未知或补记总量，不修改Provider用量。41项HTTP专项、完整909通过/1跳过且覆盖93.70%、Ruff/Mypy76模块及smoke12/12通过；后续补充确定拒绝的持久响应标记，活跃请求结束后可用新键核对，未知409继续保留原键，74项预算/HTTP/Skills回归及Ruff/Mypy通过。README/architecture/api-contract/ADR-059/062/064同步。Vue预算/费用已接并通过浏览器旅程；P调度仍待完成；连接探测与API embedding尚未纳入，不标整体完成。 |
| U3 | 进行中 | 2026-10-16：Vue费用/预算/价格控制面已接入，项目用P1真实身份、全历史不依赖分页，未知不当零、币种与来源分开，保持配置草稿和幂等请求。35项前端测试、4条真实API页面旅程（含价格固定、预算阻止、Continue和快照并发）、typecheck/build/format及独立只读复核通过；集成后端965通过/5个显式Docker跳过、覆盖93.79%，Ruff/Mypy77模块通过。README/architecture/api-contract/ADR-064同步；输入组成、高消耗统计及交付页面待完成。 请求明细现透传已观测供应商/耗时/结束原因，历史缺失为null、不从墙钟推算；90项usage/预算回归、43项前端回归、真实API用量旅程、Ruff/Mypy81及typecheck/build通过。 |
| U4 | 待开始 | 随S/C/D：渐进加载/分页/去重/片段预算、摘要阈值间隔、减少重复委派、无进展停止、稳定缓存前缀，参数实验。 |
| U5 | 待开始 | 随B/R：冻结集单项及组合消融，成功计数/总Token及费用/延迟p50/p95/分类额外开销/缓存覆盖/失败与未知；总费用÷成功数，无成功不可算。 |
| R1 | 待开始 | 后端完整门禁≥85%覆盖，默认离线无密钥/网络。 |
| R2 | 待开始 | 前端typecheck/组件/build/Playwright。 |
| R3 | 待开始 | 并发/审批/队列/取消/重启/Skill版本/采纳冲突。 |
| R4 | 待开始 | usage采集去重/父子/预留结算/价格/全历史/未知。 |
| R5 | 待开始 | 冻结集对照与至少2个用户实际项目任务。 |
| R6 | 待开始 | 安装包/容器/API/Markdown一致，本地提交。Docker不可用单列待外部验证，不能声称已实测。 |

## 用量、预算与实验细则

- 每次真实请求尝试独立记账；主Agent/摘要/worker/查询改写/embedding分类。成功响应使用供应商usage；请求失败、取消或断流无usage保持未知。
- cache/read/write/reasoning细项按供应商协议归一化，不默认是互斥计费桶；非Token计费不强行并入。历史数据不补造精度。
- 账本从模型调用事实派生，终态与worker汇总不重复计。重复usage/SSE/重启重建须幂等。
- 预留/结算覆盖子任务；未知保持未结算，调整/处理留审计。Continue新Run预算不能绕过交付/项目/日预算。
- 价格按供应商/模型/类别/币种/生效日版本化，历史固定版本，重新估价另生结果；无价格为未知，不同币种不直接相加，不自动换汇。
- 页面流式生成显示用量待确认，本地估算单独标注；Skill正文占用不等于其整轮费用，字符预算不等于真实Token。
- 月预算按100元保守安排、接受范围100–300元；批量live先估算，不自动购买或切换收费供应商。费用不完整须同时显示未知调用数。
- 优化需冻结集通过数不降、无关键正确性回归且Token或费用下降；延迟和失败消费同报。不预承诺百分比，不以低输出/低成功率冒充节省。
- 不在本轮加入写任务回答缓存、语义缓存或自动模型路由。供应商缓存可能降费用/延迟，不能说必然减少输入Token。

## 后续队列（当前不执行）

1. MySQL/Redis/独立worker服务化：持久任务、outbox、重复消费、共享限流/取消；无第二事实源，不长期双写运行库。
2. Skills Git来源：固定提交、差异和来源管理。
3. Workspace长期记忆：来源/审核/冲突/过期/撤销。
4. 大工具目录按需发现。
5. 持续指标、脱敏反馈和备份恢复。
6. LangGraph仅外层对照实验。

启动后续能力前补齐需求、验收与维护成本，不在当前实现顺手扩张。全部必要阶段通过后才整理简历。
