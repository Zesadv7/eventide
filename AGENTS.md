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
- 前端 Vue 3/TypeScript/Vite/Vue Router/Pinia，HTTP/SSE，FastAPI 同源托管产物；Node 只用于开发构建。用户CLI已退役，保留部署入口和Python开发/CI接口。
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

- 新 Runtime 功能优先进入 `host.py`、`store.py`、`providers/`、`executor.py`、`policy.py`、`worktrees.py`、`mcp/client.py`、`api.py`、`http_api/` 或 `frontend/`。
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
uv run python tests/check_service.py
uv run pytest -q
uv run pytest --cov=eventide --cov-fail-under=85
uv run ruff check eventide tests examples
uv run mypy eventide
uv run python -m tests.check_offline_evaluations evals/smoke.yaml
```

完成后：

1. 更新受影响的稳定文档。
2. 运行 `git diff --check` 并复查 `git status --short`。
3. 创建一个说明清楚的本地提交；不要夹带无关修改。
4. 在最终回复中报告修改内容、验证结果、commit hash 和仍需用户处理的阻塞。

## 回复风格

- 回复稍微带点大白话：先说结论，再补细节，少堆术语。

## 升级步骤与状态

以下记录目标、当前状态与验收证据；只有已完成行代表已验收能力。每行证据包含实际实现、验证摘要、对应文档及遗留事项；子任务在自己的 worktree 更新，集成时核实状态。

| 编号 | 状态 | 实施与验收要求 / 证据 |
|---|---|---|
| 0.1 | 已完成 | 2026-10-15：本计划和逐步状态制度已写入本文件；README 过时 Subagent 表述已修正，架构/ADR 区分目标与现状；文档检查和 git diff --check。 |
| 0.2 | 已完成 | 2026-10-15：Host/live 共用配置优先级；live 只读解析已提交 WAL 配置、隔离前解密且不创建 Host/密钥，错误不回退。88 项定向回归、Ruff、Mypy 50 模块通过；README/architecture/ADR-051 已同步。真实调用另属 0.4。 |
| 0.3 | 已完成 | 2026-10-15：全部用例独立临时 Git 项目/数据库，live/offline 均关闭 MCP；失败产物保留，报告可逐例定位，凭据不写产物、用户源码/状态不变。88 项回归及 smoke12/tasks14/engineering3/workers1 全通过；README/architecture/ADR-051 同步。 |
| 0.4 | 已完成 | 2026-10-16：真实DeepSeek Responses非思考模式，普通/只读工具及3工程fixture各2次，共10次、7次完整完成；3次Token软限停驻且固定测试仍通过，6次修复独立验收均通过。累计131,359输入/7,630输出Token、48次请求、未知0；失败全保留，未改用户状态。README/architecture有摘要，不能外推成功率。 |
| A1 | 已完成 | 2026-10-16：项目/会话/Run/模型/Skills/索引/工程评测/交付/副本/历史导入领域路由与统一错误已齐，长任务持久接收后返回身份，支持查询/SSE/停止与重启停驻。此前完整1532项/覆盖93.47%；最终写路由144项、退役入口及相关165项定向回归通过，server/api覆盖100%；24条浏览器检查及wheel/真实Docker分发通过。README/architecture/api-contract同步；真实效果及全仓最终收口继续按B/U/R。 |
| A2 | 已完成 | 2026-10-16：审计36条POST/PUT/PATCH/DELETE路由（Skill preview只读），其余持久写入均有本机同源、内容绑定幂等及未知不重放。Run/Continue接收后缓存/派发失败仍回放原202身份，Skill实际发布/回执故障保留503未知；Vue未标记409保留请求、明确拒绝可换键。144项关联回归、模块91.63%，117项前端、24条浏览器检查，Ruff/Mypy113通过；ADR-025补充和稳定文档同步，主分支最终产物及R门禁继续单列。 |
| A3 | 已完成 | 生产Host全部实际目录/动态MCP/Worker接入严格契约，首次参数错误允许一次修正轮次，再次错误停机且不重放副作用；截断调用不派发。97项core及81项主链回归、Ruff/mypy55模块通过。README/architecture/api-contract/ADR-052同步；真实Docker命令与Skill脚本路径已由S4/F5补验。 |
| A4 | 已完成 | 2026-10-16：GLM-5.3-Flash精确端点能力、Chat推理映射、仅开启思考/采样精度/输出范围与Vue约束已补齐；服务默认也接收前校验。97项后端定向、generation覆盖94.25%、116项前端、3条真实API设置页旅程、typecheck/build、Ruff/Mypy113通过；真实普通/只读工具共3次请求完成，输入2746/输出49/缓存读取832、未知0，不作为冻结集效果。README/architecture/api-contract/ADR-050同步。 generation严格配置/能力声明/模型与会话API和三adapter映射；17项生成参数测试及59项相关回归通过，Ruff/mypy50模块通过。README/architecture/api-contract/ADR-050同步；页面在F迁移。 |
| A5 | 已完成 | admission冻结、零/null/最大输出/继承、Continue及并发工作区隔离验证通过；最大输出日志字段保留数值；辅助调用不继承采样覆盖。见test_generation及ADR-050。 |
| A6 | 已完成 | 2026-10-16：Host流式主链/有界脱敏预览SSE及3类官方SDK适配完成，完整终态前不派发工具；104项集成回归通过，真实DeepSeek普通/工具调用10次基线完成。预览不落库/不推进游标；页面在F。README/architecture/api-contract/ADR-053同步。 |
| A7 | 已完成 | 2026-10-16：跨分片密钥lookahead脱敏、超长密钥禁预览，取消/断流/超时不重发或执行半成品；未知能力接收前明确拒绝采样参数。104项相关集成覆盖，详见test_stream_runtime/test_provider_streaming/test_generation；最终页面验收在F/R。 |
| P1 | 已完成 | 2026-10-16：schema v4稳定Project/源Workspace/执行副本关联及只读聚合API，迁移保留旧身份与事件原文；接收/直接运行/Continue/Worker/交付固定项目归属。15项身份专项、82项主链相关回归、23项前端测试、build/typecheck、Ruff/Mypy73模块和smoke12/12通过；容器fixture启动期限修复后集成全量880通过/5个显式Docker跳过、覆盖93.78%。README/architecture/api-contract/ADR-060同步；项目身份已完成，并发进展见P2/P3。 |
| P2 | 已完成 | 2026-10-16：Plan持久快照与写Session固定worktree均接Host/HTTP；源逻辑身份不变，后续消息/Continue复用脏副本，同Session串行、不同副本模型与文件工具可重叠。实际副本工具/Skills/索引/worker/恢复及未知隔离、共享Git命令协调、交付准备/采纳短期源锁已落地。19项快照及12项写副本专项、含交付/Skills共52项定向回归，全量937通过/7个显式Docker跳过覆盖93.60%、Ruff/Mypy76模块、smoke12/12；另6项真实Docker全部通过（含Git包装器Skill）。README/architecture/api-contract/ADR-063同步；OpenAPI/Vue已集成，39项前端回归、5条真实API静态页面旅程及wheel隔离资源检查通过，实际文件验证两副本互不修改、源不变及刷新后续写同副本；集成136项后端定向回归、6项真实Docker、Ruff/Mypy79模块通过。持久队列及容量属P3。 |
| P3 | 已完成 | 2026-10-16：canonical持久队列接Host/Python/HTTP/Delivery Writer，4根/20等待/2模型额度；排队查询/取消、容量拒绝、源漂移复核、重启停驻与Vue刷新/自动补位完成。修复精确Runtime产物指纹及旧测试替身，源码/已跟踪文件/Skill保护保留。集成完整1180通过/7个默认Docker跳过、覆盖93.90%，另76项真实Docker及相关生命周期全部通过；6条真实API静态页面旅程、48项前端回归、typecheck/build/format、Ruff/Mypy87、help/smoke12/12、wheel及新Docker分发通过。README/architecture/api-contract/ADR-065同步，无付费调用。 |
| P4 | 已完成 | 2026-10-16：精确Run/Session/执行副本取消、共享Git短期协调、根与模型所有权分离完成；4父任务/8worker共用2模型槽，Delivery控制Run不重复占writer根名额，取消等待真实副作用收尾。完整1180通过、覆盖93.90%，76项真实Docker及生命周期回归、6条真实API并发/队列旅程通过；源码漂移和采纳冲突保持拒绝，README/architecture/ADR-065同步。 |
| P5 | 已完成 | 2026-10-16：主Agent/摘要/worker每次尝试先取得全局模型槽再原子预留与结算，等待取消无attempt和预留，backoff释放槽；父子共用真实根/项目/交付预算，未知不释放。完整1180通过、覆盖93.90%，共享预算及6对话队列真实API旅程通过；README/architecture/api-contract/ADR-059/065同步。 |
| S1 | 已完成 | 2026-10-16：严格元数据、资源布局和依赖诊断已接入Host/API及Vue；损坏固定版本可诊断并禁用，执行仍拒绝。70项Skills定向回归、完整782通过/1跳过且覆盖93.36%、Ruff/Mypy70模块、smoke12/12、23项前端回归及2条真实API浏览器旅程通过；README/architecture/api-contract/ADR-057同步。 |
| S2 | 已完成 | 2026-10-16：本地目录/ZIP/内置包导入与项目/用户作用域已接API和页面，安装不自动授权用户包；同名精确身份、恶意路径/链接/规模限制、持久幂等及重启未知不重放有测试。70项Skills回归及真实API导入旅程通过；稳定文档同步。 |
| S3 | 已完成 | 2026-10-16：admission/Continue/重启固定内容；页面支持用户版本固定、差异及回退，项目更新由Git管理。预览摘要复核防源漂移，配置摘要拒绝旧标签覆盖；资源篡改、恢复与加载记录持久化通过70项Skills回归和23项前端回归。README/architecture/api-contract同步。 |
| S4 | 已完成 | 2026-10-16：ZIP/资源边界、本地权限与真实Docker Python/Shell脚本主链已验收；安装不执行、Plan不开放脚本、参数按字面传递，固定资源跨UID可读且不改用户权限。修复自动删除与显式清理竞态，停止/超时确认清理、未知仍保守保留。67项容器/Skills/交付生命周期回归及全量880通过/5个默认离线跳过、覆盖93.78%，Ruff/Mypy73模块、smoke12/12通过；README/architecture/ADR-047同步。补充复核修复清理子进程启动失败的未知状态丢失；63项相关回归通过/1项显式Docker跳过，保留Skill资源且Host停驻。 |
| S5 | 进行中 | 2026-10-16：Web/Python评测增加none/engineering显式预设，quote只读绑定3个内置包摘要，接收固定原资源，排队/重启复验不漂移；每case及Delivery Writer复用用户包启用和Run冻结，真实加载与选择分开。57项后端回归（8项新专项，profile覆盖95%）含20题scripted机制通过，121项前端、3条真实API浏览器旅程、typecheck/Vite构建、Ruff/Mypy115、help/service/smoke12通过。README/architecture/api-contract/ADR-089同步；无付费调用，S5真实效果仍待B3/U5，不标整体完成。 2026-10-16：3内置包可显式导入；Vue区分安装/启用/选择/实际加载，资源分页、运行固定版本及刷新恢复已通过真实API旅程。完整冻结任务集效果与Token成本对照仍待B/U，未用页面验证替代效果评测。 |
| F1 | 已完成 | 2026-10-16：Vue3/TS/Vite/Router/Pinia及隔离OpenAPI导出/类型生成落地；18项HTTP/组件/SSE/状态回归、typecheck/build及真实API浏览器旅程通过。README/architecture/api-contract/ADR-055同步；静态挂载由F5承接。 |
| F2 | 已完成 | 2026-10-16：Vue已承接项目/多会话、运行/审批/参数/附件、Skills、索引、离线及规划确认live评测、交付采纳、副本、历史导入、历史/trace/JSONL、用量/预算/价格、全服务队列与设置。117项前端、24条完整浏览器检查（23真实API/1布局）及typecheck/格式通过；旧终端底层能力迁至Python开发接口后退役。主分支另补交付用量及GLM页面，最终资源统一重建；效果验收B3/U5不由页面替代。 |
| F3 | 已完成 | 2026-10-16：刷新和同键未知重试、切页后台执行、项目迟到响应隔离、多标签配置/元数据竞争、精确审批/停止/Continue、排队取消及自动补位均有真实API旅程。117项前端及24条浏览器检查通过，含320px布局；Run/Continue接收后故障不产生第二意图，Skills未知发布不重放。README/architecture/api-contract同步；全仓与最终静态资源由R统一复核。 |
| F4 | 已完成 | 2026-10-16：Run/Session/项目/UTC每日及精确Delivery全历史用量、费用、输入组成、独立attempt、已知/估算/未知/币种/价格依据与共享预算均接Vue。交付链接按delivery累计全部轮次、摘要和worker，避免只看某个控制Run；119项前端、build/typecheck及真实API交付/采纳/累计费用/刷新不累加/320px旅程通过，既有账本与预算专项继续保留。README/architecture同步；质量成本收益仍按U5，不将页面统计当优化效果。 |
| F5 | 已完成 | 2026-10-16：/workbench/同源产物、源码/文件指纹、wheel资源及Docker多阶段分发完成。已有39项分发/API、23项前端、2条真实API页面旅程及wheel验收；本轮Apple Silicon Docker Desktop真实构建和Compose启动、非root/无需Node/全部资源hash/来源拒绝与同源管理/重建后会话与配置及文件恢复通过，未调用付费模型。CI加入同一容器分发及脚本检查；最新预算/快照页面镜像再次构建，3个资源hash/非root/管理来源/重启持久化通过，90项真实容器及相关回归通过，未调用付费模型。P3队列新镜像再次构建，资源/同源/非root/重启持久化及4根/2模型/20等待配置检查通过；测试容器及卷清理。README/architecture/ADR-058/061同步；旧页保留到X。 最新用量/连接测试页面统一重建，wheel脱离源码导入/资源检查与真实Docker镜像的3资源指纹、非root/无需Node、管理来源和重启恢复全部通过。 B4新版镜像追加12题真实离线API机制验证、同键去重及重启报告一致性，wheel内21个冻结任务资源验证已加入CI。 |
| B1 | 已完成 | 2026-10-16：严格版本化manifest冻结20任务（理解4/定位4/单文件6/跨文件4/恢复交付2）、12开发/8留出和家族隔离；历史engineering三题仅归开发。事实/引用、独立冻结测试、真实快照/worktree及重启/采纳漂移机制验证通过；33项冻结集专项、原有评测33项回归通过，完整1027通过/7个显式Docker跳过覆盖93.42%，最后验收标记收紧后8项复核通过、Ruff/Mypy81和smoke12/12通过。README/architecture/ADR-066同步；20/20仅为scripted机制验证，不是模型成功率；B2/B3/B4效果与Web另验收。 |
| B2 | 已完成 | 2026-10-16：报告schema v2固定Runtime commit/dirty/code指纹、任务输入/版本、Run/Session/Turn、实际生成参数、Skill/工具指纹、独占尝试usage及费用/价格历史；JSON只是各case唯一Store的可重建导出。逐题原子报告、失败/超时/重复取消/证据缺失保留，live故障停止扩大批次；全部suite默认整例300秒/12步/20k observed，收尾后再传播取消。完整1050通过/7个显式Docker跳过覆盖93.43%，最后独立复核修正后51项评测回归（含21项证据专项）、Mypy82/Ruff和smoke12/12通过；README/architecture/ADR-066同步。没有真实模型调用，B3/B4/U5效果与Web仍待验收。 |
| B3 | 进行中 | 2026-10-16：GLM完整开发首轮12题已完成、7通过（保留3个格式失败、1引用失败、1Token上限停驻），72请求reported输入115783/输出3943、unknown0；缓存读取99136占已报告输入85.62%，覆盖72/72。任务p50/p95 30.58/52.95秒，请求4.56/6.98秒；费用72次均无价格，不能计算费用/成功数。不以单次开发集外推成功率，候选20题各3次与单项/组合消融仍待执行。 2026-10-16：GLM-5.3-Flash low/温度1/输出8000真实开发批次已启动；两次临时监视脚本游标错误中断分别保留5/3次请求，随后标准等待批次在第2题连接错误中止、保留8次请求/10题未尝试。三批reported输入15709/输出516、unknown尝试3，均未作为完整成功率；第1题均未通过严格JSON格式。无密钥HTTP连通与单次完整Host文件工具复核通过（2次请求），下一完整批次仍待完成。README同步，原始报告在忽略目录。开发单次、候选每题3次，相同配置对照，保留失败/预算停止。 |
| B4 | 进行中 | 2026-10-16：Web默认offline、live显式只读规划/模型价格任务profile指纹确认、同源幂等接收和持久Run/SSE/停止/重启停驻已接主Host；4根/20等待、2模型槽及项目/日预算统一，case只存工具/验收事实及权威调用引用。此前89项桥接、62项执行、21项规划专项和12题脚本主链通过；新增none/engineering固定Skill版本资源，57项相关及20题完整scripted机制通过。主分支69项worker/profile/控制/统计集成、121项前端、24条真实浏览器检查及构建类型格式通过；真实GLM12题首轮7通过，最终重复与成本效果仍归B3/U5。README/architecture/api-contract/ADR-082/089同步，未用页面或脚本通过代替模型效果。 |
| B5 | 已完成 | 2026-10-16：新增7项源码/Skill/检索提示注入端到端专项，模拟模型主动听从恶意内容仍无法越过Plan、Auto ASK默认拒绝、只读worker及项目预算配置边界；真实内容已进入后续模型请求，文件和预算不变。与并发worker/参数/Skills/接收恢复/用量预算合计218项回归通过，Ruff通过。README/architecture同步；这是确定性执行边界验证，不宣称模型识别注入的成功率。 并发/参数/Skill/注入/恢复/用量专项回归，严重故障须回归。 |
| C1 | 已完成 | 2026-10-16：保留内容哈希增删改、模型/切块指纹、事务外计算及提交前源码/代次复验；补齐源码祖先链接、有界读取、索引目录/数据库/附属文件链接与替换拒绝，损坏指纹不静默复用。35项新增边界/增量专项与全部检索/结构回归共102项通过，index覆盖92.41%，全仓Ruff/Mypy88、help、smoke12/12及离线检索16/16通过。README/architecture/ADR-069同步；仅本地Hash验证机制，无真实embedding调用，Web控制面与语义效果分别按A1/C2–C5验收。 |
| C2 | 已完成 | 2026-10-16：固定本地模型准备/内容指纹、可选sentence-transformers6.1.0、CPU惰性加载及Host重排接线完成；最多20候选/5结果，独立重排分数，缺模型/失败明确降级，默认仍Hash。177项相关回归通过/1项显式真实模型跳过，检索及结构143项覆盖93.28%；另安装隔离依赖并用固定MiniLM/TinyBERT模型在禁止网络连接下完成真实embedding、索引、重排和Host审计工具调用。Ruff/Mypy93、help/smoke12/12、检索16/16、lock及wheel隔离资源通过。README/architecture/api-contract/ADR-071同步；实测macOS本地CPU，默认Docker不含模型/重依赖，C4/C5效果另行验收；远程embedding账本已由U1/U2接入。 |
| C3 | 已完成 | 2026-10-16：默认关闭的单次改写/补检索接入统一模型槽与预算，根Run/worker共用持久认领；一次失败不重试，取消/预算拒绝保持控制语义，索引漂移/无效输出保留原候选，按RRF去重最多5条。86项相关回归通过、改写/派发覆盖93.08%，Ruff/Mypy95通过；README/architecture/api-contract/ADR-073同步。没有付费调用，效果与默认晋升仍待C4/C5。 |
| C4 | 已完成 | 2026-10-16：冻结四路480查询及GLM改写40查询真实对照完成，原有全部失败/降级保留；最终97项检索集成通过，修正导出缺事实、全部截断原因及价格归属。排名、耗时和用量证据见本行历史与README/architecture/ADR-074；下游配对效果另属C5，当前没有配置获得默认晋升。 此前证据：2026-10-16：固定16语料/40查询及20开发/20留出，真实MiniLM/TinyBERT四路3次共480查询证据保留；留出词面Recall/MRR .80/.75、语义.85/.85、混合.85/.80、重排.85/.7292，语义延迟超过词面2倍，未晋升。新增受控改写共享真实排名、根队列/模型槽/预算与唯一账本，21项新专项、108项关联及主分支56项集成通过。真实GLM40查询各1次完成：22有效/18降级，unknown0，输入2853/输出7877（reasoning7589为子集）；原/融合Recall .925/.950、MRR .900/.9083，留出.85/.90与.800/.8417；纯检索p95 139.90/260.80ms，含模型总p95 9155.67ms。费用未知，回退排名通过不等于改写有效；导出/全部截断/未知覆盖及Session费用边界已补修，下游C5仍在集成，默认关闭。README/architecture/ADR-074同步。 |
| C5 | 进行中 | 2026-10-16：默认关闭的配对文件选择/逐字引用对照机制已实现：既有40查询/标签、固定严格JSON与512/空采样覆盖、真实源行独立判分；同Host原/候选attempt分账，候选含改写开销，Session级价格按精确attempt固定。格式/引用失败继续，未知usage/网络/预算/取消停止；两臂遵守根observed软限，整行判分后脱敏。165项关联回归通过，最终96项回归覆盖96.81%，另6项最终软限/未知/价格读取/密钥脱敏专项通过，Ruff/Mypy115通过。README/architecture/api-contract/ADR-074同步，无付费调用；真实配对质量/成本效果仍待验收，不将窄引用任务称为工程修复效果。 片段占用/输入/cache/改写开销/下游结果；Recall@5或MRR提升另一不降、p95≤基线2倍、任务通过不降才晋升默认。 |
| D1 | 已完成 | 2026-10-16：复用DeliveryController/独立Writer/Verifier实现首次+最多2轮修正，失败输出反馈、候选patch与唯一验收call绑定；最多3次候选验收，不接受模型自评。99项交付/生命周期/预算回归通过、相关模块覆盖90%，另1项真实Docker两轮修复/验收/人工采纳通过，Ruff/Mypy101与smoke12/12通过。README/architecture/api-contract/ADR-076同步；Web仍按D6。 |
| D2 | 已完成 | 2026-10-16：随P2落地独立writer checkout和每交付owner/管理Session；prepare、源码比对及apply短期协调source/Git，writer模型期不占源。并发交付、验收期间源漂移拒绝采纳、排队apply重读最新证据、取消后实际副作用收尾及writer重启恢复均通过；52项副本/交付/Skills回归、全量937通过/7跳过及6项真实Docker通过，architecture/README/ADR-063同步。D1/D3-D6有界流程已完成，真实模型效果另按B/U/R。 |
| D3 | 已完成 | 2026-10-16：交付固定36次模型尝试/60k已报告Token/900秒，统一请求预留前检查，摘要/worker/改写/重试及embedding消费跨根Run累计；新Run/Continue/关闭所有者/重启不重置。36次混合调用、embedding输入限额、绝对deadline和终态关闭专项通过；99项关联回归、限额模块100%覆盖，真实Docker验证通过。README/architecture/ADR-076同步；清理允许超时后继续收尾，不承诺账单硬限。 |
| D4 | 已完成 | 2026-10-16：通过/取消/超限/无源码进展/重复验收失败/保护变化/未知工具结果均停止；重复取消等待Provider与真实工具清理，未知保留账本和产物。99项交付/生命周期/预算回归、并发独立副本/重启/超时/未知writer及verifier专项通过；真实Docker两轮闭环通过。README/architecture/api-contract/ADR-076同步。 |
| D5 | 已完成 | 2026-10-16：开始固定checks/image/保护范围，每轮patch/checkpoint和验收绑定，旧run/verify禁止追加候选或放宽条件；修复成功后仍显式apply，候选修改/源漂移及并发采纳冲突拒绝。99项关联回归，重启保留通过证据、篡改与并发采纳专项及真实Docker修正后人工采纳通过；README/architecture/api-contract/ADR-076同步，未自动commit/merge/push。 |
| D6 | 已完成 | 2026-10-16：持久同源幂等HTTP与Vue创建/有界修复/轮次验收/补丁确认/人工采纳/精确停止已闭环；四交付Writer借用各自控制根名额、共享两模型槽，取消等待真实清理，刷新未知同键重试。修复源项目配置串线、Mac路径别名及快照文件名误脱敏。55项交付/快照回归、9项控制专项（相关模块88.92%）、完整1460通过/10显式跳过覆盖93.54%、Ruff/Mypy103、smoke12通过；92项前端、17条浏览器检查（16真实API/1布局）、typecheck/build/format与320px复核通过；wheel隔离资源、新Docker准备/重启/幂等及真实Docker修复采纳通过。README/architecture/api-contract/ADR-077同步；未调用付费模型，模型效果仍归B/U/R。 |
| X1 | 已完成 | 2026-10-16：全部旧命令已由Vue/API或Python开发接口承接，清单见下方；原Host/Store/trace/计划/导入/检索等底层验收先迁移，90项迁移关联测试通过，最终165项入口/历史/检索/观测回归通过。任意suite、手动交付验收、OTLP等开发能力保留，不维护第二终端产品。README/architecture/ADR-085同步。 |
| X2 | 已完成 | 2026-10-16：删除cli.py、10份原生web资源、旧截图、专属JS/UI检查和旧浏览器启动器；API空壳导出及探测转发移除，原底层测试迁移后保留。eventide-server及python -m eventide只启动单一FastAPI服务，根307跳转/workbench/，旧assets为404；165项关联、117项前端及24条浏览器检查通过，生产模块全量无密钥/网络导入检查通过，Ruff/Mypy113与静态检查通过。 |
| X3 | 已完成 | 2026-10-16：最终集成版OpenAPI/Vue及干净wheel已复核，21个冻结任务资源与隔离包检查通过；真实eventide:final-validation镜像验证3资源hash、非root/无需Node、同源管理、索引及交付准备幂等重启、4根/2模型/20等待和12题离线报告恢复，外部调用0。54项真实容器执行专项通过；README/architecture同步，退役入口不再进入包。 此前证据：2026-10-16：主分支已统一重建OpenAPI/Vue，119项前端与24条浏览器旅程通过；干净wheel隔离检查通过。检查捕获setuptools旧build缓存混入已删除CLI，清理确认忽略缓存后重建；wheel门禁新增精确资源清单和退役入口拒绝，6项分发回归通过。部署/CI/Python离线接口已迁移，此前x-validation真实Docker的非root/无需Node/同源/12题离线/幂等/重启和零模型调用证据保留；Skills评测新入口集成后需再次统一构建并验最终镜像，未标整体完成。 |
| U1 | 已完成 | 2026-10-16：主Agent/摘要/worker/连接探测/查询改写/远程检索及索引批量embedding均接唯一attempt账本，SDK隐式重试禁用，缺usage为unknown，父子/终态不双计，输入型调用无输出。查询改写86项回归，索引103项关联与13项专项、模块覆盖91.09%；此前完整1421项通过、覆盖91.90%，88项前端和16条浏览器检查通过，wheel与真实Docker索引/重启验证通过。README/architecture/api-contract/ADR-049/072/073/075同步；新全量收口仍按R执行，无付费模型调用。 |
| U2 | 已完成 | 2026-10-16：原子预留/结算、共享根/项目/交付/UTC日预算、固定价格与未知人工核对已覆盖主Agent/摘要/worker/探测/查询改写/embedding查询及索引批次；模型槽等待不预留，失败消费不丢失，未知不释放，重启不重置。此前预算HTTP41项及完整909项/P阶段1180项通过；新增索引103项关联与13项专项通过，远程批量实际派发替身核实项目归属，无额外generation。88项前端/16条浏览器、真实Docker重启通过。README/architecture/api-contract/ADR-059/062/064/075同步；有界交付总限及页面已按D/U3验收，不承诺供应商账单硬限。 |
| U3 | 已完成 | 2026-10-16：Vue已提供Run/Session/项目/UTC日/Delivery全历史投影、独立请求明细、未知值、固定价格与预算、字符组成、高消耗/摘要频次/失败观察。交付专用范围使用既有API，不另建账本；119项前端、typecheck/build、1条实际API有界交付/人工采纳/完整调用明细/刷新及320px旅程通过；前期168项usage/预算/probe和178项组成关联回归已有证据，最后统一门禁按R。README/architecture同步。 |
| U4 | 进行中 | 2026-10-16：重复只读委派已在全批派发前检查，同Run/显式Continue链的相同角色、精确目标和可信源码快照拒绝并返回已有父工具/child身份；从子admission重建，重启及worker.started缺口保留，旧无指纹事实不补猜。源码/角色/目标变化及普通新Run可用，批内重复零派发、不消耗四worker额度，已知凭据不复制到子消息。14项新专项及累计125项关联验证通过，worker定向41项覆盖96.83%、Ruff/Mypy115、help及smoke12/12/workers1/1通过；README/architecture/api-contract/ADR-088同步，无付费调用、未宣称Token收益。 2026-10-16：普通Run连续三次同工具/参数/结构化失败且可验证源码无进展时停驻；窗口从canonical结果与checkpoint重建，Continue/重启保留证据，同批未派发调用只补abandoned。成功读取、参数/错误/源码进展和取消/预算/未知控制均验证；Error:文件正文保留原展示而不进入新窗口。17项新专项及183项关联回归通过/1项显式Docker跳过，tool_loops覆盖96.55%，Ruff/Mypy114、help/smoke12通过；README/architecture/api-contract/ADR-086同步，无付费调用。此前摘要原子认领、后续4,000新增完成字符和60秒非阻塞冷却已通过15专项及110关联回归，context_builder覆盖91.59%，详见ADR-084。其余渐进加载/分页/检索去重/片段预算、重复委派和参数实验继续验收，未宣称真实成本收益，U4整体不标完成。 检索最多5片段/每片800/合计4,000源字符、明确截断与回读已通过94项及49项定向验证；摘要认领及检索预算不冒充Token收益。 |
| U5 | 进行中 | 2026-10-16：新增schema v2只读全尝试统计和预声明variant比较，独占身份去重、未知/估算/币种/固定价格保留，父子与cache/reasoning不双计，逐题质量及实际请求参数保护，延迟同时披露；修正恢复交付的真实repair_by来源。52项评测/权威/证据/验收关联回归通过，随后29项专项通过、模块覆盖90.51%，全仓Ruff/Mypy114通过；真实GLM开发报告只读核验7/12、72请求、119,726总Token，72项费用均未知，不新增付费调用。README/architecture/ADR-083同步；后续32项专项通过、覆盖90.49%：费用比较核对完整计价行费率指纹，不能用同名版本掩盖改价；variant须早于全部candidate请求，允许先采集开发baseline，禁止候选后补声明，留出不得选优化/调参。真实单项/组合消融和最终候选重复仍待B3/U5，不能标整体完成。 随B/R：冻结集单项及组合消融，成功计数/总Token及费用/延迟p50/p95/分类额外开销/缓存覆盖/失败与未知；总费用÷成功数，无成功不可算。 2026-10-16补验：实验身份排除交付随机writer_delivery_id，仍比较local/docker等执行配置；原ID保留于报告，避免重复实验被随机身份错误拒绝。33项统计专项及Ruff/Mypy通过。 |
| R1 | 进行中 | 2026-10-16：开始当前集成版本后端全量pytest、覆盖率≥85%、Ruff/Mypy及无密钥离线服务/评测门禁，结果待完成。 |
| R2 | 已完成 | 2026-10-16：集成固定Skill preset后重新从真实OpenAPI生成TS类型，修正默认preset可省略输入及必返详情类型；121项组件/API测试、24条浏览器检查（23真实API/1布局）、typecheck/Vite构建及Prettier全部通过。浏览器覆盖实际Skills、同项目快照/写副本并发、共享队列预算、审批/停止/Continue、交付采纳、模型配置及评测未知请求刷新，包含320px。产物与manifest同步；README/architecture/api-contract已有对应能力，最终容器与全仓仍按R1/R6。 |
| R3 | 已完成 | 2026-10-16：最终集成后端1742项通过/10项可选路径跳过，另54项真实容器专项通过；121项前端和24条浏览器检查覆盖并发副本、精确审批取消、排队补位、重启停驻、固定Skill版本及采纳冲突。真实镜像12题离线/幂等/报告重启一致通过；不以这些机制结果代替真实模型质量。README/architecture同步。 此前证据：2026-10-16：开始当前集成版本并发/审批/队列/取消/重启/Skill固定版本/采纳冲突的全量生命周期复验，结果待完成。 |
| R4 | 已完成 | 2026-10-16：最终集成1742项后端通过，包含权威attempt去重、父子归属、预算竞争/未知预留、固定价格及历史投影；新检索配对层97项集成通过，覆盖两臂软限、未知覆盖、Session价格按attempt读取及判分后脱敏。24条浏览器含费用刷新不累加，真实镜像恢复通过。价格未知继续显示未知，不承诺账单上限。README/architecture同步。 此前证据：2026-10-16：开始当前集成版本用量/父子/预留/价格及新检索双臂未知/软限/脱敏复验，结果待完成。 |
| R5 | 进行中 | 2026-10-16：以Eventide历史源码b238620的HTTP接收/GLM参数两个真实问题做独立worktree修复重放；先冻结外部验收，旧源码各2项失败、当前实现10项通过。真实模型两任务均未形成候选：HTTP加载2个固定Skill后第二请求约5秒超时（2尝试/unknown1），GLM加载2个固定Skill后10请求触达60k observed上限；合计已报告输入65,317/输出1,718，费用未知，源码/冻结测试未改、未采纳。失败全保留，不宣称修复成功；据此补齐三adapter SDK传输超时与Host配置一致，78项相关回归通过。冻结集对照仍按B3/U5验收。 |
| R6 | 进行中 | 2026-10-16：开始当前集成版本wheel及真实Docker重建、资源/部署/Markdown一致性复验，结果待完成。 |

## 用量、预算与实验细则

- 2026-10-16 用户将本轮付费验证改为 BigModel `glm-5.3-flash`，撤销本轮100元上限（不授权购买或换供应商）。隔离状态保留加密凭据；普通API一次1113余额不足、用量未知；按用户要求切换Coding Plan端点后一次最小请求成功，reported输入897/输出44，其中推理41不重复累计。官方仅支持指定工具，Eventide未列入，不将连通性当作订阅授权、工具能力或B3效果验收；已按用户授权继续后续隔离实验，全部失败和未知均保留；套餐费用未知，不套普通API价格。README同步，原始日志在忽略目录。

- 2026-10-16 U1/U2 远程embedding异步派发底座已实现：共享模型槽、显式单次SDK调用、输入用量归一化、预算拒绝及重复取消等待请求/客户端清理；54项派发/账本专项、Ruff/Mypy91通过。这是底座完成时的历史记录；检索Host及索引批次现已由ADR-072/075接线。F2历史页集成补验：78项前端回归、构建/类型/格式、1条真实API历史旅程及3项分发检查通过，静态产物同步。
- 2026-10-16 U1/U2 补充底座已验收：embedding预留固定input_only，Provider输入/缺失输出、预算结算及价格口径一致，未知/估算不释放；198项相关离线回归、Ruff/Mypy90通过。README/architecture/api-contract同步；远程检索与索引派发已由ADR-072/075接入，阶段状态见U1/U2。
- 每次真实请求尝试独立记账；主Agent/摘要/worker/查询改写/embedding分类。成功响应使用供应商usage；请求失败、取消或断流无usage保持未知。
- cache/read/write/reasoning细项按供应商协议归一化，不默认是互斥计费桶；非Token计费不强行并入。历史数据不补造精度。
- 账本从模型调用事实派生，终态与worker汇总不重复计。重复usage/SSE/重启重建须幂等。
- 预留/结算覆盖子任务；未知保持未结算，调整/处理留审计。Continue新Run预算不能绕过交付/项目/日预算。
- 价格按供应商/模型/类别/币种/生效日版本化，历史固定版本，重新估价另生结果；无价格为未知，不同币种不直接相加，不自动换汇。
- 页面流式生成显示用量待确认，本地估算单独标注；Skill正文占用不等于其整轮费用，字符预算不等于真实Token。
- 原保守月预算为100–300元；本轮用户已明确撤销此上限，仍不授权购买服务或换供应商。批量live先估算，不自动购买或切换收费供应商。费用不完整须同时显示未知调用数。
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


## X1 旧入口承接核对（2026-10-16）

| 旧能力 | 当前承接 | 剩余验收 |
|---|---|---|
| chat/run/continue/abandon | Vue对话、Run/Continue/恢复API | 已承接，服务部署门禁见X3 |
| plan show | 会话task_plan与Vue任务证据 | 保留Python状态查询，无需另一终端入口 |
| workspace list/add/show/remove | Vue项目管理、注册/切换/确认移除及原子幂等API | 72项后端、112项前端、22条浏览器及真实容器/重启通过；全仓1511通过/93.54% |
| worktree list/create/keep/remove | 同一Host管理、持久控制API与Vue工作副本页 | 已承接，完整后端1474通过/93.55%、19条浏览器及真实容器验证 |
| deliver prepare/run/verify/show/apply | 有界修复API/页面；Python独立验收接口 | D6已承接；手动操作保留Python开发接口 |
| export | 历史Run事件JSONL下载 | 已承接，保留Python导出接口 |
| trace / OTLP | telemetry.py投影、精确Run/子任务trace API与历史页JSON下载、显式Python导出 | 已承接；OTLP仅显式开发接口 |
| migrate-v02 | 同一migration模块、Web受控预检/确认导入及只读历史消息 | 已承接，106项关联/20条浏览器/真实容器验证 |
| rag build/search/eval | 索引页面、Agent search_knowledge、Python检索评测 | 默认远程路径继续统一Host预算，删除终端专属适配 |
| eval | Web离线机制评测、Python评测 | Web真实模型已承接；冻结集效果归B3/U5 |
| 原serve部署 | FastAPI同源服务与Docker | 已改独立部署入口，最终资源集成门禁见X3 |

会话重命名/归档/空会话删除已由Vue会话管理与版本化幂等API承接，含真实浏览器验证；页面操作已承接，真实模型效果按B/U/R单独验收。

历史导入当前边界：Web历史导入不恢复旧Provider连接配置；旧库事件不能伪装新的control.session/run.admitted等执行授权事实。先做有界只读预检与来源固定，导入原子提交且不自动执行历史任务。
