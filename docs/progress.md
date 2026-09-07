# 当前交付状态

更新时间：2026-09-07。

## 已完成

- 惰性配置与客户端初始化；`--help`、单测、离线 eval 无 Key 可运行。
- async-first `AgentRuntime`、provider、tool executor 三个稳定边界。
- Anthropic Messages 与 OpenAI-compatible provider。
- 统一 `ALLOW / ASK / DENY` 权限、CLI/API 审批、超时拒绝。
- 文件工具内生路径校验、agent 名称校验、worktree 基线提交检测。
- 官方 MCP SDK 的 stdio 与 Streamable HTTP 生产路径；真实 stdio 子进程集成测试。
- SQLite session/run/event、JSONL 导出、脱敏和截断。
- scripted provider、10 用例 smoke eval、live eval 开关。
- FastAPI 六个公开接口、SSE 和无 npm Web 控制台。
- `pyproject.toml`、跨平台 `uv.lock`、Ruff、mypy、pytest、coverage、GitHub Actions。
- 中文 README、同步英文 README、架构/面试/简历文档。

## 本机验证

| 检查 | 结果 |
|---|---|
| Python | 3.14.2 / Windows |
| pytest | 68 passed |
| production runtime coverage | 87.49% |
| Ruff | passed |
| mypy | 43 source files, no issues |
| offline eval | 10/10, mean 49.40 ms |
| real MCP | stdio subprocess + local Streamable HTTP passed |
| FastAPI | health/session/run/SSE/error paths passed |

## 尚需用户输入的验收

- 使用用户提供的真实、支持工具调用的国内兼容 API Key，各完成一次 CLI 与 Web 对话。
- 生成该真实运行的脱敏 trace 和 live eval 报告。
- GitHub remote、push、公开发布和 CI 云端矩阵结果需用户明确授权后执行。
- Web 空闲态实机截图已加入 README；CLI/Web 的 live 运行截图与演示 GIF 应在真实模型验收时录制，不用模拟输出冒充 live 结果。

## 指标口径

覆盖率门禁统计 v0.2 生产 Runtime；从教学示例保留的 v0.1 兼容层在 `pyproject.toml` 明示排除。离线工具成功率排除 2 次预期安全拦截，唯一工具错误是用于验证恢复的未知工具注入。
