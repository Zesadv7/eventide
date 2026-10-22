# Eventide 失败与修复案例

本文件保存可追溯的工程事实和复盘，由 [AGENTS.md](../AGENTS.md) 中的硬性规范约束维护。用于帮助理解和复述工程决策；不替代测试报告、运行事件或供应商账单。

2026-10-21 首次建立：回填本轮两类失败及三个历史案例。仅查看已有代码、Git 和保留产物，没有重新运行历史测试、模型请求或容器构建。

## 记录口径

- 先记录事实，再分析原因。按根因组织案例，关联多个失败样本；复验追加到原案例，不覆盖首次失败。状态沿用待开始、进行中、受阻、待外部验证、已完成；“复盘完成”与“原失败已修复”必须分别说明。
- 验证范围写明自动化测试、真实模型或容器/部署；失败类型区分预期 Red、受控缺陷复现、意外回归、测试问题、环境/外部故障、真实任务失败。不能证明曾经通过时，不称为回归；模型任务未完成时，不仅凭现象猜测模型或网络根因。
- 原始日志、模型输入输出、数据库和现场产物留在忽略目录。下面的 `.task_outputs/` 路径是本机保留证据，不随仓库分发；公共可核查依据优先使用源码、commit 和稳定文档。历史日志缺失或命令无法还原时明确注明，不补造记录。
- 历史结果注明发生日期与本次核对日期；本文件中的命令仅供复现说明，不代表本轮再次执行。收集成功、执行通过、跳过、真实模型任务完成分别报告；同一批验证被多例引用时不重复累计。
- 调查过程只写实际发生过的步骤，事后理解标为复盘分析。个人贡献和工具协助按事实记录；没有用户亲自操作的证据，不替用户写第一人称经历。典型案例末尾的复述摘要和追问是准备素材，不是效果证明。

## 后续案例模板

每例尽量控制在一页，未知项直接写待查，不另建记录系统。

- **编号、日期、范围/类型、状态：** 实际失败时间、登记时间和当前处理状态。
- **场景与影响：** 预期行为、实际现象，已确认影响与潜在风险分别说明。
- **证据与定位：** 基线/配置、测试或任务身份、命令、错误摘要和证据位置；已验证原因、未验证假设及实际排查步骤。
- **处理与取舍：** 最小改动或缓解措施，采用理由、代价和相关 commit；原条件变化单独注明。
- **复验与边界：** 命令/条件、结果、未验证内容；失败保留，不能靠放宽原验收改判。
- **经验与参与：** 可复用经验、实际参与者与工具协助。
- **复述摘要与追问：** 三四句事实摘要，以及两三个能结合代码回答的问题。

## 案例索引

| 编号 | 问题 | 范围 / 失败类型 | 状态 |
|---|---|---|---|
| FAIL-001 | 长日志截断验收反馈 JSON | 自动化测试 / 受控缺陷复现 | 已完成：反馈修复通过定向验证 |
| FAIL-002 | 新修正提示对旧实现的预期失败 | 自动化测试 / 预期 Red | 已完成：新契约通过定向验证 |
| FAIL-003 | Python 3.10 无法收集测试 | CI / 测试兼容性问题 | 已完成：修复及历史证据核对 |
| FAIL-004 | 原验收通过仍遗漏 HTTP 分支 | 真实模型任务 / 验收覆盖不足 | 已完成：复盘；旧条件失败仍保留 |
| FAIL-005 | Docker 基镜像获取失败 | 容器构建 / 环境或外部故障 | 待外部验证：构建已缓解，公网故障根因待查 |
| FAIL-006 | 新增搜索工具后旧目录断言未同步 | 自动化测试 / 测试契约更新遗漏 | 已完成：旧目录断言修正，新SHA六项CI通过 |
| FAIL-007 | 回查集成测试误用 Run 投影字段 | 自动化测试 / 新测试问题 | 已完成：修正投影身份来源，定向通过 |
| FAIL-008 | master推送的Git HTTPS连接超时 | 发布／Git传输 / 环境或外部故障 | 已完成：代理推送成功；底层根因未明 |
| FAIL-009 | 超过采集上限的验收中段尚不可回读 | 自动化测试 / 新能力预期 Red | 已完成：Local/Docker中段及边界回读通过 |
| FAIL-010 | 新归档恢复/清理事实被终态规则拒绝 | 自动化测试 / 新增实现缺陷 | 已完成：会话级维护事实保留原Run终态 |
| FAIL-011 | 归档打开失败原因被后续快照覆盖 | 自动化测试 / 新增实现缺陷 | 已完成：仅对实际打开的文件取快照 |
| FAIL-012 | 新归档首轮静态检查未通过 | 静态检查 / 新代码规范问题 | 已完成：Ruff及Mypy通过 |
| FAIL-013 | 分流参数增加后工具目录超出原预算 | 自动化测试 / 新增接口固定开销 | 已完成：缩短说明，原预算不变 |
| FAIL-014 | MCP 边界新增测试误用策略构造接口 | 自动化测试 / 新测试问题 | 已完成：修正fixture/拒绝原因断言及格式 |
| FAIL-015 | MCP丢失响应与文本前缀被错误归类 | 自动化测试 / 受控缺陷复现 | 已完成：停驻与显式结果定向通过 |

## FAIL-001：长日志截断验收反馈 JSON

- **日期与范围：** 2026-10-21；本地 Python 3.12、ScriptedProvider 与真实本地验收命令；受控缺陷复现。修复 commit：`5f999c7`，生产实现基线：`f828ad2`。
- **场景与影响：** 多条固定命令独立验收后，需要把失败信息交给下一轮 Writer。旧实现对整份 JSON 直接 `[:12_000]`，前面一条成功日志就可能占满额度，使后续失败项不可见。已确认受控样本的 JSON 不完整；没有实际用户事故或模型修复失败的因果证据。
- **失败证据：** 将 `5f999c7` 的新增测试用于旧生产实现后，`test_correction_feedback_keeps_late_failures_and_escaped_output` 的普通字符和转义/中文两个参数样本均报 `JSONDecodeError: Unterminated string`。当时命令为 `.venv/bin/python -m pytest -q tests/test_delivery_repair.py -k 'second_candidate or correction_feedback' --tb=short`；同批第三项失败归入 FAIL-002。原终端输出见本轮维护对话，未单独落盘；持久摘要见 [AGENTS.md 的 FB-1](../AGENTS.md)，测试与差异见 `git show 5f999c7 -- eventide/delivery_repair.py tests/test_delivery_repair.py`。
- **已证实原因与处理：** 字符串截断不理解 JSON 结构，也没有给后续检查预留空间。先保留全部检查状态、元数据及轮次/补丁/调用来源，再将剩余额度分给失败日志，按转义后的实际长度选取头尾节选，最后序列化。成功项省略日志，省略量明确显示；原验收记录及失败签名不变。
- **复验与边界：** 修复后只运行 `.venv/bin/python -m pytest -q tests/test_delivery_repair.py --tb=short`，21 项通过、1 项真实 Docker 按原开关跳过；Ruff、单模块 Mypy 和差异检查通过。长日志、合法 JSON、12,000 字符上限、所有检查可见及原轮次/预算/取消/采纳有覆盖。测试使用固定响应，不能据此宣称真实模型修复率提高。
- **经验与参与：** 上下文裁剪应同时保护结构、检查覆盖和来源，字符上限不等于 Token 上限。用户确定最小修改范围与验证约束；Codex 辅助定位、实现和验证，未记录用户亲自复验。
- **复述摘要：** 交付反馈的边界测试复现了长输出截坏 JSON 的问题。修复沿用原有执行循环，只改反馈投影，先保留各项状态，再分配日志额度。相关定向验证通过，保留了原控制规则；模型效果仍待另行测量。
- **追问：** 为什么必须按转义后的长度计算？为什么省略成功日志但保留其状态？如何确认摘要没有改变验收判分和失败签名？

## FAIL-002：新增修正提示对旧实现的预期失败

- **日期、类型与状态：** 2026-10-21；自动化测试、预期 Red；已完成。与 FAIL-001 同一开发批次，不是又一个线上故障。
- **失败证据：** 增强 `test_second_candidate_uses_failed_checks_and_cumulative_usage`，要求失败后的请求包含检查当前源码、待验证假设及修正点；旧实现在 `assert "inspect the current source" in correction` 处失败。基线、首次命令、终端证据位置及修复 commit 同 FAIL-001；这是新需求未实现时的预期结果，不足以证明旧行为违反原有契约。
- **处理与取舍：** 只在原失败后提示中增加针对性要求，继续明确测试输出是不可信数据、说明不能作为通过证据。原验收循环没有增加专门的反思模型请求，也不强制每个工具步骤输出长篇反思。
- **复验与边界：** FAIL-001 所述 21 项通过、1 项跳过已经包含本例；二轮修正仍是四次固定模型请求，累计用量和独立验收断言保留。只能证明提示被正确传入及原机制可运行，不能证明真实模型必然遵守或能力提升。
- **经验与参与：** 新要求先定义可观测验收，再确认旧版失败，能避免把“测试变绿”误说成发现并修复线上事故。用户批准提示范围；Codex 辅助编写断言、实现及验证。
- **复述摘要：** 为失败后的针对性修正先增加请求断言，旧提示按预期未通过。实现复用原请求，只增加证据、假设和修正点的要求。定向验证通过，实际模型收益没有提前下结论。
- **追问：** 预期 Red 和回归有什么区别？怎样证明请求数量没有因新增独立反思阶段而增加？为什么模型的修正说明不能判定验收通过？

## FAIL-003：Python 3.10 无法收集测试

- **日期、类型与状态：** 2026-10-16 发生，2026-10-21 回填核对；GitHub Linux Python 3.10、测试兼容性问题；已完成。修复 commit：`130a84f`，原实现见其父提交。
- **场景与证据：** 项目声明支持 Python 3.10，首轮 CI 执行 `uv run pytest -q` 时，`tests/test_retrieval_answers.py` 导入 `datetime.UTC` 报 `ImportError`，以一次收集错误、退出码 2 停止。原证据：`.task_outputs/github-py310-first-publish.log`，从第 251 行起包含命令和 traceback。
- **已证实原因与处理：** 测试依赖了最低支持版本没有的接口；`130a84f` 将其改为 `timezone.utc`。同轮检查还把测试轮询的 `asyncio.timeout(10)` 改成事件循环时钟上的十秒截止并保留超时报错。首轮被收集阻断，不能说日志证明后者也曾实际执行失败。生产实现和判分未改。
- **复验与边界：** `.task_outputs/github-py310-collection.log` 记录 1,830 项成功收集，不是 1,830 项运行通过。[AGENTS.md 的 G2](../AGENTS.md) 历史摘要另记隔离 Python 3.10.22 三文件 74 项通过；本次未找到该 74 项的原始通过日志与完整本地命令，因此只按摘要引用，不补造。代码差异可用 `git show 130a84f -- tests/test_retrieval_answers.py tests/test_evaluation_control.py` 复核。
- **经验与参与：** 最低版本约束同时适用于测试工具代码；收集阶段失败与业务断言失败分开定位。历史操作人员的具体分工未完整追溯，不归称用户亲自完成。
- **复述摘要：** 最低版本 CI 在测试收集时失败，定位到测试使用了更新 Python 才提供的接口。修复仅调整测试兼容写法并保留原十秒截止。历史收集及定向验证有不同层级的证据，不能混成全量通过。
- **追问：** 为什么高版本本机通过不能证明最低版本支持？收集成功与执行通过有什么差别？怎样保持替换后的超时语义？

## FAIL-004：原验收通过仍遗漏 HTTP 分支

- **日期、类型与状态：** 2026-10-16 的真实模型修复实验，2026-10-21 回填核对；真实任务失败及验收覆盖不足；已完成复盘。原 60k 条件失败仍保留，不标为已在同条件修复。
- **场景与证据：** 回放 Eventide 历史 `b238620` 的 HTTP Run/Continue 接收问题。用户持久请求身份创建后，即使附件缓存或派发失败，也应可追溯原身份，避免重试重复创建。200k/Writer30 条件下，HTTP 候选用 15 次请求通过原两项检查，但后续源码复核发现仍遗漏 Continue 缓存异常和派发异常等分支。之后冻结 v2 的 11 项检查：历史基线 11 失败、该候选 5 通过/6 失败、当时主程序 11 通过。
- **已证实原因与未解问题：** 原两项验收没有充分覆盖原目标的异常分支；测试通过不能证明完整实现。模型为何遗漏这些分支没有独立证据，不能仅归因于模型能力、预算或提示。
- **处理与取舍：** 保留原验收结果，另行冻结增强检查并在新实验开始前声明 400k/Writer30、总 36 次尝试和 900 秒上限；新 HTTP 回放 21 次请求后首轮独立 11 项通过，源码复核确认遗漏已处理。提高预算和增强验收属于不同条件，不能当同预算改善或单因素收益。候选没有自动采纳、提交或推送。
- **证据与复核方法：** `.task_outputs/quality-real-project-200k/actual-repairs.json` 中 HTTP 记录保存原命令、15 次请求和结果；Runtime commit 为 `71bb9299`，实际指纹/dirty 状态以记录为准。`.task_outputs/quality-http-v2-400k/` 的 `preregistration.json`、`baseline.log`、`candidate.log`、`current.log` 与 `actual-repairs.json` 保存新条件及结果；新 Runtime commit 为 `cacc43aa`。固定 v2 测试 SHA-256 为 `e4e60881e680dd7b1a2bc2fcc721e8b29797917b3e46a297e298c35572caf061`；对应命令为项目虚拟环境 Python 的 `-m pytest -q` 加该目录 `frozen/test_http_acceptance.py`，当时绝对路径见 JSON 的 `checks`。另见[架构中的真实模型实验记录](architecture.md#真实模型实验记录)及其追加工程验收记录。
- **经验与参与：** 独立验收也需要审查覆盖范围，失败样本和旧条件必须保留。历史记录可确认用户允许提高本轮测试预算、Codex 辅助运行与复核；不能扩写为用户亲自逐步排查。新高额度结果仅证明这些历史问题的有限实验，不代表一般项目成功率。
- **复述摘要：** 真实修复候选通过原测试后，源码复核仍发现目标分支遗漏。处理时保留旧结果，冻结更完整的验收，在声明的新预算下重新回放并核对源码。关键经验是把验收覆盖、模型效果和预算条件分开报告。
- **追问：** 独立验收为什么仍会漏问题？为什么加强检查后不能追溯改判旧结果？改变预算后应该怎样报告比较？

## FAIL-005：Docker 基镜像获取失败

- **日期、类型与状态：** 2026-10-16 的本机构建验证，2026-10-21 回填核对；容器构建、环境或外部故障；待外部验证。固定已验证 digest 的构建与冒烟路径已通过，原公网获取故障的底层原因未查明。
- **场景与证据：** Docker Desktop 的 `desktop-linux` builder 获取 `node:22-bookworm-slim` 元数据时，请求 `auth.docker.io` 的匿名 token 出现 TCP `i/o timeout`，构建失败；再次尝试也失败。原日志：`.task_outputs/quality-followup-docker-build.log` 第 14 行及 `quality-followup-docker-build-retry.log` 第 11 行。日志不含完整 build CLI，不能将重构命令伪称为当时实际命令。
- **原因边界与处理：** 已确认失败发生在外部镜像认证端点的连接阶段；没有证据进一步归因账号权限、代理、DNS 或 Dockerfile 逻辑。使用忽略目录内的 `quality-followup-pinned.Dockerfile`，固定上轮已验证的 Node/uv 基镜像 digest，完成该次本地构建；仓库 Dockerfile 未改。该缓解不能保证新机器离线或任意网络下可构建。
- **复验与证据：** `.task_outputs/quality-followup-pinned-build.log` 第 102 行起记录成功导出 `eventide:quality-followup-pinned`；`quality-followup-pinned-smoke.log` 保存三项静态资源校验、非 root/无需 Node、来源管理、重启持久化、索引/交付准备恢复及 12/12 离线机制结果，外部模型请求为 0。另见 [AGENTS.md 的 Q3](../AGENTS.md)。这是历史构建证据，本次没有调用 Docker；原浮动标签公网路径未重新确认恢复。
- **经验与参与：** 先区分外部依赖获取和项目构建阶段，保留首次失败，并记录验证用镜像身份及本地依赖。历史角色分工未完整追溯，不把环境缓解写成用户亲自修复网络基础设施。
- **复述摘要：** 一次容器验证在获取基镜像时因外部连接失败中断。固定已验证镜像身份后完成了该次本地构建和分发冒烟，同时保留原失败及网络根因未知的边界。该结果只证明限定环境下的交付路径可验证。
- **追问：** 固定 digest 能解决什么、不能解决什么？如何区分外部连接错误与 Dockerfile 错误？为什么离线脚本通过不能说明真实模型任务通过？

## FAIL-006：新增搜索工具后旧目录断言未同步

- **日期、范围、类型与状态：** 2026-10-22；自动化测试／测试契约更新遗漏；已完成。基线 `acf9056` 加本轮 TS-1 未提交实现。
- **现象与证据：** `uv run pytest -q tests/test_text_search.py tests/test_tools.py tests/test_tool_contracts.py tests/test_run_policy.py tests/test_readonly_workers.py tests/test_sandbox_execution.py tests/test_runtime.py tests/test_host.py -k 'not timeout'` 得到 106 通过、1 失败、1 未选中；日志 `.task_outputs/quality-increment/ts-tests.log`。失败仅为 `test_host_project_binding_and_lease` 的精确工具集合未包含已授权新增的 `search_text`。
- **已证实原因与处理：** 实现和其他工具目录测试已更新，这处旧断言遗漏。补充新工具名称，保留原目录与所有权断言，不修改生产行为。新目录实测 3,920 字符，相比原 3,283 增加 637；目录上限和压缩测试固定开销已显式同步，不以隐藏目录开销使测试通过。
- **复验与边界：** 仅重跑原失败 `uv run pytest -q tests/test_host.py::test_host_project_binding_and_lease`，1项通过，日志 `ts-recheck.log`；独立枚举修正后的 `test_text_search.py` 11项通过，日志 `ts-bounded-enumeration.log`。没有付费模型、容器或模型质量结论。修复提交 `88a7ab8`。
- **经验、参与与追问：** Codex 实施并定位，用户指定范围；新增基础工具同时影响 Host、Plan、worker 及请求固定开销。如何区分旧契约更新遗漏和权限回归？为什么不能只扩大压缩预算却不记录工具目录增长？

- **2026-10-22 合并前追加：** 基线83abd88，静态复核发现 `tests/test_worker_lifecycle.py` 的 ControlledProvider 也遗漏新工具。实际执行 `uv run pytest -q tests/test_worker_lifecycle.py -x`，首项等待 `both` 三秒超时、1失败即停（`.task_outputs/quality-increment/merge-worker-red.log`）。代码中目录断言位于设置同步事件之前，不能把该现象当生产调度变慢。此次只补该集合，保留3秒时限和停止/父子隔离等断言；`uv run pytest -q tests/test_worker_lifecycle.py` 随后6项通过（4.82秒，`merge-worker-green.log`），Ruff及diff检查通过。生命周期修正提交 `da2eff9`，由本条和该测试文件的Git历史追溯。先前首批定向范围不覆盖该文件，不能冒充当时已经验证，也不能将本次定向通过当当前提交完整CI已通过。

- **2026-10-22 当前提交真实CI追加：** 用户授权发布后，候选1166ca6的CI `39642683561`、Python3.10 job `119370022716` 在 `uv run pytest -q` 得到1956通过、1失败、29跳过。唯一失败 `test_runtime_exposes_and_audits_workspace_skill_loading` 的精确工具集合也遗漏search_text；日志 `.task_outputs/quality-increment/github-py310-first.log`（约第288–366行）证明新增工具是差异。先前worker专项不覆盖此文件，不能称之前已验证；最终两个Python版本均为同一项失败、各1956通过/29跳过；3.14覆盖率93.04%达到85%，该轮整体仍为失败（`github-first-failed.log`）。其他四个job通过。此次只补该断言，本地 `uv run pytest -q tests/test_skill_loading.py` 4项通过（`merge-skills-green.log`），Ruff/diff通过；全测试AST盘点确认其余五类基础目录契约已同步，sandbox限制不改。修正提交 `918b40a` 的[CI39644102821](https://github.com/Zesadv7/eventide/actions/runs/39644102821)六job全部通过，两个Python版本各1957通过/29跳过，覆盖率93.03%；本地4项验证不是整版结果的替代。首轮失败不删除、不改判。GitHub对修正后的新SHA仍需真实门禁：现有push工作流会触发整套，不能用旧SHA通过的job替代新SHA结果；不另跑本地全量。

## FAIL-007：回查集成测试误用 Run 投影字段

- **日期、范围与状态：** 2026-10-22，自动化测试／新测试问题，已完成；基线88a7ab8加EV-1未提交实现。
- **命令与证据：** `uv run pytest -q tests/test_tool_result_paging.py tests/test_delivery_repair.py tests/test_host.py::test_tool_catalog_stays_within_its_budget`：27通过、1失败、1真实Docker按原开关跳过；`.task_outputs/quality-increment/ev-tests.log`。新增中段回查用例已完成两轮修复和成功回查，随后用 `child["run_id"]` 读取Run详情导致KeyError；该投影使用其他身份字段。
- **原因与处理：** 测试错误假定Run投影字段，改为使用此前已获得的权威worker_run_id，不改业务断言或生产行为。独立复核另发现临时grant随执行配置继承的权限风险，现将能力绑定精确Writer Run并加拒绝测试；这是审查发现，不冒充本次KeyError的原因或已经发生的数据泄露。类型检查还要求为新增结构配置显式声明dict[str, Any]，已补齐。
- **复验与边界：** 首次定向复验1通过、1失败（`ev-recheck.log`）：同一新测试又误将workspace_id视作Run字段，补齐身份来源应通过Session查询。两次均为新测试投影假定错误，未改生产结果；最终仅复验该项1通过（`ev-final-recheck.log`），权限白名单unit在前次复验已通过；4个变更模块Mypy及Ruff通过。实际模型效果、付费接口和真实容器未验证。实现及测试修正提交 `ec075fa`。
- **经验、参与与追问：** Codex实施和静态复核，用户指定范围；应复用权威身份，不能猜投影字段。临时能力为何不能随持久后端配置复用？如何区分测试错误与权限边界问题？


## FAIL-008：master 推送的 Git HTTPS 连接超时

- **日期、范围与状态：** 2026-10-22，发布／Git传输，环境或外部故障，已完成（传输已恢复，底层根因未明）。已通过CI的源码候选918b40a、本地合并d16c006；源码与验收未改变。
- **失败事实与证据：** `git push origin master` 退出128，Git报告连接github.com:443在75,005ms超时；失败回执节选保存在 `.task_outputs/quality-increment/github-master-push-direct.txt`。GitHub API读取正常且核对远端master仍是acf9056，没有把本地合并当远端成功。
- **原因边界与处理：** 只能确认该次Git HTTPS连接失败，未证明DNS、路由或GitHub服务的具体根因。`scutil --proxy` 确认本机系统已有127.0.0.1:7899的HTTP/HTTPS代理；仅对Git命令设置http.proxy重试，不更改全局设置，不强推；该路径成功不等于查明原直连超时的底层原因。
- **复验及参与：** `git -c http.proxy=http://127.0.0.1:7899 push origin master` 成功，输出acf9056→d16c006，证据 `.task_outputs/quality-increment/github-master-push-proxy.log`；随后GitHub API返回完整d16c006399720536d0aca44924bb4c1d3e65a3f8，代码合并已在远端确认。修复未改变生产/测试/资源，仍复用918b40a的成功CI；最后仅补文档并同步。Codex在用户授权下执行、记录并排查，不宣称用户亲自修复网络或应用发生故障。
- **经验与追问：** 推送失败后先核对远端身份，不能把本地提交等同远端成功；按命令使用现有代理避免改变用户全局配置。为什么API可读不能保证Git直连可用？为什么换路径成功不能证明原网络根因？

## FAIL-009：超过采集上限的验收中段尚不可回读

- **日期、类型与范围：** 2026-10-22，自动化测试／新能力预期Red；基线7d84f6c加L1新增测试，已完成。不是线上事故，也不追溯改变旧回查能力的验收结果。
- **证据与现象：** `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/test_acceptance_output.py --tb=short`：1失败，2.53秒；日志 `.task_outputs/acceptance-output/red.log`。真实独立验收命令单个stdout约100KB，中间唯一标记不在首尾；前置断言确认短content及其全部旧分页均无该标记，随后新stream参数因尚未实现而TypeError。这分别证明采集丢失与新契约尚不存在，不能把缺少参数报错单独当作生产缺陷。
- **原因与处理：** `_BoundedOutput`只保留每流25,000字节头尾，Event Log未收到中段；已有约18KB用例只证明反馈层省略可回读。新增独立验收的受控脱敏文件及兼容分流分页，不增加重试/反思调用，不扩大预览。
- **复验与边界：** 实现后首批28通过/1显式Docker跳过（initial-green.log），随后长输出/存储/权限/重启的21项通过，包含真实Docker参数（lifecycle.log，12.20秒）；新增最终化路径故障及Docker创建失败接线也通过（integration.log）。中段标记可回读，短预览仍缺标记；原Red保留，不改判。Codex编写并运行受控用例，用户指定范围，不虚构真实用户影响或模型修复率收益。

### 同批采集hook的预期Red与定向复验

- **日期、范围与状态：** 2026-10-22，基线 `7d84f6c`，自动化测试／新增能力预期 Red；已完成。独立子任务 worktree 为 `ci-dedup/eventide`、分支 `codex/acceptance-output-executor`。
- **失败命令与证据：** `PYTHON_DOTENV_DISABLED=1 /Users/zesadv/Documents/Codex/eventide/.venv/bin/python -m pytest -q tests/test_command_results.py tests/test_container_executor.py -k 'output_observer or cancel_keeps_received' --tb=short`；5失败、61未选中，退出1，日志 `.task_outputs/acceptance-output-hook/red.log`。
- **已证实原因：** 旧 Local／Container 构造函数不接受新增 `output_observer` 参数，五项均为对应 TypeError；这是接口需求尚未实现，不能包装成生产故障，也尚未单凭此轮证明大日志回查已改善。
- **处理与复验：** 实现默认关闭的可选两流原始字节观察接口，Docker仅向命令采集器传入，不观察inspect/create/rm。随后同一解释器运行 `-m pytest -q tests/test_command_results.py tests/test_container_executor.py --tb=short`，65通过／1项既有真实Docker按开关跳过、退出0，日志 `green.log`；Ruff通过、Mypy两模块通过（各自日志同目录）。原25,000字节预览、计数、取消/超时和Docker控制流程未改变。未运行真实容器或付费模型，回调通过不代替主任务日志归档、脱敏及授权回读的效果验收；本例修复与文档同一提交，以Git历史追溯。
- **参与与追问：** 用户授权主任务，Codex分工实施；如何确保Docker管理输出不会误归档？为何回调观察与证据最终化需分开？

## FAIL-010：新归档的恢复与清理事实被终态规则拒绝

- **日期、类型与状态：** 2026-10-22，自动化测试／新增实现缺陷，已完成；基线8c0e238加未提交归档接线。不是原版本已发生的事故。
- **命令与证据：** `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/test_acceptance_output_storage.py --tb=short`：10通过、5失败，0.20秒，日志`.task_outputs/acceptance-output/storage-tests.log`。其中4项重启/过期/删除失败场景在追加归档事实时被`Run already terminated`拒绝；另1项错误原因断言见下条。
- **已证实原因：** 新归档将恢复和清理事实绑定已终态的原控制Run，现有Store禁止终态后追加一般Run事实。日志正文已存在并不等于允许重开运行生命周期；需要保留原规则，只对严格限定的归档维护事实及已开始证据身份设计接收路径。
- **处理与复验：** 归档事实改为同一Session的独立维护事实，以source_run_id绑定原运行；Store通用终态规则不改，没有伪造新工具完成或重开Run。storage套件随后15项通过（0.12秒，storage-green.log），包含4项原恢复/清理失败；用户指定证据范围，Codex实施并通过新增机制测试暴露问题，未运行真实模型。修复随本轮归档实现提交。

## FAIL-011：归档打开失败的原因被后续快照失败覆盖

- **日期、类型与状态：** 2026-10-22，自动化测试／新增实现缺陷，已完成；同FAIL-010批次和基线。
- **现象与原因：** 注入ENOSPC后归档文件未创建，finish仍尝试读取该路径，将已明确的open_failed改成snapshot_failed。两者都未声称完整，但丢失了最早有依据的故障原因。
- **处理与复验：** 仅对实际打开过的文件生成快照，保留首次有依据的打开失败原因；失败后仍计数后续收到字节。同一storage套件15项通过，包含原失败（storage-green.log）；最初失败证据同storage-tests.log首项，不虚构磁盘真的用尽。修复随本轮归档实现提交。

## FAIL-012：新归档首轮静态检查未通过

- **日期、类型与状态：** 2026-10-22，静态检查／新增代码规范问题，已完成；基线8c0e238加未提交归档实现。
- **证据与原因：** 运行变更文件的`ruff check --fix`后退出1，剩4项：不可在参数默认值中调用OutputLimits（B008），及三处超过100字符（E501）；不是运行时故障。原命令及输出保留本轮维护对话，后续结果写入.task_outputs/acceptance-output/静态日志。
- **处理：** 使用模块级不可变默认配置并换行，未修改配额或运行语义；Ruff随后通过，8个变更模块Mypy通过，证据分别为ruff.log、mypy.log；未改变运行语义。Codex新增实现并处理规范问题，不虚构用户影响。


## FAIL-013：分流参数增加后工具目录超出原预算

- **日期、类型与状态：** 2026-10-22，自动化测试／新增接口固定开销超限，已完成；基线8c0e238加未提交归档接线，不是线上故障。
- **证据与原因：** `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/test_acceptance_output_storage.py::test_finalize_path_error_preserves_both_stream_records tests/test_container_executor.py::test_verifier_creation_failure_is_not_empty_complete_output tests/test_delivery_repair.py tests/test_tool_result_paging.py tests/test_host.py::test_tool_catalog_stays_within_its_budget tests/test_context_builder.py --tb=short`：31通过、1失败、1真实Docker按开关跳过，120.13秒，日志 `.task_outputs/acceptance-output/integration.log`。唯一失败为工具目录4,034字符超过4,000上限；新stream schema及说明带来固定请求开销，日志回读行为断言均通过。
- **处理与取舍：** 缩短read_tool_result工具说明，保留stream字段、权限及4,000目录上限，不靠提高预算消除断言；修复后仅定向复验该失败，再按计划一次全量收口。Codex实施与定位，未运行真实模型，不宣称Token收益。

- **FAIL-013复验：** 仅复验原目录失败与受中断原因字段调整影响的Local验收用例，共4通过、1显式Docker参数未选中（7.97秒，recheck.log）。目录回到4,000字符上限内；先前真实Docker中段回读已通过，不因本轮缩短说明重复运行。全仓Ruff及124模块Mypy通过（ruff-final.log/mypy-final.log）。

### 本批最终收口（FAIL-009–013）

2026-10-22：同一本地归档实现的一次全量 `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q --cov=eventide --cov-fail-under=85` 为2,002通过、12显式跳过、覆盖93.74%（1091.87秒），无新增失败；1条Starlette/AnyIO依赖弃用警告单列，不当失败或隐藏。Ruff全仓、Mypy124模块、服务help/启动冒烟、离线smoke12/12通过；对应日志均在.task_outputs/acceptance-output/。原Red及各次失败保留，修复代码、专项测试和本段同提交，可按本文件与acceptance_output.py的Git历史追溯；无付费模型、用户事故或修复率提升结论。

## FAIL-014：MCP 边界新增测试误用策略构造接口

- **日期、类型、基线：** 2026-10-22，自动化测试／新测试问题；05cb80b加TC-1测试，已完成。
- **证据：** `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/test_mcp_outcomes.py --tb=short`，2失败，0.12秒；`.task_outputs/tool-outcomes/red.log`。两项均在PolicyEngine("agent")构造处TypeError，尚未执行工具。
- **原因与处理：** 新测试错误猜测策略接口；改用无参数PolicyEngine并显式提供审批处理器。此轮不能证明MCP缺陷，也不是预期Red。Codex编写并定位，不虚构线上影响；复验见下方汇总。

- **FAIL-014追加（同类新增测试接口假定）：** 首批五文件定向运行68通过/10失败（19.32秒，`backend-targeted.log`）。10项均已验证unknown和单次派发，但Continue实际在更早的Workspace门禁以“unresolved tool outcome”拒绝，测试错误匹配后置校验的“Unknown tool outcome”。改为断言实际入口的精确拒绝原因，保留次数、停驻及重启断言，不改生产检查。该问题与FAIL-015的旧实现缺陷分开记录。

- **FAIL-014静态复验追加：** 修正精确错误断言后，变更文件Ruff首次退出1，仅新增测试的三行断言超100字符（`ruff.log`）；换行处理，运行语义不变，后续静态复验通过，见下方汇总。


## FAIL-015：MCP 丢失响应与文本前缀被错误归类

- **日期、类型、基线：** 2026-10-22，自动化测试／受控缺陷复现；05cb80b加新增测试，已完成。未观察到线上重复副作用。
- **证据：** 与FAIL-014相同命令修正fixture后2失败，0.12秒；`.task_outputs/tool-outcomes/behavior-red.log`。替身已记一次远端写入后抛ConnectionError，结果只有execution_failed；另一调用明确isError=false，但正文以Error:开头仍被标失败。
- **原因与处理：** MCP handler将结构化结果压成字符串，未区分调用中失联与明确工具错误；接入已有unknown标记并保留显式isError。两例分别覆盖安全停驻和错误可观察性，不借此宣称远端执行恰好一次；复验见下方汇总。

### FAIL-014/015定向复验

修正测试后仅复验`tests/test_mcp_outcomes.py`：16通过、5.93秒（`mcp-recheck.log`）；包含原两项缺陷、8种传输异常、真实等待超时/取消、重启拒绝Continue、明确MCP失败三次停驻和参数/权限/协议拒绝。前批另外62项既有契约/循环/真实本机stdio及HTTP测试通过，不重复运行。三处格式换行后Ruff通过（`ruff-green.log`）。没有真实远端写服务、付费模型或线上事故证据。实现与复验文档同提交`d2fccd6`，由本文件及mcp/client.py的Git历史追溯。

复述要点：远端已执行不等于本地收到结果；先区分明确失败与结果未知，再决定能否继续。Codex实施与故障注入，用户指定范围；可追问为何取消不能证明撤销、保守停驻会牺牲哪些可用性、显式isError为何比文本前缀可靠。

TC-2收口：存储/Host/Runtime/Trace61项、原unknown恢复2项、前端13项及分发2项通过，未新增失败；全仓Ruff与Mypy124模块、Vue类型/构建和变更格式通过。既有Starlette/AnyIO弃用警告单列，不作失败。TC-1/2只运行受影响范围，不把143项不同后端定向通过称为本轮完整CI或模型效果通过。

## RG-EVAL：检索评测冻结与受控执行

- **已完成定向复验与取舍：** 538c450冻结数据阶段Mypy补类型后通过。Runner初次5通过/2失败；只修唯一工具ID后两项参数化Agent测试通过（5.58秒，`eval-agent-recheck.log`），Ruff换行/绑定修正后通过，Mypy两模块通过。随后为区分20题与60本地计时样本、记录跨副本/过期误引用，定向复验受影响3项通过（9.74秒，`eval-metrics-recheck.log`）；新增质量失败不重试/无需RAG、资源跨cwd与旧引用3项通过（1.26秒，`eval-final-targeted.log`），新增Agent取消1项通过（`eval-agent-cancel.log`）。总计原数据22＋资源1＋Runner9＝32个不同用例通过，非全量；未重跑历史测试。所有网络边界使用替身，没有模型效果结论。Runner提交可通过本节与模块Git历史定位。
- **经验与可追问点：** 脚本替身也要遵守真实协议的唯一调用身份；多次本地计时不能放大质量统计分母；未知模型调用必须停批并留剩余未尝试，不能以零值或补轮掩盖。Codex负责fixture、实现和故障注入；未声称用户事故、真实检索收益或个人人工排障经历。

- **两臂首轮失败定位：** 原Store的`run.failed`明确为 `Duplicate tool call IDs in model response`。新增ScriptedProvider fixture连续两轮未写id，默认均为call_1，触发已有协议检查；改为语义与文件读取各自唯一ID，不改生产检查、标签或脚本响应次序。原始两个隔离Store与report已复制到忽略目录的 `eval-first-failure/` 供复核；仅重跑两项参数化Agent测试。

- **Runner首轮定向：** 借入Root生产接线补丁后运行 `PYTHON_DOTENV_DISABLED=1 /Users/zesadv/Documents/Codex/eventide/.venv/bin/python -m pytest -q tests/test_retrieval_upgrade_evaluation.py --tb=short`，5通过/2失败（6.70秒）；两臂调查在首个候选前停止，原因待查，报告与Store留在pytest临时产物，摘要 `.task_outputs/retrieval-upgrade/eval-runner-tests.log`。同轮Ruff复验仅新增测试两处长行失败，已按100字符规则换行。无付费调用，不把fixture失败当真实模型失败。

- **Runner静态检查追加：** 初次 `ruff check eventide/rag/agent_evaluation.py eventide/rag/upgrade_evaluation.py` 退出1：21处循环回调未显式绑定当前尝试变量及2处长行；日志 `.task_outputs/retrieval-upgrade/eval-runner-ruff.log`。虽然调度逐例await，本次仍改为显式固定回调参数，防后续调度变化串题；不放宽检查。尚未运行模型或新Runner测试，复验记录随后追加。

- **2026-10-22，新代码静态检查问题，基线0a80177：** 新增8题数据和独立判分的22项定向测试、Ruff通过；`PYTHON_DOTENV_DISABLED=1 /Users/zesadv/Documents/Codex/eventide/.venv/bin/python -m mypy eventide/rag/agent_evaluation.py` 首轮退出1，唯一错误为批次列表缺显式类型（`var-annotated`）。证据 `.task_outputs/retrieval-upgrade/eval-contract-mypy.log`。补充列表类型，不改运行行为或冻结标签；仅复验该模块Mypy。此为维护期静态问题，不是线上故障或模型失败；Codex编写、定位和修正，可追问冻结批次为何不能按结果选择重跑。

## RG-CORE：检索证据与独立索引的预期 Red（2026-10-22）

- 类型：新增能力的预期 Red，非线上事故；基线 `0a80177`（生产实现同 `845a6b0`）。
- 命令：`PYTHON_DOTENV_DISABLED=1 /Users/zesadv/Documents/Codex/eventide/.venv/bin/python -m pytest -q tests/test_retrieval_evidence.py`；两项失败、0.14 秒。证据：本任务 worktree 忽略目录 `.task_outputs/retrieval-upgrade/core/red.log`。
- 现象和已证实原因：修改源文件后，旧检索依然返回 `stale_unique_marker` 正文，查询只核对模型指纹，没有核对命中文件内容；指定外部 `index_dir` 产生 TypeError，旧索引路径固定在源码 `.eventide`。两项分别证明旧正文可信性与存储位置缺口，不宣称已复现跨用户数据泄露。
- 执行包装错误：首次命令用 zsh 只读变量 `status` 保存退出值，报告命令另报 shell 错误；pytest 已完整执行，原日志保留，未为包装错误重复测试。后续使用专属变量或工具直接返回退出码。
- 处理及复验：待实现后追加；计划分离源码与存储、用同一数据库快照记录哈希和代次，再核对最终命中文件。
- 实际参与与可追问点：维护代理在临时 Git 项目编写并运行确定性测试；无真实模型、用户项目或生产事故影响，不能据此宣称模型效果提高。


- 静态检查问题：首批 59 项和扩展 92 项定向测试通过后，Ruff 在新增的外部索引参数和诊断文本发现 3 处 E501 换行问题；日志 `core/static.log`。原因是本轮新增行超过仓库 100 字符上限，调整换行，不改变行为；复验随下方收口摘要。
- 已完成处理及定向复验：外部目录保留原有路径检查，源码不再决定唯一存储位置；候选哈希与代次取自同一读快照，最终命中文件核对失败时移除正文，而不是用旧内容补足结果。先 59 项通过，再补齐专项后 92 项通过（11.95 秒），另本地检索/冻结 benchmark/旧离线评测与开发接口 70 项通过、1 项真实模型按开关跳过（4.50 秒）；总计 162 个不同用例。Ruff 和四模块 Mypy 复验通过；保留既有依赖弃用警告，不把跳过算通过。Red 提交 `e28add2`，实现提交由本节和相关源码 Git 历史定位；日志 `core/expanded.log`、`compatibility.log`、`static-green.log`、`mypy.log`。
- 取舍及经验：每次只核对最终至多五条命中的当前文件，增加少量有界读取以防止旧正文被误用；不自动重新索引，不提高结果数量，也不能保证文件之后不变。真实模型效果、主链生命周期和页面由主任务另验收，不用此处机制通过替代。

## RG-UI：检索升级前端新增测试的 DOM 类型错误（2026-10-22）

- 类型：测试问题。基线 `0a80177`，本轮新增检索 UI 未提交状态；不是线上故障，也不影响后端验收结论。
- 失败命令：`cd frontend && npm run typecheck`。70 项相关组件测试此前通过，但 TypeScript 在 `tests/deliveries.test.ts:395` 报 TS2339：通用 `VueNode<Element>` 没有 `type` 属性。原始输出：忽略目录 `.task_outputs/retrieval-upgrade/ui/typecheck.log`。
- 已证实原因：新测试用 `findAll("article input")` 获得通用 Element，再直接读取 `element.type`；运行时 DOM 有该属性，但静态类型未声明为 HTMLInputElement。
- 处理与取舍：使用精准 CSS 选择器选择镜像输入，不增加生产类型断言或放宽类型检查。类型定向复验通过，关联交付组件也已通过；提交随 RG4-UI 保存。经验：测试 DOM 选择器应表达所需元素类型，运行时测试通过不能代替静态检查。实际参与为新增页面与测试、定位并修正测试，不虚构用户影响。

### RG-UI 补充：SSE 恢复测试初轮未更新（待查）

- 类型：测试问题／原因待查。仍为 `0a80177` 上的本轮未提交修改。
- 命令：`npm --prefix frontend test -- tests/indexes.test.ts tests/execution.test.ts tests/deliveries.test.ts tests/sandbox.test.ts tests/workbench.test.ts`。结果 53 通过、1 失败；新用例注入 `retrieval.status` 后仍读到 preparing，未得到 expected available。原始输出 `.task_outputs/retrieval-upgrade/ui/recheck.log`。
- 先核对事件订阅替身和既有会话身份初始化，不据此认定生产 SSE 有故障；原因和复验待补。
- 第一次原因已证实：fixture 的 `api.runs.mockResolvedValue([...])` 每次返回同一个数组，生产刷新会替换其中最新 Run；重复使用同一原始引用不等价于 HTTP 每次解析新 JSON，也没有触发 Vue 的数组替换。该新用例改为每次返回新数组后，available 状态断言通过，未修改生产逻辑。
- 随后单用例复验又暴露测试访问私有 `store.cursors`（未导出）的 TypeError。证据 `.task_outputs/retrieval-upgrade/ui/sse-recheck.log`；改为断言公开 events 中的序号，并复验整个相关测试文件。两次均属新增测试问题，未放宽状态或权限断言。

- 最终复验：`tests/workbench.test.ts` 12 项全部通过（`.task_outputs/retrieval-upgrade/ui/sse-final.log`）；SSE 状态更新及重新创建 Pinia 后读取恢复均通过。前一次相关 53 项通过仍保留，不重新执行无关测试。经验：HTTP 替身每次返回独立 JSON 对象，断言公开接口，避免将测试中的共享引用或私有实现细节误判为生产故障；提交随 RG4-UI 保存。


## RG-LIVE：标准 embedding-3 首次接入未成功

- **日期、类型、基线：** 2026-10-22，真实 API／环境或外部故障待查；`0a80177` 加本轮未提交主链接线。不是模型检索质量失败。
- **证据：** `.task_outputs/retrieval-upgrade/live/probe.py`，退出 2；`live/probe.json` 与隔离 `live/probe-state/runtime.sqlite`。只派发 1 次受 Host 管理的请求，0 个成功向量，1 次未知用量，预留未按零消费释放。
- **现象与已知边界：** 返回 `EmbeddingRequestError: Embedding request or client cleanup failed`。现有适配器隐藏供应商异常细节，当前证据不足以归因于认证、余额或网络；不得写成“余额不足”。使用的是此前用户授权的智谱密钥，未写入文件或事件。
- **处理：** 停止扩大调用，保留失败及未知用量；离线实现继续。用户先选择“已配置”，随后澄清尚不清楚密钥获取位置；已提供官方标准 API 入口，尚未再次派发请求。后续配置完成再使用新尝试身份，不覆盖首轮失败。
- **参与与总结：** Codex 执行单次受控接入，用户处理外部配置。可追问：为什么接口失败不能记真实零 Token，以及错误脱敏与故障可诊断性之间的取舍；尚未证明检索效果。

### RG-LIVE：改用 Coding Plan 地址仍未获得向量（2026-10-23）

- **类型与基线：** 真实 API／外部接入失败，基线 `b283495`。用户明确要求停用标准 API；仅将忽略的本机 `.env` 中 embedding 地址改为 `https://open.bigmodel.cn/api/coding/paas/v4/`，保留密钥及权限0600，没有修改聊天配置或生产代码。
- **命令与证据：** `PYTHONPATH=. .venv/bin/python .task_outputs/retrieval-upgrade/live/probe-coding.py`，退出2。独立 `embedding3_smoke_03_coding` 经 RuntimeHost 派发一次，结果在 `live/probe-03-coding.json`，账本在 `live/probe-03-coding-state/runtime.sqlite`；1次attempt、unknown1、没有向量，不覆盖前两次记录。
- **已证实结果与限制：** Coding Plan 地址同样返回HTTP429／数字码1113。官方错误码将1113解释为账户欠费；但该响应不能单独证明此密钥属于哪种账户、套餐是否可用或 `/embeddings` 具体如何计费。[套餐模型列表](https://docs.bigmodel.cn/cn/coding-plan/overview)未列embedding-3，[embedding-3文档](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)仅展示标准接口，因此不能宣称换URL即可用套餐抵扣embedding。
- **处理与取舍：** 失败后停止，未重试、未回退标准API、未启动40查询或24次Agent批次；未知用量不改零。当前配置只是按要求切换地址，不能称为可用。后续需确认供应商支持及授权，或经用户选择另用本地语义模型；不擅自改变冻结实验基线。
- **参与与总结：** Codex核对官方资料、切换本地配置并做一次受Host管理的验证。可追问聊天生成和向量接口为何不能互换、HTTP429为何需结合业务码、无法确认计费时为何保留unknown；没有已证实的语义质量收益。

## RG-ROOT：任务准备接线的定向验证

- **受控缺陷复现：** 交叉审查发现本轮新增手动索引workspace_id会改变控制Session，原run_receipt因此改变同一项目HTTP请求的去重身份。同一Idempotency-Key改目标可能再次构建；新增两方向测试复现202而非409，证据`index-key-red.log`。这属于本轮未发布实现缺口，不是历史线上事故。修复保持项目级请求身份，实际执行仍绑定具体副本；定向复验另记。

- **幂等复验：** `index-key-green.log` 两项通过（0.87秒），同时保留同目标重放与仅一条构建历史断言。原源目录请求身份算法不变，不增加回执事实源。经验：资源目标放入请求内容，不能随目标一起改变同一HTTP入口的去重范围；修复随主分支本地提交。
- **最终静态检查：** 全仓Mypy126模块通过；Ruff仅delivery_runtime新补类型的行长101失败（final-ruff.log），换行后复验，不改逻辑。完整Vue199项与类型/构建通过。

- **后端全量收口首次失败：** 完整pytest执行中，旧`test_local_admission_keeps_legacy_idempotency_fingerprint`出现失败（final-pytest.log）；先登记，具体断言及原因待完整报告确认。其余用例继续执行，不中断后重跑全量；随后仅定位并复验受影响范围。

- 旧测试以当前RunBody推算旧请求，排除sandbox字段但没有排除新knowledge_index默认值，因此期待的旧指纹发生变化；生产路由已明确省略existing，仍保持旧身份算法。本次只修测试的旧字段集合，同时新增同键显式existing回放、改prepare返回409断言；不修改生产实现或旧请求口径。

- **最终后端结果及复验：** 唯一完整运行2087通过、1失败、12显式可选跳过，1082.98秒；85%覆盖率门禁达到93.69%。旧指纹测试修正后仅该项复验通过（0.78秒，final-backend-recheck.log），含新增默认兼容和不同意图拒绝；受影响测试Ruff通过。不将组合证据写成第二次全量全绿。代码接线0e447d0、核心dcb436e、边界8e36c00、评测9694c90、浏览器813b30e、幂等修复d8e1d0a；测试与分发收口提交由本节Git历史定位。参与为实现、测试和故障定位，不以机制测试替代真实模型效果。当前仅标准API额度及真实效果对照待外部验证。

- **最终浏览器收口：** 同源构建产物全套35旅程为34通过、1失败；既有生成参数页在查询默认值时超时，单项报告5.9分钟而测试截止45秒，原因尚未证实（final-playwright.log及保留error-context）。本轮新增4条检索旅程全部通过；先保留失败产物，仅复验该参数页，不改变超时、断言或产品逻辑，不把推测的主机暂停/负载写成结论。

- 浏览器原样定向复验1通过（1.1秒，final-playwright-recheck.log）。超时原因没有新增证据，不改写为已确认环境故障；首轮34项通过不重复运行。
- **安装包构建产物问题：** 本地`uv build --wheel`之后`check_wheel.py`发现过期或缺失前端资源，退出1（final-wheel-check.log）。现有setuptools build目录保留旧哈希资源；先把该目录移动到忽略产物中保存，再全新构建并仅复验wheel。未删除用户源码或放宽资源精确集合校验，不将旧缓存混入发行包。

- 干净构建及wheel隔离资源/服务复验通过（final-wheel-clean-build.log、final-wheel-clean-check.log）；旧缓存保存在`.task_outputs/retrieval-upgrade/pre-clean-build`，原失败包保留在dist，最终通过包在clean-dist。该类问题沿用现有严格打包检查发现和处理，未新增另一套分发机制。

- **2026-10-22／维护期静态检查问题：** RG1已冻结，主链接线尚在实现。变更文件Ruff首轮退出1：4处导入排序、2处行长；`.task_outputs/retrieval-upgrade/root-ruff.log`。仅整理新增导入和换行，不改变边界、预算或验收；复验随本轮定向验证记录。Codex实现和修正，无模型效果或线上故障含义。

- **RG-LIVE 新密钥复验：** 用户提供新的标准API Key后，单独保存到Git忽略且权限0600的本机`.env`，仅设置embedding字段。隔离`probe-02`再次只派发1次，退出2，返回HTTP429、供应商数字码1113；`.task_outputs/retrieval-upgrade/live/probe-02.json`与对应独立Store保留本次证据。未返回usage，1次未知保留；未扩大索引或模型批次。新增安全错误诊断不记录响应原文/密钥，35项embedding定向回归通过（embedding-runtime.log），HTTP401替身验证状态/数字码可见且密钥不泄露。供应商码含义待官方资料核对，不能把所有429都解释成限流。

- **RG-LIVE 外部原因核对：** 智谱官方错误码页 `https://docs.bigmodel.cn/cn/faq/api-code` 将1113/HTTP429定义为账户欠费；第二次故障归类为标准API额度阻塞，已告知用户处理所属账户余额/适用资源包后再恢复。首轮仅通用错误，仍不倒推为同一已证实原因；两次均保留，不重写未知Token为0。

- **RG-ROOT 类型检查：** 9个受影响模块Mypy首轮2项错误（root-mypy.log）：可选workspace_id使改写handler返回str/ToolOutput联合类型，Writer契约字典缺显式类型。生产调用已有真实Workspace身份，收紧内部handler参数为必填str并补字典类型；行为不增加兼容回退，复验见后续。
- **RG-ROOT 并行整合工具问题：** 多worktree共用Git stash栈，默认索引发生交错；误删除的是评测代理暂存引用，已按完整对象SHA恢复全部两份实现，双方工作区核对无源码损失。只处理本轮自己的暂存，不触碰旧用户stash。后续并行整合用具体SHA或独立patch，不用默认stash栈顶。此为维护流程问题，非产品运行或线上事故。

- **RG-ROOT 首次主链集成：** `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/test_retrieval_preparation.py tests/test_index_control.py tests/test_retrieval_runtime.py tests/test_rag_tool.py tests/test_delivery_repair.py --tb=short` 返回6失败、46通过、1项真实Docker按开关跳过，134.20秒；证据 `root-integration.log`。新增测试漏传list_sessions的workspace_id、未创建fixture父目录、重复删除已释放admission；旧测试仍替换已改名的构建接线、断言旧源码索引位置。均为本轮测试接线/预期问题，已通过的源/副本/Writer与取消机制不重跑；修正后先复验这6项，保留原断言目的。不是用户事故或模型效果失败。

- RG-ROOT边界修正后的Ruff再次报告3处新增长行及1处可合并if；root-ruff-recheck.log，均只调整新改行的格式，不改判定。

- **RG-ROOT定向复验：** 上述6项原失败全部通过（1.68秒，root-integration-recheck.log）；随后新增source/worktree Continue继承与配置漂移、ScriptedProvider收到过期提示后读取当前源码3项通过（5.36秒，continue-stale.log）。9模块Mypy与受影响Ruff通过；无真实请求。首轮其余46项结果保留，不将本次复验写成重跑全量。
### RG-CORE：准备边界新增测试的口径及前提错误

- 类型：测试问题及复核推断纠正，非生产回归；基线 `fc666fe` 加借用的主任务 `root-runtime-v2.patch`，未提交借用生产改动。
- 命令：`PYTHON_DOTENV_DISABLED=1 /Users/zesadv/Documents/Codex/eventide/.venv/bin/python -m pytest -q tests/test_retrieval_preparation_boundaries.py`；2026-10-22 首轮 2 通过、3 失败 / 1.68 秒。日志 `.task_outputs/retrieval-upgrade/core/preparation-boundaries.log`。
- 已证实原因一：65 文件索引首批已报告 20,001 Token 后确实没有第二次 embedding 请求，Run 正确停驻；测试额外要求账本包含 `output_tokens: 0`，违反已有 embedding 输入型口径。改为只断言实际的 `input_tokens`，仍保留单次请求、已知消费及停驻全部检查。
- 已证实原因二：新增的两个状态根嵌套场景假设能在源目录内的状态根创建 managed worktree / Writer，但既有 `WorktreeService` 在创建之前就要求状态根位于源 Workspace 之外。此前只读复核未追到此已有前置条件，错误地把新检索源检查视为可达功能缺陷；该推断已撤回，不放宽旧 worktree 安全边界。后续改为准确验证既有约束，并单独检查外部注册副本的实际源码边界。
- 首轮已通过的范围：注册路径被替换为另一仓库链接时，状态查询不可用且手动构建拒绝；源码临时不可见时，已完成索引历史仍可读取。不为补日志重复运行这两项。
- 参与与经验：维护代理编写故障注入与测试；错误来自测试对现有口径和前置条件理解不全。可追问 input-only 用量为何不能补造输出字段，以及为什么要检查完整调用链后再认定可复现问题。后续定向复验另追加，不把这一轮称为全通过。
- 修正后仅复验原预算断言及替换后的实际副本用例：2 通过 / 1.56 秒，`core/preparation-boundaries-recheck.log`；Ruff 与 diff 检查通过。新用例使用用户在源目录外创建的真实 Git worktree，再注册到同项目并直接绑定 Session；源 prepare 拒绝，实际副本 prepare 成功，不借此宣称 Host 可在嵌套状态根创建 managed worktree 或 Writer。最终四项边界各有通过证据，前两项原通过未重复运行；原五项尝试的失败仍保留。


## RG-UI-E2E：真实检索浏览器旅程首轮路径校验失败（2026-10-22）

- 类型：环境／测试夹具问题，原因待核实；基线为前端 `d004f0e`、借入 core `fc666fe`（本地 cherry-pick `9a83339`）与主任务未提交 `root-runtime.patch`，后端借入修改不由本子任务提交。
- 命令：`PYTHON_DOTENV_DISABLED=1 EVENTIDE_PYTHON=/Users/zesadv/Documents/Codex/eventide/.venv/bin/python npm run test:e2e -- tests/e2e/retrieval.spec.ts`（frontend 目录）。首轮 1 通过、3 失败／12.8 秒；等待准备锁时取消与刷新通过，源目录/worktree/Writer 在准备阶段停止，尚未执行模型。
- 现象：Run 报 `Knowledge index path contains a link or unsupported file type`；Writer 同时返回 `writer_stopped`、零调用与零验收轮次。需核对 macOS 临时目录 `/var` 到 `/private/var` 的别名与 fixture 状态路径是否规范化；不把推测写成生产实现原因。
- 证据：`.task_outputs/retrieval-upgrade/ui/e2e-first.log`，浏览器 error-context 原件存放 `frontend/test-results/retrieval-*/error-context.md`（忽略产物）。处理和定向复验待补；没有真实模型请求或用户项目影响。

- 路径原因已证实：测试服务用 `Path(temporary)` 保留 macOS `/var` 目录别名，而新外部索引目录校验拒绝祖先链接；将测试临时根先 `.resolve()`，不改变生产 IndexStore 的链接拒绝规则。首轮 error-context 已复制到 `.task_outputs/retrieval-upgrade/ui/e2e-first-artifacts/`，避免 Playwright 后续覆盖；只复验三个失败旅程。

- 最终定向复验：同一隔离真实服务，仅运行 `retrieval.spec.ts --grep 'source preparation|ordinary worktree|delivery Writer'`，3/3 通过（约1.1分钟），证据 `.task_outputs/retrieval-upgrade/ui/e2e-recheck.log`；此前等待锁取消旅程已通过，不重复运行。合计4个不同旅程通过，没有付费模型调用。新增浏览器用例和测试服务钩子在 `d004f0e`，夹具路径修正与本记录随 RG4-E2E 收口提交；原失败不是生产事故，实际参与为编写定向旅程、定位夹具路径别名及复验，不能宣称模型检索质量提高。

- 与主任务交叉核对：生产 `Settings.from_env()` 已对状态目录调用 `.resolve()`；本问题来自浏览器服务手工构造 Settings 绕过该入口，故只改 fixture 根路径，不放宽索引链接校验。

### RG-LOCAL：角色提示适配的索引指纹测试夹具为空（2026-10-23）

- 类型：测试问题；不是用户事故或生产缺陷。基线 `2696b2e` 加本地角色适配未提交改动。
- 命令：`PYTHON_DOTENV_DISABLED=1 <项目 .venv>/bin/python -m pytest -q tests/test_local_retrieval.py -k 'package_roles or missing_prompts or invalid_prompt or prompt_policy or distinct_roles'`。首轮 9 通过、2 失败、43 未选；日志 `.task_outputs/retrieval-upgrade/local-roles/new-tests.log`。
- 已证实原因：两项指纹变更用例只调用 `make_repo`，其初始文件不属于可索引语料，空索引无法推断本地模型维度；尚未触达要验证的指纹拒绝路径。
- 处理与取舍：只补实际可索引文档，不修改生产空语料保护、不伪造向量维度。第一次仅复验两项均触达正确拒绝，但新测试错误消息少写 `with`，再次 2 失败；证据 `local-roles/recheck.log`。随后只修断言匹配现有明确消息；原失败2项及尚未运行的相关旧用例合计72通过、1项真实模型显式跳过，不重复首轮已过9项，最终81个不同用例通过。Ruff通过，证据 `local-roles/regression.log`、`ruff.log`；相关代码、测试与本记录同一本地提交。真实模型效果不由此判断。
- 实际参与与经验：本轮编写角色适配及定向测试，发现的是新增测试前提不足；可追问固定模型提示如何进入指纹、为何应在请求前拒绝不匹配索引，不能讲成解决了线上检索事故。

### RG-LOCAL：共享依赖环境阻断 Python 3.10 类型目标（2026-10-23）

- 类型：环境/外部依赖故障。变更三模块运行 `python -m mypy eventide/rag/embedding.py eventide/rag/local_models.py eventide/rag/search.py`，退出2；共享 `.venv` 的 `numpy/__init__.pyi:737` 使用Python3.12 `type`语法，仓库默认3.10目标在检查项目代码前失败。证据 `.task_outputs/retrieval-upgrade/local-roles/mypy.log`。
- 已证实环境变化：主任务安装本地retrieval可选依赖，新增的NumPy类型文件引发语法不兼容；本子任务没有改动依赖。先完成3.12目标三模块检查，再使用主任务提供的既有 `.task_outputs/publish-py310/bin/python` 保留默认3.10目标复核，三模块均通过。证据 `local-roles/mypy-py312.log`、`mypy-py310.log`；没有放宽仓库门槛或改生产代码，原失败保留。
- 经验与范围：区分类型环境解析失败与项目类型错误，复用已有最小检查环境；没有重跑全仓或为修测试重新安装依赖。记录随本地角色适配同提交，真实模型推理另验收。

### RG-LOCAL：冻结检索留出集仍有漏召回（2026-10-23）

- 类型：真实本地模型／检索效果失败，非运行异常。基线 `9318396`；固定Qwen3-Embedding-0.6B revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`、官方query提示、CPU、无重排/改写。原16语料40查询不改，开发20先执行，再一次留出20；质量分母按题计，三次重复仅测本地排序时间。
- 命令：`PYTHON_DOTENV_DISABLED=1 PYTHONPATH=. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python .task_outputs/retrieval-upgrade/local-qwen/compare.py`。退出0表示批次执行完成，不代表每题命中；完整结果 `local-qwen/comparison.json`、输出 `comparison.log`，进程禁止网络。
- 结果：开发三臂均20/20；留出FTS5为16/20、Hash混合15/20、Qwen混合19/20。Qwen未通过 `hold-redis`；FTS5另漏 `hold-password`、`hold-db`、`hold-unauth`，Hash另漏上述三题及 `hold-sql`。0运行异常、Qwen空结果0；漏召回的具体排序原因待核对保留命中证据，不推测为源码或协议故障。
- 处理与取舍：不修改标签、query提示、候选数或重跑留出集。保留唯一失败与全部基线失败，不能把19/20写成全部正确；排名改善不等于Agent调查或工程修复率提高。实际参与为接线、固定模型与实验执行；待补充失败证据解释和最终提交，真实Agent下游对照仍未执行。

- 结果核对：`hold-redis`询问缓存服务器连接数量上限，标注目标为 `docs/redis-pool.md`。保留的前5条结果是缓存过期、缓存函数、金丝雀部署、授权失败及密码恢复，均为已核对的新鲜正文，目标未入前5；这是相关性失败，不是过期或跨副本误读。当前报告没有保存未返回候选的全部名次，不能断言错误只来自Qwen向量或RRF融合；不为分析而重新计算留出查询。角色适配提交主分支 `9318396`；本地完整Host冒烟另外通过，`smoke.json`显示真实向量1024维、禁止网络、源码无改动及无变化增量复用。质量回归无需“修到全过”才结束试用，后续优化必须使用开发集或新冻结集，保留当前结果。
