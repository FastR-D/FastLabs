# FastLab 实施进度

最近核对：2026-09-28。范围仅限 FastLab，不修改 FastResearch、FastNews。

## 当前目标

可信内部成员使用可复用 Agent 定义完成仓库内代码和文档任务。FastLab 统一组织
规划、确认、执行、验收和交付，保留 Codex 与 Claude，使用 Linux process Runner。

## 本轮交付与验证

| 能力 | 代码与验证入口 | 状态 |
| --- | --- | --- |
| Agent 创建、编辑、复制、归档及版本快照 | `agent_library.py`；`test_agent_library.py` | 已实现，已通过自动化验证 |
| 选择已有定义、生成及编辑任务内临时定义、另存到库 | Planner、任务分配 API、Agent 库页面 | 已实现，真实 HTTP + 假 CLI 浏览器流程通过 |
| 确认后执行；任务独立会话、原会话续接 | `runtime.py`、应用任务流程 | 已实现，端到端及回归测试通过 |
| 每次调用的配置、状态、会话与运行历史 | `agent_runs`、任务详情 | 已实现，异常和重启恢复测试通过 |
| 双执行器、原有 Git 交付、超时与 Dispatch 保护 | 现有适配器、Runner、调度与验收 | 保留并运行回归测试 |
| 真实 Codex/Claude 模型与用户登录态 | 服务器集成验证 | 待部署环境验证 |
| Linux 多用户沙箱与资源边界 | `tests/test_linux_sandbox.py` | 本机为 macOS，待 Linux 实机验证 |
| agent-git、跨任务记忆、动态递归派发 | 后续设计 | 本阶段不实现 |

本轮验证：Python 共 87 项，85 项通过、2 项 Linux 专属检查在 macOS 跳过；
前端单元测试 26 项通过；浏览器端到端测试 5 项通过；部署 smoke test、Ruff 正确性检查、
Python 编译与 `git diff --check` 通过。HTTP 测试已在允许本机监听的环境完整执行。
假 CLI 测试不代表真实模型和生产隔离已验证。

## 后续发布顺序

1. 备份服务数据、租户数据库和凭证主密钥；固定 Codex/Claude 版本。
2. 在 Linux 测试机通过跨租户文件、凭证、进程取消和并发门禁。
3. 使用每个测试用户的独立 runtime 登录，验证双执行器规划、执行、续接和验收。
4. 通过后再部署；失败时停止服务并恢复匹配备份，不自动重复执行旧运行。

## 文档职责

- 产品语义：[需求](docs/requirements.md)、[Agent 与协作](docs/agents.md)。
- 实现边界：[架构](docs/architecture.md)、[API](docs/api.md)、[数据模型](docs/data-model.md)。
- 架构取舍：[ADR](docs/adr/README.md)。
- 旧设置功能进度：[历史记录](docs/archive/2026-09-28-settings-progress.md)，不作为当前规范。
