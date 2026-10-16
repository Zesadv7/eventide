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
| A1 | 进行中 | 2026-10-16：索引已接持久HTTP接收/查询/取消、4根20等待调度及Vue专页，配置冻结/同键回放/取消/重启停驻/发布代次同事务；完整1434项后端通过/9项显式跳过、覆盖92.46%；103项关联及13项专项回归、模块覆盖91.09%、Ruff/Mypy99、88项前端、16条浏览器检查及320px复核、wheel和真实Docker索引/重启验证通过。README/architecture/api-contract/ADR-075同步。现有路由已模块化，真实模型评测与有界交付长任务仍待完成，不标整体完成。 |
| A2 | 进行中 | 2026-10-16：会话改名/归档/空会话删除已接版本校验、持久幂等及原子回执，Vue与旧页同步；46项后端关联回归、100项前端、36项旧页、2条真实API浏览器旅程（丢响应刷新同键、多标签冲突、归档恢复删除及320px）、build/typecheck、Ruff/Mypy105、wheel隔离通过。README/architecture/api-contract/ADR-078同步；其他管理写接口继续补齐。 2026-10-16：Provider保存/重置及模型/会话参数已接持久幂等、版本冲突保护和SQLite原子回执；写入前客户端清理失败可明确重试，结果未知保留原身份。Vue与旧页同步承接，修复连接测试切页后的表单锁定。完整后端1344通过/8个显式跳过、覆盖92.67%；最后恢复边界97项专项通过，Ruff/Mypy92、82项前端/36项旧页、14条整套浏览器检查加生成参数0/null专项通过，typecheck/build/format、smoke12/12、wheel及最新真实Docker分发/重启/12题离线评测通过。README/architecture/api-contract/ADR-070同步；其余管理写接口仍按后续阶段补齐，A2不标整体完成。 2026-10-16：连接探测HTTP持久幂等及旧页稳定重试身份已实现；14项HTTP专项、159项关联后端回归、34项旧页JS（新增5项）、真实Chrome丢响应/刷新/同键回放旅程、Ruff/Mypy83模块通过，HTTP探测模块覆盖100%。README/architecture/api-contract/ADR-067同步；其余写接口仍按后续阶段补齐。 Run/Continue持久幂等接收、重启重试、内容冲突及同源/DNS-rebinding防护已实现；43项HTTP/接收/API回归、Ruff/mypy52模块通过。api-contract/architecture同步；Skills导入/配置已接持久幂等与来源检查、配置竞争返回409；Delivery等继续按后续步骤接入。2026-10-16：容器本机转发精确peer启动配置已接管理守卫，额外peer仍强制回环Host/匹配Origin，禁用代理头信任；85项来源/API/Skills/CLI回归及Ruff/Mypy72模块通过，README/architecture/api-contract/ADR-061同步。真实镜像验证仍由F5记录。 |
| A3 | 已完成 | 生产Host全部实际目录/动态MCP/Worker接入严格契约，首次参数错误允许一次修正轮次，再次错误停机且不重放副作用；截断调用不派发。97项core及81项主链回归、Ruff/mypy55模块通过。README/architecture/api-contract/ADR-052同步；真实Docker命令与Skill脚本路径已由S4/F5补验。 |
| A4 | 已完成 | generation严格配置/能力声明/模型与会话API和三adapter映射；17项生成参数测试及59项相关回归通过，Ruff/mypy50模块通过。README/architecture/api-contract/ADR-050同步；页面在F迁移。 |
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
| S5 | 进行中 | 2026-10-16：3内置包可显式导入；Vue区分安装/启用/选择/实际加载，资源分页、运行固定版本及刷新恢复已通过真实API旅程。完整冻结任务集效果与Token成本对照仍待B/U，未用页面验证替代效果评测。 |
| F1 | 已完成 | 2026-10-16：Vue3/TS/Vite/Router/Pinia及隔离OpenAPI导出/类型生成落地；18项HTTP/组件/SSE/状态回归、typecheck/build及真实API浏览器旅程通过。README/architecture/api-contract/ADR-055同步；静态挂载由F5承接。 |
| F2 | 进行中 | 2026-10-16：Web旧版历史预检/确认导入、持久队列、同键重试、固定脱敏档案、原子数据/成功终态、停止等待真实事务、重启停驻已完成；旧会话只读归档，历史页按身份分页显示消息及输出。106项后端关联回归、53项控制/迁移/检索专项（相关模块89.65%）、108项前端、20条完整浏览器检查、Ruff/Mypy109、help/smoke12/12、wheel及真实Docker分发通过；无网络容器预检/导入/消息/重启、源未改且模型调用0。README/architecture/api-contract/ADR-080同步；全仓门禁留到下一集成收口，不宣称当前已完成R。  2026-10-16：工作副本生命周期已接持久控制Run、4根/20等待队列、完整创建提交和目录版本冻结、源码/Git协调、取消等待真实结果、重启停驻，以及Vue创建/保留/确认清理、历史/队列导航。51项副本关联回归、8项新增专项（控制模块92.65%）、103项前端、19条完整浏览器检查、Ruff/Mypy107、help/smoke12/12、wheel隔离和真实Docker分发通过；无网络容器内创建/保留/清理与重启3条历史通过，模型调用0。README/architecture/api-contract/ADR-079同步；完整后端1474通过/10个显式跳过、覆盖93.55%，会话删除旧测试已按新版本/幂等契约修正并通过16项参数回归。  2026-10-16：会话改名/归档/空会话删除已接版本校验、持久幂等及原子回执，Vue与旧页同步；46项后端关联回归、100项前端、36项旧页、2条真实API浏览器旅程（丢响应刷新同键、多标签冲突、归档恢复删除及320px）、build/typecheck、Ruff/Mypy105、wheel隔离通过。README/architecture/api-contract/ADR-078同步；其他管理写接口继续补齐。  2026-10-16：精确Run/子任务脱敏trace HTTP、历史页查看/JSON下载已接入，共用有界64相关Run投影，关联不混入其他会话历史；31项trace/CLI回归（相关模块97.58%）、3项分发、95项前端、typecheck/build/format、真实API历史下载旅程及wheel隔离验证通过，Ruff/Mypy104通过；README/architecture/api-contract/ADR-046同步。 2026-10-16：交付页面及历史/队列导航已按D6验收；会话写管理、历史导入、trace、worktree维护及live评测继续补齐。 2026-10-16：索引专页承接增量/强制/结构构建、历史分页、刷新原身份重试、停止、精确Run用量导航及队列/历史跳转；88项组件、16条完整浏览器检查、320px复核、wheel与真实Docker通过。交付等剩余页面继续实施。 2026-10-16：独立历史运行页已承接当前项目含归档会话、100条身份游标分页、精确Run输出/状态/工具证据与JSONL下载；URL刷新可恢复已加载页以外的旧Run，跨项目迟到响应不串线，不替换当前执行会话。9项新增回归含105条分页，全部75项前端测试、typecheck/build/format通过；1条真实API浏览器旅程验证两条实际Run、六次工具证据、归档/下载/刷新及后台任务隔离，320px无溢出。README/architecture/api-contract同步；会话写管理、历史导入、独立trace及其他未承接控制面仍待后续，F2不标整体完成。主分支静态分发由集成统一重建。 2026-10-16：项目/会话、流式、审批/停止/Continue、附件、模型参数、导出、Skills及预算/费用已接真实API，Plan固定快照与写Session独立worktree均可在页面并发，固定副本刷新后继续使用。新增全服务队列页面和精确停止/取消，42项前端回归、typecheck/build/格式检查及6条真实API静态页面旅程通过；索引、交付控制面仍待接入。 2026-10-16：Vue候选连接测试已接真实Run/usage及服务UTC日预算说明，候选不覆盖已保存Provider；切页保留在途所有权、刷新重试原身份、表单锁定及未知值均验证。8项新增专项/本分支47项组件、typecheck/build/格式、2条真实API静态浏览器旅程（含320px）、40项探测/分发后端回归通过，README/architecture/api-contract/ADR-067同步。 主分支统一56项前端回归、9条浏览器检查（8条真实API旅程及1条布局fixture）、typecheck/build/format通过。 2026-10-16：离线评测页面发起/历史/排队/停止/报告及同键刷新恢复已通过66项组件与2条真实API旅程；索引、交付和真实模型评测仍待后续。 评测集成后69项前端/11条静态浏览器检查通过；队列与历史评测记录跳转专页，不开放普通Agent继续/输入。 |
| F3 | 进行中 | 2026-10-16：刷新恢复、会话草稿、切页后台继续、多标签审批、项目快照并发与精确停止已验证；预算编辑不被轮询/旧响应覆盖，慢请求不被轮询饿死。未知请求保留原幂等键，明确拒绝可换键；39项前端回归和5条真实API旅程通过；轮询不重置执行选项，新Session不继承自动批准模式，绑定worktree不可切回源。队列页面已补齐并通过6对话实际旅程：4根/2模型/2等待、刷新、取消等待无usage和预留、停止后自动补位；前端42项回归与6条浏览器旅程通过。 2026-10-16：离线评测页面发起/历史/排队/停止/报告及同键刷新恢复已通过66项组件与2条真实API旅程；索引、交付和真实模型评测仍待后续。 |
| F4 | 进行中 | 2026-10-16：Run/Session/项目/UTC每日全历史用量与费用、独立尝试、已知/估算/未知及币种、固定价格依据、共享预算和人工核对已接Vue；新增输入组成、高消耗运行、摘要和多次失败观察，48项前端回归及6条真实API页面旅程通过。交付范围页面随D完成；字符统计与实际Token严格区分。 |
| F5 | 已完成 | 2026-10-16：/workbench/同源产物、源码/文件指纹、wheel资源及Docker多阶段分发完成。已有39项分发/API、23项前端、2条真实API页面旅程及wheel验收；本轮Apple Silicon Docker Desktop真实构建和Compose启动、非root/无需Node/全部资源hash/来源拒绝与同源管理/重建后会话与配置及文件恢复通过，未调用付费模型。CI加入同一容器分发及脚本检查；最新预算/快照页面镜像再次构建，3个资源hash/非root/管理来源/重启持久化通过，90项真实容器及相关回归通过，未调用付费模型。P3队列新镜像再次构建，资源/同源/非root/重启持久化及4根/2模型/20等待配置检查通过；测试容器及卷清理。README/architecture/ADR-058/061同步；旧页保留到X。 最新用量/连接测试页面统一重建，wheel脱离源码导入/资源检查与真实Docker镜像的3资源指纹、非root/无需Node、管理来源和重启恢复全部通过。 B4新版镜像追加12题真实离线API机制验证、同键去重及重启报告一致性，wheel内21个冻结任务资源验证已加入CI。 |
| B1 | 已完成 | 2026-10-16：严格版本化manifest冻结20任务（理解4/定位4/单文件6/跨文件4/恢复交付2）、12开发/8留出和家族隔离；历史engineering三题仅归开发。事实/引用、独立冻结测试、真实快照/worktree及重启/采纳漂移机制验证通过；33项冻结集专项、原有评测33项回归通过，完整1027通过/7个显式Docker跳过覆盖93.42%，最后验收标记收紧后8项复核通过、Ruff/Mypy81和smoke12/12通过。README/architecture/ADR-066同步；20/20仅为scripted机制验证，冻结集live禁用，不是模型成功率；B2/B3/B4效果与Web另验收。 |
| B2 | 已完成 | 2026-10-16：报告schema v2固定Runtime commit/dirty/code指纹、任务输入/版本、Run/Session/Turn、实际生成参数、Skill/工具指纹、独占尝试usage及费用/价格历史；JSON只是各case唯一Store的可重建导出。逐题原子报告、失败/超时/重复取消/证据缺失保留，live故障停止扩大批次；全部suite默认整例300秒/12步/20k observed，收尾后再传播取消。完整1050通过/7个显式Docker跳过覆盖93.43%，最后独立复核修正后51项评测回归（含21项证据专项）、Mypy82/Ruff和smoke12/12通过；README/architecture/ADR-066同步。没有真实模型调用，B3/B4/U5效果与Web仍待验收。 |
| B3 | 待开始 | 开发单次、候选每题3次，相同配置对照，保留失败/预算停止。 |
| B4 | 进行中 | 2026-10-16：唯一20题内置资源/wheel及严格别名已通过126项相关回归和45项资源专项。Web离线控制面已接主Host控制Run、4根/20等待调度、同源幂等接收、查询/SSE/停止及hash校验报告；接收冻结cases，重启停驻不重放，重复取消等待子Host收尾，case账本不导入主服务。实际20题scripted机制通过且源checkpoint不变/主usage为空；72项控制/队列/源码证据/取消相关回归、Ruff/Mypy90及help/smoke12通过，前端代理使用本后端的2条真实API旅程通过。README/architecture/api-contract/ADR-068同步；完整后端1239通过/8个显式跳过、覆盖93.72%，容器集成证据见本行，live仍明确禁用，不标整体完成。 Vue页面历史/停止/报告、未知接收同键和URL恢复已通过66项组件与2条真实API旅程（含320px），主分支69项前端、11条静态浏览器检查（10真实API/1布局fixture）、42项控制/API/分发回归、Ruff/Mypy90、wheel脱离源码21资源gate通过。真实Docker开发12/12、幂等回放/原项目未改/重启报告不变通过，零外部模型请求，临时容器/卷已清理。 |
| B5 | 待开始 | 并发/参数/Skill/注入/恢复/用量专项回归，严重故障须回归。 |
| C1 | 已完成 | 2026-10-16：保留内容哈希增删改、模型/切块指纹、事务外计算及提交前源码/代次复验；补齐源码祖先链接、有界读取、索引目录/数据库/附属文件链接与替换拒绝，损坏指纹不静默复用。35项新增边界/增量专项与全部检索/结构回归共102项通过，index覆盖92.41%，全仓Ruff/Mypy88、help、smoke12/12及离线检索16/16通过。README/architecture/ADR-069同步；仅本地Hash验证机制，无真实embedding调用，Web控制面与语义效果分别按A1/C2–C5验收。 |
| C2 | 已完成 | 2026-10-16：固定本地模型准备/内容指纹、可选sentence-transformers6.1.0、CPU惰性加载及Host重排接线完成；最多20候选/5结果，独立重排分数，缺模型/失败明确降级，默认仍Hash。177项相关回归通过/1项显式真实模型跳过，检索及结构143项覆盖93.28%；另安装隔离依赖并用固定MiniLM/TinyBERT模型在禁止网络连接下完成真实embedding、索引、重排和Host审计工具调用。Ruff/Mypy93、help/smoke12/12、检索16/16、lock及wheel隔离资源通过。README/architecture/api-contract/ADR-071同步；实测macOS本地CPU，默认Docker不含模型/重依赖，C4/C5效果对照与远程embedding账本接线仍待后续。 |
| C3 | 已完成 | 2026-10-16：默认关闭的单次改写/补检索接入统一模型槽与预算，根Run/worker共用持久认领；一次失败不重试，取消/预算拒绝保持控制语义，索引漂移/无效输出保留原候选，按RRF去重最多5条。86项相关回归通过、改写/派发覆盖93.08%，Ruff/Mypy95通过；README/architecture/api-contract/ADR-073同步。没有付费调用，效果与默认晋升仍待C4/C5。 |
| C4 | 进行中 | 2026-10-16：已冻结打包16语料/40查询，开发留出各20，严格hash/标签/临时索引及报告指纹；24项评测回归通过、模块覆盖96.40%，真实禁网MiniLM/TinyBERT四路各题3次共480查询完成，无失败/降级。留出词面Recall/MRR 0.80/0.75，语义0.85/0.85、混合0.85/0.80、重排0.85/0.7292；p95分别15.03/108.35/121.10/145.75ms，均不晋升默认。README/architecture/ADR-074同步；真实查询改写与下游质量费用对照仍待完成。 |
| C5 | 待开始 | 片段占用/输入/cache/改写开销/下游结果；Recall@5或MRR提升另一不降、p95≤基线2倍、任务通过不降才晋升默认。 |
| D1 | 已完成 | 2026-10-16：复用DeliveryController/独立Writer/Verifier实现首次+最多2轮修正，失败输出反馈、候选patch与唯一验收call绑定；最多3次候选验收，不接受模型自评。99项交付/生命周期/预算回归通过、相关模块覆盖90%，另1项真实Docker两轮修复/验收/人工采纳通过，Ruff/Mypy101与smoke12/12通过。README/architecture/api-contract/ADR-076同步；Web仍按D6。 |
| D2 | 已完成 | 2026-10-16：随P2落地独立writer checkout和每交付owner/管理Session；prepare、源码比对及apply短期协调source/Git，writer模型期不占源。并发交付、验收期间源漂移拒绝采纳、排队apply重读最新证据、取消后实际副作用收尾及writer重启恢复均通过；52项副本/交付/Skills回归、全量937通过/7跳过及6项真实Docker通过，architecture/README/ADR-063同步。D1/D3-D6有界流程仍待实施。 |
| D3 | 已完成 | 2026-10-16：交付固定36次模型尝试/60k已报告Token/900秒，统一请求预留前检查，摘要/worker/改写/重试及embedding消费跨根Run累计；新Run/Continue/关闭所有者/重启不重置。36次混合调用、embedding输入限额、绝对deadline和终态关闭专项通过；99项关联回归、限额模块100%覆盖，真实Docker验证通过。README/architecture/ADR-076同步；清理允许超时后继续收尾，不承诺账单硬限。 |
| D4 | 已完成 | 2026-10-16：通过/取消/超限/无源码进展/重复验收失败/保护变化/未知工具结果均停止；重复取消等待Provider与真实工具清理，未知保留账本和产物。99项交付/生命周期/预算回归、并发独立副本/重启/超时/未知writer及verifier专项通过；真实Docker两轮闭环通过。README/architecture/api-contract/ADR-076同步。 |
| D5 | 已完成 | 2026-10-16：开始固定checks/image/保护范围，每轮patch/checkpoint和验收绑定，旧run/verify禁止追加候选或放宽条件；修复成功后仍显式apply，候选修改/源漂移及并发采纳冲突拒绝。99项关联回归，重启保留通过证据、篡改与并发采纳专项及真实Docker修正后人工采纳通过；README/architecture/api-contract/ADR-076同步，未自动commit/merge/push。 |
| D6 | 已完成 | 2026-10-16：持久同源幂等HTTP与Vue创建/有界修复/轮次验收/补丁确认/人工采纳/精确停止已闭环；四交付Writer借用各自控制根名额、共享两模型槽，取消等待真实清理，刷新未知同键重试。修复源项目配置串线、Mac路径别名及快照文件名误脱敏。55项交付/快照回归、9项控制专项（相关模块88.92%）、完整1460通过/10显式跳过覆盖93.54%、Ruff/Mypy103、smoke12通过；92项前端、17条浏览器检查（16真实API/1布局）、typecheck/build/format与320px复核通过；wheel隔离资源、新Docker准备/重启/幂等及真实Docker修复采纳通过。README/architecture/api-contract/ADR-077同步；未调用付费模型，模型效果仍归B/U/R。 |
| X1 | 进行中 | 2026-10-16：Web旧版历史预检/确认导入、持久队列、同键重试、固定脱敏档案、原子数据/成功终态、停止等待真实事务、重启停驻已完成；旧会话只读归档，历史页按身份分页显示消息及输出。106项后端关联回归、53项控制/迁移/检索专项（相关模块89.65%）、108项前端、20条完整浏览器检查、Ruff/Mypy109、help/smoke12/12、wheel及真实Docker分发通过；无网络容器预检/导入/消息/重启、源未改且模型调用0。README/architecture/api-contract/ADR-080同步；全仓门禁留到下一集成收口，不宣称当前已完成R。  2026-10-16：工作副本生命周期已接持久控制Run、4根/20等待队列、完整创建提交和目录版本冻结、源码/Git协调、取消等待真实结果、重启停驻，以及Vue创建/保留/确认清理、历史/队列导航。51项副本关联回归、8项新增专项（控制模块92.65%）、103项前端、19条完整浏览器检查、Ruff/Mypy107、help/smoke12/12、wheel隔离和真实Docker分发通过；无网络容器内创建/保留/清理与重启3条历史通过，模型调用0。README/architecture/api-contract/ADR-079同步；完整后端1474通过/10个显式跳过、覆盖93.55%，会话删除旧测试已按新版本/幂等契约修正并通过16项参数回归。  2026-10-16：已核对cli.py全部命令与当前Vue/API/Python能力，迁移清单见下方；历史导入、trace、项目/会话写管理、受管worktree维护和真实模型评测仍有缺口，旧入口保留，不标完成。 |
| X2 | 待开始 | 删除用户命令行产品和旧前端/专属死代码，不留转发空壳。 |
| X3 | 待开始 | 打包/Docker/示例/CI/文档同步；help/eval门禁改服务冒烟/Python接口，不降低范围。 |
| U1 | 已完成 | 2026-10-16：主Agent/摘要/worker/连接探测/查询改写/远程检索及索引批量embedding均接唯一attempt账本，SDK隐式重试禁用，缺usage为unknown，父子/终态不双计，输入型调用无输出。查询改写86项回归，索引103项关联与13项专项、模块覆盖91.09%；此前完整1421项通过、覆盖91.90%，88项前端和16条浏览器检查通过，wheel与真实Docker索引/重启验证通过。README/architecture/api-contract/ADR-049/072/073/075同步；新全量收口仍按R执行，无付费模型调用。 |
| U2 | 已完成 | 2026-10-16：原子预留/结算、共享根/项目/交付/UTC日预算、固定价格与未知人工核对已覆盖主Agent/摘要/worker/探测/查询改写/embedding查询及索引批次；模型槽等待不预留，失败消费不丢失，未知不释放，重启不重置。此前预算HTTP41项及完整909项/P阶段1180项通过；新增索引103项关联与13项专项通过，远程批量实际派发替身核实项目归属，无额外generation。88项前端/16条浏览器、真实Docker重启通过。README/architecture/api-contract/ADR-059/062/064/075同步；有界交付总限及页面仍分别按D/U3验收，不承诺供应商账单硬限。 |
| U3 | 进行中 | 2026-10-16：Vue已接Run/Session/项目/UTC每日全历史账本、价格快照、币种与来源、共享预算、未知核对、请求耗时/结束原因及七项输入字符组成；高消耗独立Run排名、摘要调用频次和多次失败观察已实现，未知不记零，刷新不累计。178项组成后端回归及本次168项usage/预算/probe组合回归、48项前端回归、6条真实API静态页面旅程、typecheck/build/format、Ruff/Mypy87通过；最新Docker镜像资源/非root/同源/重启/队列额度验证通过。README/architecture/api-contract/ADR-064同步；交付范围的页面闭环随D补齐，完整阶段尚未标完成。 320px失败列表布局回归通过，长Run身份自动换行、不溢出页面。 |
| U4 | 待开始 | 随S/C/D：渐进加载/分页/去重/片段预算、摘要阈值间隔、减少重复委派、无进展停止、稳定缓存前缀，参数实验。 |
| U5 | 待开始 | 随B/R：冻结集单项及组合消融，成功计数/总Token及费用/延迟p50/p95/分类额外开销/缓存覆盖/失败与未知；总费用÷成功数，无成功不可算。 |
| R1 | 待开始 | 后端完整门禁≥85%覆盖，默认离线无密钥/网络。 |
| R2 | 待开始 | 前端typecheck/组件/build/Playwright。 |
| R3 | 待开始 | 并发/审批/队列/取消/重启/Skill版本/采纳冲突。 |
| R4 | 待开始 | usage采集去重/父子/预留结算/价格/全历史/未知。 |
| R5 | 待开始 | 冻结集对照与至少2个用户实际项目任务。 |
| R6 | 待开始 | 安装包/容器/API/Markdown一致，本地提交。Docker不可用单列待外部验证，不能声称已实测。 |

## 用量、预算与实验细则

- 2026-10-16 U1/U2 远程embedding异步派发底座已实现：共享模型槽、显式单次SDK调用、输入用量归一化、预算拒绝及重复取消等待请求/客户端清理；54项派发/账本专项、Ruff/Mypy91通过。仍待检索Host接线，不标整体完成。F2历史页集成补验：78项前端回归、构建/类型/格式、1条真实API历史旅程及3项分发检查通过，静态产物同步。
- 2026-10-16 U1/U2 补充底座已验收：embedding预留固定input_only，Provider输入/缺失输出、预算结算及价格口径一致，未知/估算不释放；198项相关离线回归、Ruff/Mypy90通过。README/architecture/api-contract同步；远程检索与索引派发已由ADR-072/075接入，阶段状态见U1/U2。
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


## X1 旧入口承接核对（2026-10-16）

| 旧能力 | 当前承接 | 剩余验收 |
|---|---|---|
| chat/run/continue/abandon | Vue对话、Run/Continue/恢复API | 现有用户操作已覆盖，X3调整门禁 |
| plan show | 会话task_plan与Vue任务证据 | 保留Python状态查询，无需另一终端入口 |
| workspace list/add/show/remove | API全部存在；Vue添加/切换 | 页面移除与幂等管理写入 |
| worktree list/create/keep/remove | 同一Host管理、持久控制API与Vue工作副本页 | 已承接，完整后端1474通过/93.55%、19条浏览器及真实容器验证 |
| deliver prepare/run/verify/show/apply | 有界修复API/页面；Python独立验收接口 | D6集成检查；旧手动操作保留为开发接口 |
| export | 历史Run事件JSONL下载 | 已承接，保留Python导出接口 |
| trace / OTLP | telemetry.py投影、精确Run/子任务trace API与历史页JSON下载、显式Python导出 | 已承接；OTLP仅显式开发接口 |
| migrate-v02 | 同一migration模块、Web受控预检/确认导入及只读历史消息 | 已承接，106项关联/20条浏览器/真实容器验证 |
| rag build/search/eval | 索引页面、Agent search_knowledge、Python检索评测 | 默认远程路径继续统一Host预算，删除终端专属适配 |
| eval | Web离线机制评测、Python评测 | Web真实模型及冻结集效果验证 |
| serve | FastAPI同源服务与Docker | X3改独立部署入口、资源/容器/CI门禁 |

会话重命名/归档/空会话删除已由Vue会话管理与版本化幂等API承接，含真实浏览器验证；其他F2缺口按表继续。

X1导入实现前的边界：Web历史导入不恢复旧Provider连接配置；旧库事件不能伪装新的control.session/run.admitted等执行授权事实。需先做有界只读预检与来源固定，导入原子提交且不自动执行历史任务。
