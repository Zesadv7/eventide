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
| A2 | 进行中 | Run/Continue持久幂等接收、重启重试、内容冲突及同源/DNS-rebinding防护已实现；43项HTTP/接收/API回归、Ruff/mypy52模块通过。api-contract/architecture同步；Skills导入/配置已接持久幂等与来源检查、配置竞争返回409；Delivery等继续按后续步骤接入。 |
| A3 | 已完成 | 生产Host全部实际目录/动态MCP/Worker接入严格契约，首次参数错误允许一次修正轮次，再次错误停机且不重放副作用；截断调用不派发。97项core及81项主链回归、Ruff/mypy55模块通过。README/architecture/api-contract/ADR-052同步；真实Docker单列未验证。 |
| A4 | 已完成 | generation严格配置/能力声明/模型与会话API和三adapter映射；17项生成参数测试及59项相关回归通过，Ruff/mypy50模块通过。README/architecture/api-contract/ADR-050同步；页面在F迁移。 |
| A5 | 已完成 | admission冻结、零/null/最大输出/继承、Continue及并发工作区隔离验证通过；最大输出日志字段保留数值；辅助调用不继承采样覆盖。见test_generation及ADR-050。 |
| A6 | 已完成 | 2026-10-16：Host流式主链/有界脱敏预览SSE及3类官方SDK适配完成，完整终态前不派发工具；104项集成回归通过，真实DeepSeek普通/工具调用10次基线完成。预览不落库/不推进游标；页面在F。README/architecture/api-contract/ADR-053同步。 |
| A7 | 已完成 | 2026-10-16：跨分片密钥lookahead脱敏、超长密钥禁预览，取消/断流/超时不重发或执行半成品；未知能力接收前明确拒绝采样参数。104项相关集成覆盖，详见test_stream_runtime/test_provider_streaming/test_generation；最终页面验收在F/R。 |
| P1 | 待开始 | 项目聚合/执行副本关联，保留 Session 稳定身份，不运行中重新绑定。 |
| P2 | 待开始 | 同 Session 串行，只读固定快照并发、写入各 worktree、直接源/采纳独占；快照检测漂移。 |
| P3 | 待开始 | 默认活跃根 Run 4/模型请求 2/等待 20；持久队列可查可取消，重启不自动重放写任务。 |
| P4 | 待开始 | 精确审批/取消/用量身份；共享 Git 元数据协调；父子等待无额度死锁。 |
| P5 | 待开始 | 接入 U2 原子预留结算，多窗口不超额派发。 |
| S1 | 已完成 | 2026-10-16：严格元数据、资源布局和依赖诊断已接入Host/API及Vue；损坏固定版本可诊断并禁用，执行仍拒绝。70项Skills定向回归、完整782通过/1跳过且覆盖93.36%、Ruff/Mypy70模块、smoke12/12、23项前端回归及2条真实API浏览器旅程通过；README/architecture/api-contract/ADR-057同步。 |
| S2 | 已完成 | 2026-10-16：本地目录/ZIP/内置包导入与项目/用户作用域已接API和页面，安装不自动授权用户包；同名精确身份、恶意路径/链接/规模限制、持久幂等及重启未知不重放有测试。70项Skills回归及真实API导入旅程通过；稳定文档同步。 |
| S3 | 已完成 | 2026-10-16：admission/Continue/重启固定内容；页面支持用户版本固定、差异及回退，项目更新由Git管理。预览摘要复核防源漂移，配置摘要拒绝旧标签覆盖；资源篡改、恢复与加载记录持久化通过70项Skills回归和23项前端回归。README/architecture/api-contract同步。 |
| S4 | 待外部验证 | 2026-10-16：ZIP/资源边界及本地脚本权限执行已通过回归，Plan不提供脚本、安装不执行、未知结果保留资源；HTTP上传接入。当前主机未安装Docker，真实容器脚本执行仍待外部验证；本地路径完成，不宣称容器实测。 |
| S5 | 进行中 | 2026-10-16：3内置包可显式导入；Vue区分安装/启用/选择/实际加载，资源分页、运行固定版本及刷新恢复已通过真实API旅程。完整冻结任务集效果与Token成本对照仍待B/U，未用页面验证替代效果评测。 |
| F1 | 已完成 | 2026-10-16：Vue3/TS/Vite/Router/Pinia及隔离OpenAPI导出/类型生成落地；18项HTTP/组件/SSE/状态回归、typecheck/build及真实API浏览器旅程通过。README/architecture/api-contract/ADR-055同步；静态挂载由F5承接。 |
| F2 | 进行中 | 2026-10-16：项目/会话、流式、审批/停止/Continue、附件、模型参数、export/usage及Skills管理已接真实API；Skills导入启用/资源/加载记录旅程通过。索引、评测、交付控制面仍待接入。 |
| F3 | 进行中 | 已验证刷新恢复、会话草稿、切页后台继续及多标签审批同步；网络结果不明保留原幂等键。完整新控制面和并发队列旅程待后续API集成。 |
| F4 | 进行中 | Run/Session账本、请求明细/调用分类/缓存推理细项/未知完整性已展示；价格/预算/项目日统计/输入组成待U2/U3后续接入。 |
| F5 | 待外部验证 | 2026-10-16：/workbench/同源产物、源码/文件指纹、Python包资源与Docker多阶段构建已落地；39项API/分发回归、23项前端回归、build/typecheck、2条直接FastAPI静态页面旅程及wheel隔离导入/全部资源冒烟通过，Ruff/Mypy70模块通过。README/architecture/api-contract/ADR-058同步。主机无Docker，镜像实测待外部验证；旧页保留到X。 |
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
| D2 | 待开始 | Writer 独立 worktree，prepare/apply 协调源，不整轮阻塞其他隔离对话。 |
| D3 | 待开始 | 交付默认36步/60,000已观测Token/15分钟，摘要/子任务累计；恢复不清零。 |
| D4 | 待开始 | 通过/取消/超限/无进展/重复失败/保护变化/未知结果停止，等待副作用收尾。 |
| D5 | 待开始 | 固定验收/保护范围，patch/checkpoint绑定，新改动使旧验收失效；显式采纳，不自动提交/合并/推送。 |
| D6 | 待开始 | Vue目标/后端/轮次/失败/补丁/用量/停止/采纳，失败保留产物。 |
| X1 | 待开始 | CLI能力清单逐项承接，包括历史导入、worktree、export/trace/检索/评测/交付。 |
| X2 | 待开始 | 删除用户命令行产品和旧前端/专属死代码，不留转发空壳。 |
| X3 | 待开始 | 打包/Docker/示例/CI/文档同步；help/eval门禁改服务冒烟/Python接口，不降低范围。 |
| U1 | 进行中 | Host尝试身份/SDK禁隐式重试、provider归一化、去重账本、Run/Session全历史及子Run查询已接线；70项相关集成通过，未知/取消/父子/重启回归通过。流式终态已随A6接入，embedding接线待C；README/architecture/api-contract/ADR-049同步。 |
| U2 | 待开始 | 随P：Run/交付/项目/当日预算，原子预留估计输入+输出、实际结算，未知不释放为0；重启保留；80%提示/阈值阻止新请求。 |
| U3 | 进行中 | Vue已消费去重请求账本，未知不记零、父子按服务端口径汇总；18项前端回归及真实API用量页面旅程通过。成本/共享预算/项目与每日/内容组成尚未完成。 |
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
