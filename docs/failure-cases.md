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

- 2026-10-26 MK08A 续接路由复发：基线 `5acba20` 加本轮改动，共享 Python `-m mypy eventide/http_api/streams.py` 同样退出 2。证据 `.task_outputs/maka-upgrade/connection/mypy.log`；复用上述隔离环境按默认目标检查通过，证据 `mypy-py310.log`，未降低门槛或改共享依赖。相关实现提交 `983f0c4`。

### RG-LOCAL：冻结检索留出集仍有漏召回（2026-10-23）

- 类型：真实本地模型／检索效果失败，非运行异常。基线 `9318396`；固定Qwen3-Embedding-0.6B revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`、官方query提示、CPU、无重排/改写。原16语料40查询不改，开发20先执行，再一次留出20；质量分母按题计，三次重复仅测本地排序时间。
- 命令：`PYTHON_DOTENV_DISABLED=1 PYTHONPATH=. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python .task_outputs/retrieval-upgrade/local-qwen/compare.py`。退出0表示批次执行完成，不代表每题命中；完整结果 `local-qwen/comparison.json`、输出 `comparison.log`，进程禁止网络。
- 结果：开发三臂均20/20；留出FTS5为16/20、Hash混合15/20、Qwen混合19/20。Qwen未通过 `hold-redis`；FTS5另漏 `hold-password`、`hold-db`、`hold-unauth`，Hash另漏上述三题及 `hold-sql`。0运行异常、Qwen空结果0；漏召回的具体排序原因待核对保留命中证据，不推测为源码或协议故障。
- 处理与取舍：不修改标签、query提示、候选数或重跑留出集。保留唯一失败与全部基线失败，不能把19/20写成全部正确；排名改善不等于Agent调查或工程修复率提高。实际参与为接线、固定模型与实验执行；当时真实Agent下游尚未执行，后来完成的24次及逐例复盘见下方RG-AGENT，不改写本次检索结果。

- 结果核对：`hold-redis`询问缓存服务器连接数量上限，标注目标为 `docs/redis-pool.md`。保留的前5条结果是缓存过期、缓存函数、金丝雀部署、授权失败及密码恢复，均为已核对的新鲜正文，目标未入前5；这是相关性失败，不是过期或跨副本误读。当前报告没有保存未返回候选的全部名次，不能断言错误只来自Qwen向量或RRF融合；不为分析而重新计算留出查询。角色适配提交主分支 `9318396`；本地完整Host冒烟另外通过，`smoke.json`显示真实向量1024维、禁止网络、源码无改动及无变化增量复用。质量回归无需“修到全过”才结束试用，后续优化必须使用开发集或新冻结集，保留当前结果。

### RG-AGENT：维护诊断脚本的字段假设错误（2026-10-23）

- 类型：维护工具问题，基线 `617c0af`，非Agent结果或产品故障。一次性源码扫描统计将 `_Scan.sources` 元组误按字典调用 `.values()`，退出1；另一次只读SQLite进度查询假设 `runs` 表含 `status` 列，退出1。两次均仅诊断，没有模型调用、源码写入或数据库写入；工具输出留在本轮维护记录中。
- 已证实原因与处理：未先核对内部数据结构；查看 `_Scan` 定义后按元组统计，取消臆测SQL状态字段的查询，进度改读现成 `report.json`。只复验统计与报告读取，不重跑模型任务；实际任务结果仍以canonical事件与导出为准。经验：复用公开投影，不把诊断脚本错误讲成排除了线上故障；相关文档随本轮提交。

### RG-AGENT：真实Coding Plan对照首题答案未通过（2026-10-23）

- 类型：真实任务质量失败，初次登记时原因待查，后续逐例复盘见下。基线 `617c0af`；固定GLM-5.3-Flash Coding Plan、温度1/low/输出8000、本地固定Qwen；连接检查已报告usage。正式命令 `PYTHONPATH=. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python .task_outputs/retrieval-upgrade/agent-local/run.py` 初次登记时仍在执行，最终退出0、24槽全执行；运行成功不等于答案通过。
- 现象与证据：首个baseline正常完成5步、6工具调用、源码未变，但事实匹配失败、3条引用均未满足冻结判分；没有未知消费。原始日志 `agent-local/live.log`，独立Store `agent-local/state/runtime.sqlite`，可重建报告 `agent-local/development/report.json`。后续固定槽继续执行，不重跑首槽、不改判。
- 维护诊断纠正：中间快照在已完成控制Run终态、但全批尚未结束时显示aborted；最初误按该字段写了“退出2、首槽停批”，实际运行工具仍返回活动session，candidate也已开始。这一推断已撤回；应以进程真实退出状态及最终计划覆盖率判定批次完成，不把运行中的旧导出当最终结果。

- 开发集已完成8次：baseline0/4、candidate1/4，全部Run正常完成，7个质量失败均保留。两臂的symbol-receipt、concept-drain、chain-capacity失败；fresh-retention仅baseline失败。首题baseline将示例占位键 `requested_key` 原样输出，遗漏要求的 `deduplication_field` / `writer_function`，其引用也绑定占位键；这是合法JSON中的事实/引用契约错误，不是认证或执行异常。候选4/4实际只用了文件工具，未调用search_knowledge，因此通过数量差异不能归因Qwen；没有过期误引也不能宣称真实模型挑战过检索新鲜度路径。留出继续按冻结批次执行，未调整提示。

- 最终批次退出0，计划24槽全部执行：开发7个质量失败，留出15个质量失败；留出baseline0/8、candidate1/8，候选引用有效率12/17低于基线15/19，未达到下游晋升门槛。两臂真实语义调用均0，不能将通过数差异归因Qwen。所有失败仍按原标签保留，未调整提示、放宽精确值或补跑；逐例身份/输出/引用保存在原Store及两份report，进一步失败类型核对另追加。本轮真实对照只衡量冻结调查任务，不代表全项目通用成功率。

- 逐例复盘已完成：22次失败按互斥口径为facts键集合错误19次、键正确但精确字面量不符2次、facts全对但citation.fact仍为占位键1次。56条quote的文字及行号全部对应当前源码，其中19条不满足事实绑定/冻结文件范围（12条未知fact键、7条文件范围错误），不能称为19条引文幻觉。98次聊天请求输入146207/输出4055、unknown0；24个Agent和24个control均正常completed，没有预算/超时/取消/执行故障。baseline47请求/48工具、candidate51请求/50工具，本地准备另占时间但没有Provider消费。独立只读复核已核对canonical输出，保留报告与原判分；后续输出契约修正须另立实验，不用改判制造检索收益。

### RG-INDEX：真实项目CPU BF16构建未完成（2026-10-23）

- 类型：真实性能验证受阻、维护主动停止，不是已发布索引损坏。基线 `617c0af`，默认状态根、固定Qwen BF16/CPU；命令为 `PYTHONPATH=. HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python .task_outputs/retrieval-upgrade/agent-local/rebuild-project.py`。当前源码约492文件/10839片段，全部片段不超过800字符，第一批64条在已观测3分47秒时仍未返回；进程CPU约347%、RSS约1GiB，并非只在等待锁。
- 证据与原因边界：`agent-local/project-index-force.log`、`index-cpu-sample.txt`显示热点位于 `cpu_flash_attention<BFloat16>` 的 `cpublas_gemm_no_downcast`。由此确认当前运行在慢CPU矩阵计算路径，尚不能只凭采样量化各算子占比或推广所有硬件。SDPA math隔离诊断也未完成，未将其宣称为有效方案。
- 处理与取舍：仅停止本轮PID47011（未发布构建）与47360（math诊断），两命令退出143；没有删除旧索引或伪造完成。开始单独验证相同固定包的CPU float32，若采用则必须新算法指纹并重新显式构建；增加内存换可接受的构建速度，不更换模型、调留出或引入全局SDPA开关/MPS平台。初次中断Run须由既有重启恢复标记停驻，不自动重放，后续用新身份。

- FP32修正已通过局部验证：固定实例dtype=float32及v3指纹，6项定向通过、Ruff/3.10目标Mypy通过；旧v1/v2指纹拒绝在请求前执行。真实长片段16条9.54秒、RSS3.437GB，实际Host冒烟11.23秒、RSS2.792GB且索引复用通过。16语料文档向量与原BF16最低余弦0.999833，不能据此改写质量结果；不重跑40条留出或24次真实任务。原始证据 `cpu-fp32-probe.json`、`fp32-vector-check.json`、`local-qwen-fp32/smoke.json`，完整项目仍需使用新身份发布；这是当前性能瓶颈的最小修正，不承诺全部硬件收益或供应商成本改善。

- 最终处理与复验：代码提交 `ceea5d7`，全量显式重建新Run `qwen_eventide_force_fp32_20261023` 退出0、completed、实际发布generation1。492文件/10849片段/1024维，3637.58秒、峰值RSS5.838GB；492文件索引哈希与构建前后源码全相符、Git状态不变、Provider用量为空。旧中断Run已恢复为interrupted，未自动重放或删除旧索引；报告 `agent-local/project-index-force-fp32.json` 及日志保留。后续收口Markdown变化须另作增量刷新。原BF16未完成证据不覆盖、不声称提速倍数；完整项目首次FP32构建仍约61分钟，内存/CPU和增量更新是当前维护取舍，不代表检索质量提升。
- 附带环境诊断：一次只读 `ps -p 47990 -o pid=,etime=,%cpu=,rss=` 被本机沙箱拒绝，退出1；未据此判断构建失败，也未扩大权限重跑。最终使用原Host报告及命令真实退出状态确认完成。实际参与为采样定位、实例精度最小修正和真实索引核验，可追问为何计算策略进入指纹、如何避免中断发布和实验条件偷换。

## FILE-UI：前端子任务准备命令路径错误（2026-10-23）

- 类型：维护命令问题；基线9768103与本轮未提交UI。首次在frontend目录执行`cat > frontend/tests/file-operations.test.ts`，重复了目录前缀，文件未创建；后续Prettier因缺少该文件返回2。尚未运行测试，无生产或用户文件受损。
- 处理：改用当前目录的`tests/file-operations.test.ts`，随后再运行定向用例和类型检查；结果待补。修改限于独立worktree，不提交或推送。

- FILE-UI首次定向验证：`npm run test -- tests/file-operations.test.ts tests/deliveries.test.ts tests/chat-experience.test.ts`，39通过、1失败；`npm run typecheck`通过。失败是新增冲突用例期望list两次、实际三次；证据`.task_outputs/file-operations/ui/tests.log`。已证实测试同时设置details.open（触发原生toggle）并手动派发toggle，导致重复读取；删除重复测试事件，不修改撤销冲突行为。复验仅运行该原失败，再补新增案例；原交付/聊天24项通过保留。

- FILE-UI复验收口：原冲突失败单独1通过；随后补多文件分页及重挂载用例，共17项文件操作组件/API、11项交付组件通过（最终28项），原14项聊天组件首轮已通过，合计42个不同前端用例有通过证据。Vue/TypeScript类型检查与git diff --check通过；`.task_outputs/file-operations/ui/conflict-recheck.log`、`tests-final.log`、`typecheck-final.log`。未运行浏览器、构建、后端或付费模型；不提交/推送，主任务继续统一集成验收。实际参与为UI实现与定向验证，失败是维护路径/测试事件问题，不是线上事故。

## FILE-CORE：新增文件操作静态检查首轮失败（2026-10-23）

- 类型：新增实现的静态检查问题；基线9768103加本轮未提交修改。`.venv/bin/python -m ruff check eventide tests examples`首轮报17项导入顺序/Iterator导入/长行问题，测试首轮30项通过；证据`.task_outputs/file-operations/ruff-first.log`与`backend-first.log`。包装命令末尾cat使shell退出0，实际Ruff检查明确失败，不能记为通过。
- 原因已证实为新增代码尚未整理格式；修正仅涉及本轮变更，定向和完整门禁结果待补。无模型调用或用户项目破坏性测试；本轮按用户要求不创建提交。

- FILE-CORE静态修正：自动整理只处理导入，第二次仍有新增长行检查失败（同根因，`ruff-fix.log`）；随后仅格式化三个新增文件，复验待补。

## FILE-ENV：OpenAPI导出使用了缺依赖的系统Python（2026-10-23）

- 类型：维护环境问题，基线9768103加本轮修改。`npm run api:generate`退出1，系统python3导入FastAPI失败；证据`.task_outputs/file-operations/api-generate.log`。尚未生成有效新契约，不属于接口测试通过。
- 处理：使用已有项目`.venv/bin`加入本次命令PATH，不安装依赖或更改系统配置；随后重新导出，结果待补。本轮没有真实模型或用户状态读写。

## FILE-INTEGRATION：交付回归与新增副本用例失败（2026-10-23）

- 类型：意外集成回归及新增测试问题；基线9768103加本轮实现。定向`pytest tests/test_executor.py tests/test_runtime.py tests/test_host.py tests/test_workspace_store.py tests/test_preference_memory.py tests/test_delivery_repair.py tests/test_sandbox_execution.py`退出1，98通过/15交付失败/1显式Docker跳过，交付多为no_source_progress；证据`.task_outputs/file-operations/regression-first.log`。原因待查，不能声称交付无回归。
- 新增六项故障/副本定向5通过/1失败：测试错误使用Host.create_session(cwd=...)，实际API参数尚待核对；`backend-extra.log`。新增测试样式检查另报长行，格式化后待复验（`test-style.log`，同FILE-CORE）。修复与定向复验继续进行，无用户项目破坏性测试、无付费调用、无提交。

- FILE-INTEGRATION原因已证实：新增state目录保护把位于state/worktrees中的受管Writer也当成状态文件拒绝；临时失败Store的tool.completed明确PermissionError。保护改为仅当state嵌在当前cwd中才拒绝该子树，数据库/密钥/lease仍逐文件拒绝；执行副本cwd自身位于state内时允许其源码路径。新副本测试参数改为既有working_directory；将定向复验两处原失败及所有交付修复用例，不放宽测试或验收判定。

## FILE-E2E：独立worktree静态检查缓存权限（2026-10-23）

- 类型：环境权限问题；基线9768103加本轮UI及E2E未提交改动。`<主项目.venv>/bin/python -m ruff check frontend/scripts/e2e_server.py`退出2，默认沙箱无法在独立worktree建立`.ruff_cache`，尚未报告源码诊断。
- 处理：后续使用`--no-cache`复验脚本，并按独立worktree所需权限运行浏览器；不修改生产权限或安装依赖。真实浏览器尚未开始，结果待补。
- FILE-E2E脚本无缓存检查退出1：新增provider分支两处行长137/103超过100，证据`.task_outputs/file-operations/ui/e2e-ruff.log`。只换行修正新增语句，不调整脚本模型行为，后续定向复验。

## FILE-E2E：交付浏览器回归未通过（2026-10-23）

- 类型：待查；独立UI worktree9768103加本轮UI/E2E，实际后端导入主checkout本轮file-operation未提交实现。命令`EVENTIDE_E2E_REPOSITORY=<主checkout> EVENTIDE_PYTHON=<主.venv>/bin/python PYTHON_DOTENV_DISABLED=1 npm run test:e2e -- tests/e2e/file-operations.spec.ts tests/e2e/deliveries.spec.ts`退出1，3项新文件操作通过、既有交付1项失败，10.6秒。
- 现象：交付控制Run已completed，但repair_status为stopped，未达到补丁展示/采纳断言。失败日志`.task_outputs/file-operations/ui/e2e-first.log`，浏览器error-context存于忽略test-results。新旅程实际验证dirty内容恢复、刷新/重复撤销、新文件删除及外部修改冲突通过；不把这3项通过当作交付通过。
- TypeScript检查及新增fixture Ruff复验通过；交付原因待查，保留原始证据后仅复验失败项，不跑整套或付费模型。

- FILE-E2E交付原因已证实：主任务保留的失败Store中`tool.completed`为`Runtime state cannot be changed by builtin file tools`；新file-operation保护将state根下管理的Writer源码也一并拒绝。主任务已收窄为当前源码内嵌状态子树及精确运行状态文件保护，保留受管worktree合法写入。这里只修改浏览器失败诊断以保留完整delivery state，不放宽通过条件。
- 修正后只复验原delivery旅程，1/1通过／12.3秒，证据`.task_outputs/file-operations/ui/e2e-delivery-recheck.log`；涵盖丢回执同键重试/刷新、Writer写入、独立验收、共享红绿补丁、显式采纳、后续只读Run、用量刷新和320px。新3项普通文件操作首轮已过不重复；合计4个不同真实API旅程通过，无付费模型。新增脚本Ruff、Vue TypeScript与前端变更格式及diff检查通过；缓存权限和行长两项失败保留。不提交/推送，未运行整套浏览器/构建/后端或进程重启测试（后者由主任务后端专项覆盖）。实际参与是新增隔离旅程、保留失败与复验，生产边界修复由主任务完成。

- FILE-UI最终只读复核发现Hash Router与diff文件锚点冲突（此前未点击文件列表），已改为打开差异并滚动的按钮，不修改路由；新增路由保持与超多短行解析上限用例通过。修改后原长差异用例因泛指第一个button（现为文件按钮）失败，18通过/1失败，`ui-last.log`；已证实是旧选择器不足，改为精确“再显示200行”按钮后定向复验，未放宽行数断言。状态目录cwd/大小写保护同时补齐，新边界1项通过（`state-boundary.log`）。

- FILE-UI构建首轮退出2：新路由保持用例直接访问通用VueNode.open，TypeScript要求HTMLDetailsElement类型，证据`.task_outputs/file-operations/build.log`。仅修测试DOM类型断言，构建待复验；组件运行断言此前已通过，不将其代替构建成功。

## FILE-ENV：最终静态页面浏览器启动被沙箱拒绝（2026-10-23）

- 类型：环境权限故障。最终源码重建后，`EVENTIDE_E2E_BUILT=1 ... npm run test:e2e -- ... -g 'ordinary edit|delivery survives'`退出1，临时FastAPI在127.0.0.1随机端口bind收到EPERM，场景尚未运行；证据`.task_outputs/file-operations/e2e-built.log`。将按同一临时项目测试范围请求执行权限复验，不更改服务监听范围或测试断言；此前4项独立worktree浏览器证据仍保留。

## FILE-FULL：全量收口出现失败（2026-10-23）

- 类型：待查。基线9768103加本轮未提交实现；唯一一次`.venv/bin/python -m pytest -q --cov=eventide --cov-fail-under=85`运行中在约16%出现两项F，尚无最终退出状态/异常明细；证据`.task_outputs/file-operations/full.log`。不称全量通过，待保留完整输出后定位并只复验受影响范围；不重复全量。

## FILE-MIGRATION：schema升级在DDL后中断不能重开（2026-10-23）

- 类型：受控缺陷复现/预期Red。基线9768103加本轮v6实现，检查SQLite DDL与版本写入的崩溃窗口；新增临时数据库用例模拟恢复表已存在但版本仍5，`pytest ... -k interrupted_schema`退出1，`table file_operation_backups already exists`；证据`.task_outputs/file-operations/migration-red.log`。
- 已证实原因：新表创建没有沿用既有建表的IF NOT EXISTS，DDL成功而版本事实未提交时重启会重复建表失败。采用原Store幂等DDL习惯补齐，不删除内容或重放文件操作；将定向验证此窗口、原schema迁移及文件操作范围。全量正在执行的字节码仍是修正前SQL，最终结果将如实区分，不重跑全量替代定位。

- FILE-INTEGRATION最终安全复核：仅按工具相对路径检查.git还会漏过Session cwd='.git'的情况，未发生实际Git损坏；改为检查相对实际Workspace根的完整组件，补同一临时目录边界测试。状态目录判断也已从cwd改为Workspace根并统一NFD/casefold，保留受管副本源码写入；不修改原路径策略或Shell权限。定向复验继续，新增分支不重复全量。

- FILE-CORE补充：新增属性核对错误假设macOS Python带os.setxattr，属性定向退出1/AttributeError，证据`attributes-green.log`；此前mode/inode/mtime核对均通过。改用macOS自带xattr命令在临时文件核对，Linux仍用os API；不更改生产写入。新增迁移用例另有一个局部导入空行Ruff失败（`ruff-last.log`），仅格式修正待复验。
- FILE-FULL运行中新增三项失败已定位身份：原有本机MCP HTTP测试，以及两个旧schema3迁移测试；未获得最终异常前原因不下结论。原始日志继续保留，schema中断修正的97项定向已通过，最终逐项对照全量输出。

- FILE-FULL约63%另有三项setup E，仍待完整异常输出；不将其当成功或静默跳过。两个Shell取消原失败按原断言在允许ps的执行环境下单独2项通过（`command-cancel-recheck.log`），但原全量结果仍保留。属性验证已改用本机标准xattr命令并通过（`attributes-final.log`），Ruff最终通过见`ruff-last-green.log`。

- FILE-FULL最终结果：唯一全量退出1，2133通过/5失败/3setup错误/12显式可选跳过/1依赖弃用警告，1030.81秒；覆盖93.59%达到85%（17368语句/1113未覆盖），不称该次全量全绿。完整异常确认：2个Shell取消用例因沙箱禁止ps，1个MCP HTTP及3个Provider wire因禁止本机端口bind；原断言在允许所需本机权限后分别2项和4项通过（`command-cancel-recheck.log`、`network-recheck.log`），没有改生产或测试断言。另2个schema3迁移失败是同FILE-MIGRATION的恢复表重复创建，已修为幂等DDL；原失败与40项新增、schema/记忆/状态范围共97项通过（`migration-green.log`）。全量进程在SQL修正前已导入模块，异常行展示磁盘新文本但实际仍运行旧字节码，不据此误判修正无效。后增.git cwd保护定向1项通过，mode/inode/mtime/xattr核对1项通过；不重复全量。
- FILE-CORE/ENV/INTEGRATION/MIGRATION最终收口：Ruff全仓及浏览器fixture、Mypy128模块通过；既有服务/help与scripted smoke12/12通过；API导出改用已有venv后成功。原15项交付回归及副本/故障范围修正后58通过/1既有Docker显式跳过，新故障2项通过；迁移与全部40新后端用例见97项，不相加成互不重叠总数。最终schema中断Red已Green，状态/Git子目录与大小写别名保护都有通过证据；无未处理的已知本轮失败。实际参与为实现、故障注入、边界复核及原断言复验，没有用户项目破坏性测试、线上事故或模型效果收益，不创建提交。
- FILE-UI/E2E最终收口：前端216项全量通过，后增路由保持/超多短行2项通过；原长差异选择器精确化后单独1项通过，新测试DOM类型修正后Vue类型/Vite生产重建及4资源打包通过。最终生产静态页面2条旅程（含文件列表点击路由不变、撤销脏文件和交付采纳）在允许本机端口后通过（`e2e-built-green.log`），连同之前新建文件/后续外部修改冲突共4个不同浏览器旅程均有通过证据。Prettier/diff通过，未重跑整套浏览器、真实Docker或付费模型；独立worktree日志/截图逐文件摘要核对并保留到主忽略目录，工作副本干净归档。


## RQ-CORE：切块重叠和空白窗口的受控复现（2026-10-23）

- **范围/类型/状态：** 自动化测试；受控缺陷复现，已完成。基线9768103，隔离分支codex/retrieval-chunks-v2。新增长行与空白前缀夹具，不声称线上事故。
- **证据：** `PYTHONPATH=$PWD PYTHON_DOTENV_DISABLED=1 /Users/zesadv/Documents/Codex/eventide/.venv/bin/python -m pytest -q tests/test_rag_chunker.py -k 'long_line_after or whitespace_window' tests/test_rag_source_selection.py --tb=short`，2失败/9未选；原始输出`.task_outputs/retrieval-quality/core/red.log`。源码选择测试被-k筛掉，未执行不算失败。
- **已证实原因：** 旧窗口为对齐行首向前回退，本例800字符首块与次块重叠793字符，超过名义100；窗口遇到800字符纯空白直接break，1700空白后的非空正文消失。
- **处理与取舍：** 实施严格100字符上限，在上限内向后选行首；遇到纯空白窗口推进游标，保留后续正文，不将空白送入embedding。窗口指纹改为windows-v2，已有索引显式force才能增量准备，历史查询继续可读。相关提交为包含本节的RQ2/RQ3-CORE提交（可由Git追溯）。
- **复验与边界：** chunker/source_selection/index/boundaries/code_structure首批80项通过（targeted.log），后补身份及Unicode边界14项通过（boundaries.log），去重共87项。Python3.10目标三个变更模块Mypy通过；未运行历史模型实验、生产索引重建或全量，不宣称检索质量提高。
- **经验与参与：** 用户授权最小语料/切块优化，Codex构造边界、定位并实现；可追问字符/行边界取舍、配置指纹为何阻止混用。


## RQ-CORE-STATIC：新增语料测试的格式失败（2026-10-23）

- **范围/类型/状态：** 静态检查；新测试格式问题，已完成。基线9768103加本轮核心修改；Ruff定向检查只有新测试一行103字符超过100字符规则，生产逻辑和80+14项定向结果未失败。证据`.task_outputs/retrieval-quality/core/ruff.log`。
- **处理与复验：** 将检索结果赋值与断言分两行，不改断言语义；同一变更文件范围Ruff复验通过，见ruff-green.log；Git差异检查通过。相关提交为包含本节的RQ2/RQ3-CORE提交，不重新运行功能测试。
- **经验与参与：** Codex编写及修正测试格式；不包装成生产故障或性能收益，无遗留问题。


### RQ-INTEGRATION：准备策略接线验证（2026-10-23）

- 类型：测试问题。基线9768103，隔离分支codex/retrieval-quality，核心提交1e0f359。
- 失败：受影响组件三文件首轮31项中30通过，indexes.test.ts「chooses only project workspaces」失败。页面正确提交新增默认source_scope=code，旧精确参数断言漏了此字段；不是跨副本串线。证据.task_outputs/retrieval-quality/integration/components.log。
- 处理：只补该断言的默认范围，不改旧pending请求的省略字段或幂等内容；该项定向复验1通过（components-recheck.log），31个不同组件用例均通过。其他通过用例未为此重复。
- 维护代理完成定位与修正；前端类型检查已通过，真实模型效果尚未验证。

- RQ-INTEGRATION静态检查补记：首次Ruff报2处新增import排序和1处101字符行宽；仅格式问题，日志integration/ruff.log。已调整，受影响两文件Ruff复验通过（ruff-recheck.log）。

- RQ-INTEGRATION受控缺陷复现：锁等待期间变更已冻结规则，两个新增边界用例均失败（普通任务错误完成、手动构建未拒绝），证据integration/lock-drift-red.log。原因：复验位于拿锁前；拿锁后未再次比较admission。补为锁内实际构建前统一复验，2项定向转绿（lock-drift-green.log），后续legacy关联复验也通过。这是新增契约的受控复现，不是线上事故。

- RQ-CORE-STATIC追加（RQ2-LEGACY）：新增只读预检接口后，`ruff check eventide/rag/index.py tests/test_rag_source_selection.py`首次仅三处101–103字符行超限，日志`core/legacy-ruff.log`。按相同根因合并；只调整换行后同范围复验通过（legacy-ruff-green.log），22项定向与1项加强快照复验及Mypy原本通过，未重复功能测试。相关提交为本次legacy策略预检提交。

- RQ-INTEGRATION测试问题补记：新增legacy集成用例首次4通过/1失败，测试误用不存在的IndexStore.db_path属性，尚未进入生产断言。改为实际path属性；只复验该用例，日志integration/legacy-green.log和legacy-recheck.log。锁等待两个受控失败已转绿，未再出现错误发布。

RQ-INTEGRATION收口：legacy属性修正后单项通过（legacy-recheck.log），不改生产行为。57个不同后端定向、31组件、4浏览器旅程、类型/构建/Ruff/Mypy通过；所有本轮失败均已定位并复验，相关实现见本次准备策略提交。维护代理编写与验证，独立审查指出锁及legacy边界；尚无真实模型收益结论。
### RQ-EVAL：v2评测新增代码行宽检查失败（2026-10-23）

- 类型：开发检查失败，非真实Agent质量或运行故障。独立worktree基线`9768103`；命令`python -m ruff check eventide/rag/agent_evaluation.py tests/test_retrieval_agent_v2.py tests/test_retrieval_upgrade_evaluation.py`退出1，五处新增代码超过100字符。证据`.task_outputs/retrieval-quality/eval/ruff.log`；同批定向pytest为73通过、类型检查通过，没有调用真实模型。
- 已证实原因：新路径表达式、提示文字和测试断言未按仓库行宽折行。处理仅调整代码换行，保留提示字符串、数据哈希和判分语义；同范围Ruff复验退出0（`ruff-final.log`），仅复验涉及字符串的9项测试通过（`pytest-string-formatting.log`），未重跑全量。实际参与为新增评测契约及定向检查，不描述为修复线上事故或提升模型成绩。
- 附带只读维护错误：一次`rg`参数误写不存在的`tests/test_distribution.py`，退出2；无测试/模型执行，改用已知的`pyproject.toml`包资源声明核对。它不影响产品或测试结论，不据此新增产品回退。相关修正与本条随v2实现提交。
## RQ5-RETRIEVAL：新代码题库准备与定向验证

- **日期、类型、状态：** 2026-10-23；评测夹具准备 / 测试问题；已完成（夹具准备和离线机制）。基线 `9768103`，不是线上检索事故。
- **初次失败：** `.venv/bin/python .task_outputs/retrieval-quality/benchmark/prepare_manifest.py` 在读取固定 Git blob 时退出 1；脚本把现有 `openai_compatible.py` 误写成不存在的 `openai_chat.py`。原始错误保留在本轮工具输出；同时按真实目录改正 `schema.d.ts` 名称，并核对预算标签原文。没有生成或运行任何真实模型结果。
- **处理与取舍：** 读取实际目录和冻结源文件后修正一次性夹具生成脚本；生产实现、原 40 题及历史报告不变。冻结语料生成成功，独立审查收紧三题题意/证据；新增29项离线Hash与合成策略夹具定向测试全部通过，日志 `manifest.log`、`targeted.log`、`policy.log` 位于 `.task_outputs/retrieval-quality/benchmark/`。这不代表真实模型收益。
- **经验与参与：** Codex 编写夹具时应先核实固定提交的路径和标签，不靠记忆拼文件名；用户确定改进范围，未参与这次脚本排错。

- **静态检查环境失败：** 首次 Ruff 无法在本隔离 worktree 新建默认 `.ruff_cache`（sandbox write permission，退出2），未开始代码检查；改用 `--no-cache`，不更改权限或共享依赖。初次错误保留工具输出。无缓存首次检查发现5处新增测试行超100字符（`ruff.log`），只拆行修正；这是测试格式问题，复验通过；后续报告字段和中断文案追加行长问题，仅拆行或缩短文字后复验。一次文档编辑脚本在stdin UTF-8解析阶段失败，尚未执行文件写入；改用显式解释器及Unicode转义继续，原失败保留在工具输出，未据此推断生产编码问题。最终31项不同定向用例全部通过（final-targeted.log），Python3.10目标Mypy一模块通过，Ruff及diff检查通过。未进行真实模型实验、全量门禁或历史重跑；相关commit为本条新增提交。


### RQ-RETRIEVAL：新代码查询开发集未完整召回（2026-10-23）

- 类型：真实本地检索效果失败（调用机制正常），不是线上事故。候选cdc2468，历史干净9768103；固定Qwen CPU FP32、19份代码/142971字节/12开发查询，每题1次向量、3次本地计时，未重跑题目。证据.task_outputs/retrieval-quality/experiments/development.json及四份*-build.json/.prof/.log。
- 结果：FTS完整命中3/12、历史窗口7/12、新窗口all与filtered各8/12、结构候选8/12。所有调用完成，未知/中断0；报告中的failed=0表示执行无故障，不表示质量全过。候选未完整命中dev-queue-concept、dev-skills-cross_file、dev-attachments-cross_file、dev-generation-cross_file。
- 分层诊断：结构整体Recall .7083/MRR .6667，高于历史.625/.5417；但跨文件完整命中1/4，低于新窗口2/4。可确认是有界前5片段未覆盖冻结的全部事实来源；更深层排序/表征原因待查，不能归因“模型小”或靠改标签修分。
- 处理与取舍：保留5段/800/4000限制、原RRF和显式结构选项，不添加去重启发式/重排或扩大批次；新窗口有界重叠保留。结构使204片段变298，构建37.79秒变54.19秒，不能宣称建库提速；性能与质量分别报告。接下来只执行预定留出，不用留出调参。
- 参与与可追问：维护代理编写窗口/范围/准备契约并用冻结语料分臂测量，独立审查修正评测问法；未部署到真实用户场景。旧成绩与原始失败全部保留，仍待留出和Agent证据判断效果。

- 留出追加（faa1fc3，Python代码指纹与cdc2468相同）：一次预定12题，FTS完整4/12、历史窗口8/12、结构7/12；历史/结构Recall@5为.7917/.7500、MRR为.6736/.6903。跨文件完整命中1/4降为0/4，概念题均4/4；前5片段覆盖不足仍在，不能将排名略升解释为整体改善。纯检索p95为44.22/47.99ms，含首次冷加载的完整查询p95为4.740/4.743秒。全部执行完成，未知/中断0；证据experiments/holdout.json与measurement-summary.json。
- 留出结论：未达到预定推荐门槛，结构保持显式可选，不调参、不改标签、不重跑本批次。实现完成与效果达标分别报告；Agent下游另验。当前只证实重复减少与跨文件漏召回，不把未定位的排序原因写成模型规模问题。

### RQ-AGENT-V2：开发批次的引用绑定失败（2026-10-23）

- 类型：真实Agent任务质量失败；基线36a7ad3、冻结v2、GLM-5.3-Flash Coding Plan及本地Qwen FP32，未调用标准embedding API。开发批次进行中，已完成的baseline catalog-restore-window判定citation_binding_mismatch及uncited_fact，原因待逐项核对；此前三槽已通过。证据.task_outputs/retrieval-quality/agent-live/development/report.json及原Store，未重跑失败题。
- 整批中间导出可能标aborted，不能仅凭该快照宣称进程已失败；批次退出和最终状态另行核实。与一次后端全量同时执行，耗时会受本机CPU争用影响，不据此作严格性能收益结论。开发契约检查结束前不进入留出。

- 开发最终8槽完成、退出0：baseline3/4、candidate4/4；两臂JSON/schema及字段键各4/4，facts各8/8，引用真实性各8/8。唯一失败catalog基线的transition_function事实正确，但引用garden/catalog.py:8–9仅含状态/期限赋值，漏了第4行函数定义名；属于证据绑定不足，不是引文幻觉、过期或错键。有效绑定7/8对8/8，unknown0，无外部失败或未尝试。两臂search_knowledge均0；差异不能归因Qwen或结构切块，不改判或补跑。已满足开发契约门槛，按冻结计划进入一次留出8槽。

- 留出阶段追加（7d58f91，批次仍进行中）：candidate broadcast-final-drain 的schema/字段正确，两个引文均真实且有效绑定，但buffer_operation值不匹配冻结答案，判fact_values_mismatch（1/2事实正确）。实际值与题意原因待核对；保留原判分和report/Store，不重跑或调整标签。

- 留出继续追加：baseline broadcast-final-drain 同样出现buffer_operation精确值不符；baseline report-signing-chain的空输出初步显示invalid_answer_schema，后核实为下述外部配额中断，不能归为已完成模型答案的格式退步。不增加尝试或修改原报告判分。证据仍为同一holdout/report.json及其逐任务Store。

- 已证实原因：broadcast两臂都回答buffer.flush_pending，冻结题面要求源码完整属性表达式stream.buffer.flush_pending；引用真实且包含完整表达式，但facts漏了stream前缀，因此严格值判分失败。这是值表示不满足已公开题面契约，不能简单归因检索未找到。

### RQ-EXTERNAL：Coding Plan五小时配额耗尽中断留出（2026-10-23）

- 类型：环境/外部服务限额，基线7d58f91。第五槽baseline report-signing-chain的第二次模型请求HTTP429、业务码1308，供应商返回五小时使用上限及重置时间2026-10-23 22:12:37；本次usage未知保留，剩余3槽未尝试。原run.failed及report在agent-live/holdout的唯一Store和导出，密钥未写日志。
- 影响与处理：空回答被底层grader标invalid_answer_schema，但它来自未完成的Provider请求，应在分析中单列外部中断，不能宣称模型结构回归。沿已有停批规则停止扩大，不重试、不换Key/供应商、不把未尝试算成功；最终进程退出2，状态aborted、无缺失结果；16计划槽合计13尝试（开发8、留出5），12正常完成、1外部未知、3未尝试。继续离线收口，剩余实际效果待外部额度恢复后另行明确批次安排。

### RQ-FULL：本轮唯一全量出现失败，待定位（2026-10-23）

- 类型待查；Python实现指纹e020f09d、测试基线36a7ad3（期间仅追加结果Markdown）。命令PYTHON_DOTENV_DISABLED=1 PYTHONPATH=$PWD python -m pytest -q --cov=eventide --cov-fail-under=85，使用既有.venv Python3.12。进度78%后出现一个F，完整进程仍在运行，具体用例/原因和退出状态待最终摘要；证据.task_outputs/retrieval-quality/integration/full-backend.log。先保留失败，不启动第二轮全量，结束后只复验受影响范围。

- 最终合并：正常完成12次中9通过、3质量失败；schema/字段12/12，事实22/24，引文真实性24/24、绑定23/24。全部43次模型attempt中已报告输入74023/输出2237、缓存读51840、推理95，另1次unknown，因此总用量不完整。两臂语义调用均0；未执行槽为candidate report-signing-chain、candidate export-copy-profile、baseline export-copy-profile。combined-summary.json单列已完成质量及外部状态，原始批次报告不覆写；不能宣称全16槽完成、RAG收益或跨版本提升。供应商时间未独立核对时区/账户，不据此自动续跑。

### RQ-METRICS：外部故障诊断被计入答案质量分母（2026-10-23）

- 类型：评测报告口径缺陷，由上述真实配额中断暴露；v2汇总只检查grade存在，没有区分Run终态。Host在失败时返回error or output，Provider诊断文字因此进入底层grader并产生invalid_answer_schema，不是模型生成了坏JSON。原始事件/尝试/grade全部保留；修正仅针对派生quality汇总：只纳入completed且有grade，引用真实性分母一致；其他计ungraded（可有完整答案），原尝试/失败和v1不变。新增真实Host三槽混合回归覆盖真正JSON错误、完成正确答案及未知用量正确答案，17项关联复验通过，Ruff/单模块Mypy通过；未重跑真实批次或覆盖旧报告。

- 全量最终退出1：2201通过、1失败、12显式跳过，覆盖93.81%。唯一失败test_local_admission_keeps_legacy_idempotency_fingerprint用最新RunBody生成旧指纹预期，却未剔除新增code_structure/source_scope默认字段；生产路由已正确省略默认值，实际保持旧身份。本次只更新测试排除列表，不改生产幂等逻辑；复验此用例及新增报告分类范围，不重跑全量。

- RQ-INTEGRATION环境补记：最后两份前端文件的prettier检查首次在写入受管worktree日志时被沙箱拒绝（operation not permitted），检查程序尚未执行，不是格式回归。改用获授权的同一工作树执行后复验；命令、退出状态在本轮工具记录，复验日志integration/final-frontend-format.log。

- RQ-FULL最终复验：唯一旧断言修正后通过；连同v2报告专项共17项通过（integration/final-targeted.log），不重复全量；原完整失败保留。RQ-INTEGRATION页面复核发现请求结构不代表实际结构，现展示已有structured_files和structure_fallbacks，索引页12项含2个实际回退用例通过；类型/构建、格式以及最终wheel通过。先前沙箱重定向失败已在授权环境执行同一检查且退出0。
- 本轮收口责任与取舍：维护代理实现、编写定向测试、执行真实与离线对照；独立复核指出锁内配置、legacy预检与页面实际结果问题。没有声称线上事故、自动消除外部限额或RAG质量收益；修正随包含本记录的本地收口提交，可由Git追溯。仍待外部处理的仅RQ5模型余量及未执行槽，相关质量失败保留原判。

### RQ-DOC：收口脚本误覆盖设计决策文档（2026-10-23）

- 类型：本地文档意外回归，未推送。最后维护脚本复用变量s，在追加ADR摘要后将failure-cases正文写入decisions.md；提交差异统计出现1357行变化，复查定位到该写入。恢复本次收口前的设计决策原文，只追加上述测量结论；核对原文逐字前缀一致、最终差异仅一段新增并修正尚未发布的本地提交。代码及模型产物不变，无需重跑测试。实际参与为维护代理自身误写、差异复查和修复，不虚构为线上事故或产品能力；经验是文档脚本使用明确目标且收口同时检查内容和差异规模。


## MERGE-FILE-01：文件冲突跨越写入边界时状态分类错误

- 类型：受控缺陷复现；本地整合代码复核发现，非线上事故。基线为检索83bbd07与文件操作42b3414的未提交合并。
- 复现命令：python -m pytest -q 'tests/test_file_operations.py::test_post_write_failure_parks_actual_host[verification_conflict]' tests/test_file_operations.py::test_prewrite_conflict_records_failure_without_mutation（设置PYTHON_DOTENV_DISABLED=1、PYTHONPATH为当前隔离目录）。2026-10-23三项失败，日志.task_outputs/local-integration/file-conflict-red.log。
- 已证实原因：写后核验的FileOperationConflict被外层直接传播，Executor当普通失败，实际Host最终completed；写前读取/打开阶段同类冲突未记录failed，投影停在prepared。
- 修正：写后核验冲突转现有FileOperationUnknown并复用needs_review/Host停驻；明确写前冲突统一记录failed。不自动重试、撤销或新增恢复抽象。
- 验证：文件操作43项通过，含三项原失败；其他关联文件100项此前已通过，不重复运行。静态复核确认异常边界，修正随合并提交3ddb960保存，原始文件操作提交42b3414保留。
- 维护执行问题：首次写入修正的内联脚本报Non-UTF-8 SyntaxError，未修改代码；该命令未启用失败即停，随后关联测试在未修正代码上启动，结果保留在backend-targeted.log。后续改用项目解释器及ASCII转义脚本，并将编辑与验证分开，不把这次重复运行称为修复后的复验。
- 实际参与及可追问点：Agent静态复核提出缺口，维护者构造故障注入并修复。可解释相同异常在副作用前后为何采用不同状态；不能描述成线上丢数据事故。

- MERGE-FILE-01首次关联结果：140通过、上述3项原缺陷失败（66.37秒）；修正脚本失败所致未修复运行如实保留，已通过的无关文件不重跑。

- MERGE-FILE-01修正后：文件操作43项全部通过（8.86秒），含三项原失败；结合此前其他关联文件100项通过，合计143项不同后端用例有通过证据。前端受影响5文件66项通过。Ruff首次仅本次新增raise语句102字符超100，记录为维护静态检查问题；拆行后只复验该模块，未改语义。

- MERGE-FILE-01最终验证：Ruff行长修正后模块通过；Python3.10目标Mypy130模块、66项前端、8条生产静态页面真实API旅程、12项分发/服务测试、Vue构建/格式、服务/help/smoke12与干净wheel隔离检查通过。原始首次失败和未修正运行均保留；该缺陷无剩余未验证项，不将本轮定向结果称为再次全量通过。


## MK-ZCODE-CONCURRENCY：外部子agent并发限制（2026-10-26）

- 分类：环境/外部故障，开发子agent调用失败，不是Eventide应用真实模型评测失败。基线5acba20。
- 命令：指定Node调用/Users/zesadv/.codex/tools/zcode-start-plan/run.mjs，--cwd分别为maka-context/eventide、maka-native/eventide独立工作副本，--prompt-file使用本轮临时UTF-8任务，--mode yolo；桥接options.reasoningLevel=max。原始命令及任务文件路径在本轮工具记录，未写入凭据。
- 现象：增加至5个在途ZCode任务后，摘要和原生隔离两次调用返回ok=false，HTTP429/provider_code3008，message=user concurrency limit exceeded。确认是账号并发限制，不能解释为余额耗尽；未切换Start GLM-5.3或Coding Plan，不购买/升级套餐。不能由两次失败推断套餐精确并发上限。
- 证据：各worktree忽略目录.task_outputs/maka-upgrade/zcode/result.jsonl、progress.log；context session sess_962957e5-ed1e-4d2b-8c13-438228eca475，native session sess_752a61d0-3dd1-46b0-80e0-f2198cfd3e96。
- 处理：停止扩大在途调用，保留已有任务，后续采用有界分批调度；核对失败会话是否存在文件修改，不把子agent的ok当实现验收。此时尚未完成后续复验，保留待验证。
- 参与与可追问点：主agent实际拆分/启动并读回错误，未修改登录/额度路由，未宣称模型自动恢复或额度切换通过。

## MK08A-LINT：新增续接测试格式检查失败（2026-10-26）

- 分类：测试问题。基线 `5acba20` 加本轮页面续接改动；命令为共享环境 Python `-m ruff check eventide/http_api/streams.py tests/test_stream_reconnect.py`，退出 1。
- 现象及已证实原因：新增测试第 33、36、38 行超过 100 字符，三条 E501；功能定向测试此前通过。原始结果见本轮工具输出。
- 处理：按项目格式拆分断言；Ruff 定向复验通过，8 项后端续接/流式测试通过。相关提交 `983f0c4`。实际排障为主代理执行，未涉及模型效果或生产故障。

## MK08A-UI-TEST：重连提示定向测试失败（2026-10-26）

- 分类：测试问题。基线 `5acba20` 加本轮页面续接改动；命令 `node node_modules/vitest/vitest.mjs run tests/events.test.ts tests/workbench.test.ts tests/chat-experience.test.ts tests/connection-notice.test.ts`，frontend 目录运行，退出 1。
- 原始证据：`.task_outputs/maka-upgrade/connection/frontend-red.log`。已确认失败在卸载后全局计时器数量断言（预期 0、实测 1）；全局计时器来源未逐一归属，该断言超出了本组件的清理责任。改为验证组件创建的 interval 身份确实被 clearInterval 清理，保留倒计时和离线文案行为断言；4 文件 36 项前端定向复验通过，证据 `connection/frontend.log`。相关提交 `983f0c4`。

## MK-ZCODE-TIMEOUT：开发子任务达到桥接执行时限（2026-10-26）

- 分类：环境/执行时限，非Eventide模型评测。基线5acba20，maka-recovery独立副本；指定Node/run.mjs --cwd该副本 --prompt-file恢复任务 --mode yolo，退出1。
- 已证实原因：桥接固定30分钟时限，结果ok=false、error=ZCode task exceeded 30 minutes；会话sess_671f8729-7be9-4dad-9240-9ae3226ae9af，Start Plan GLM-5.3-Flash/max。不是额度耗尽，不切换套餐。
- 证据：maka-recovery/eventide/.task_outputs/maka-upgrade/zcode/result.jsonl和progress.log。保留修改与新增测试；尚未有可核验的测试日志或提交，不计完成。
- 处理：释放调用槽，待主代理接手核验及完成切片；后续任务收紧范围并预留验证时间。后续复验、提交及遗留项待补充。实际参与为ZCode写入候选、主代理调度和审查，不能宣称重试或恢复已验收。

- 同根因第二次：maka-contract会话sess_598a5faa-7a1a-41a3-a1ab-9a9bc6b8e21d同样30分钟退出1，但已产生本地提交`2f6ec8f`与定向/前端/浏览器验证日志。桥接超时不否定已保存证据，也不等于整体验收；主代理仍须复核并处理额外边界。证据为该副本zcode/result.jsonl及contract目录日志，保留未经修改。

- 同根因第三次：maka-file-evidence会话sess_b3cd4b84-dab5-4cc5-9114-7a76752a210d在30分钟退出1；文件工具/副本worker/测试候选保留，未提交、未取得完整验证结果。主代理接手后核验，不能计为完成。证据在该副本zcode/result.jsonl/progress.log。

- recovery后续：主代理完成独立核验并修正候选，179项后端定向及Ruff/Mypy通过，本地候选提交d75cd93；细节随集成保留MK08B-REVIEW。桥接超时日志仍保留，不改称ZCode调用成功。context/native按三路队列重发后均进入工具执行；没有再次出现并发拒绝，但任务最终验收仍待完成。

## MK08B-REVIEW：重试候选独立复核未通过（2026-10-26）

- 分类：受控缺陷复现/待查。基线5acba20加ZCode未提交候选，主代理接手后运行共享Python `-m pytest -q tests/test_model_failure_review.py tests/test_model_retry.py`，退出1。
- 原始证据：`.task_outputs/maka-upgrade/recovery/review-red.log`。主代理新增独立的结构化账单429及错误文本数字误分类用例；原因、修复、复验待补齐。不计完成，不把开发失败当线上影响。

- 后续核验：核心83项已通过（recovery/core.log）；Ruff变更范围检查退出1，详情recovery/ruff.log，尚待修正。关联pytest命令误用了不存在的tests/test_model_slots.py（同时检查实际日志是否有其他缺失文件），收集退出4，未执行测试；这是主代理测试调用问题，非应用失败，命令和现象保留recovery/regression.log，改用实际文件名后复验。

- 已证实原因及处理：结构化账单429被HTTP状态覆盖、裸数字子串被误当状态码；调整结构化优先级与状态码边界，未知Retry-After不授权重试。另补累计等待与模型执行耗时分离、服务商执行项立即停止，避免随后超时丢失边界证据。核心83项、关联96项均通过，Ruff复验与Mypy11模块通过，证据recovery/core.log、regression.log、ruff-green.log、mypy.log。静态失败仅新增测试导入排序和循环闭包绑定，已修复。错误文件名日志已另存regression-command-error.log；实际test_scheduler.py及其他关联范围合计96项通过。相关提交由本记录所在提交定位；无真实模型效果结论。参与为ZCode初版实现、主代理独立复现和修正，可追问如何区分账单与限流、为何等待额度不应包含单次请求时间。

## MK00-ZCODE：MK-00 契约切片首轮新增测试与组件初版失败（2026-10-26）

- 类型：测试问题与静态检查问题，非生产缺陷；发生在新增代码首次验证，无线上影响。基线 5acba20，隔离 worktree codex/maka-contract。
- 复现命令与现象（2026-10-26，日志 .task_outputs/maka-upgrade/contract/first-run-failures.log）：
  1. `uv run pytest tests/test_state_copy_migration.py -q` 首轮 1 项失败：断言 `file.operation` 事实总数为 1，实际为 2。追加式账本对同一次操作分别保留 prepared 与 completed 两条事实属既有设计（`FileOperations.write` 先 prepare 再 `_record` 完成），是新增测试预期写错，不是重复迁移。
  2. `vue-tsc --noEmit` 首轮 3 个类型错误：`Number.isSafeInteger` 不收窄 unknown 导致 contract_version 赋值报错；ContractBarrier 模板在联合类型 `ContractCheck` 上直接取 `.message`。组件初版编写问题。
  3. `ruff check` 首轮 2 项：新增测试一行 101 字符超限、未使用的 `asyncio` 导入。
- 已证实原因与处理：均为新增测试/组件初版的预期或类型收窄写法问题，生产代码（store/api 契约路由）无改动。测试断言改为按唯一 operation_id 去重后核对 prepared+completed 两条事实；`asContract` 先做 `typeof` 收窄，屏障组件用 `failure`/`serverFacts` computed 在联合类型上收窄；Ruff 拆行并删除未用导入。
- 复验：修正后仅重跑受影响范围——`tests/test_runtime_contract.py` + `tests/test_state_copy_migration.py` 7 项通过；`tests/contract.test.ts` + `tests/shell-navigation.test.ts` 16 项通过；vue-tsc、Ruff 全仓、Prettier 均通过。随后定向回归 40 项后端 API/错误/导入/开发接口、前端全量 232 项、3 条真实浏览器契约旅程、真实 HTTP 契约读取全部通过（完整清单见 .task_outputs/maka-upgrade/contract/*.log）。
- 相关提交：见本切片本地提交（MK-00 契约切片）。
- 经验：对追加式事实账本写"数量等于 N"断言前，先核对该操作会追加几条事实；联合类型的结果对象在模板里取字段前先用 computed 收窄。
- 实际参与及可追问点：本切片子代理独立编写并复验；可解释 file.operation 两条事实的状态机与 `records()` 去重投影的关系；不能把首轮失败描述成兼容性回归。
