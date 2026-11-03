# 架构与运行模型

## 总体结构

FastLab 是单进程、轻依赖的模块化单体。HTTP 控制面、SQLite、Git 编排和 Agent 调度在同一
服务中运行，耗时 CLI 工作位于子进程。根目录 `server.py` 仅负责启动；代码统一从 `fastlab/` 包导入。

```text
启动入口
server.py ──> fastlab/bootstrap.py

fastlab/
├── config.py              配置解析、默认值、环境白名单
├── contracts.py           稳定协议、枚举和 Runner 接口
├── errors.py              可映射到 HTTP/任务状态的领域错误
├── storage.py             SQLite Store 与序列化
├── documents.py           task/progress/acceptance 文档投影
├── git_service.py         非交互 Git 命令边界；应用层编排 Worktree/交付
├── agent_library.py      租户 Agent 定义、版本、快照解析和调用历史
├── agents/                Codex、Claude、Planner、Schema
├── runtime.py             单次 Agent 生命周期和日志
├── planning.py            计划生成、校验与用户编辑
├── scheduling.py          FIFO 并发门；应用层编排依赖和 Dispatch
├── verification.py        验收、人工证据和再验收
├── tenant_registry.py     租户注册、目录和加密凭证
├── tenancy.py             每租户应用实例与 Runner 生命周期
├── integrations/
│   └── fastresearch.py    会话验证
├── runners/
│   └── process.py         直连 CLI 后端
├── application.py         FastLab 应用服务与用例编排
├── http_server.py         路由、认证、JSON 和静态资源
└── bootstrap.py           CLI、启动检查、信号和优雅停机
```

重构允许分阶段落地，但依赖方向必须保持：入口和 HTTP 依赖应用层，应用层组合领域服务，
领域服务依赖 contracts/storage，不反向导入 HTTP 或启动入口。Runner 不修改任务状态，
Store 不启动线程，Git 服务不解析 HTTP。

## 应用接口

`FastLab` 组合 Store、DocumentProjector、GitService、调度和验收服务。
使用 `FastLab(data_dir=...)` 创建应用；仓库由 `provision_repository` 创建，任务按仓库 ID 选择。
根目录不再导出公共符号，脚本必须从相应的 `fastlab.*` 模块导入。

## 任务数据流

```text
HTTP 请求
  -> FastResearchPrincipal / local principal
  -> 租户 FastLab 实例
  -> Store 创建 planning 任务
  -> Planner（全局 FIFO 槽位）
  -> awaiting_approval
  -> 用户确认
  -> GitService 创建 integration/subtask Worktree
  -> Scheduler 按依赖派发 Worker（每次生成 dispatch_id）
  -> 子任务提交并顺序合入 integration
  -> Verifier 在可丢弃 Worktree 验收
  -> GitService 安全同步并快进目标分支
  -> completed / needs_attention / failed / cancelled
```

每次状态更新先写 SQLite，再刷新 `task.md`、`progress.md`、`acceptance.md` 投影并追加事件。
文档用于人读和排障，不是数据库的替代事实源。

## 用户设置解析

```text
管理员策略（CLI 路径、模型允许列表、安全/并发上限）
  -> 当前用户设置（Planner、Worker 默认值、认证来源、Skill）
  -> 创建任务时的不可变快照
  -> Planner 生成的子任务分配
  -> 用户在任务确认/重试/验收页的逐项覆盖
```

管理员策略只能收紧用户可选范围。每用户设置保存在该租户自己的数据边界中；任务开始后使用
任务快照，避免用户稍后修改设置导致历史任务语义漂移。内置 orchestration Skill 是模板，用户
副本保存在 `<tenant-root>/skills/orchestration/SKILL.md`，Planner 只读取当前租户副本。
创建任务时完整 Skill 与 SHA-256 随角色设置固化在租户任务数据库中；公开任务载荷和 Markdown
投影只暴露来源与摘要。

## 设置中心前端结构

设置中心沿用 `/lab/` 的单页应用和现有前端模块，不增加独立 HTML 或服务端页面路由。
`state.js` 保存当前设置分类及进入设置前的页面/任务；`events.js` 处理进入、分类切换和返回；
`render.js` 只为当前分类生成表单 DOM。分类到数据边界的映射如下：

```text
全局设置入口
  -> 设置中心（← 返回来源页）
       ├── Agent 配置  -> executor settings + executor health
       ├── 模型凭证    -> credential modes / masked keys
       ├── Skill 配置  -> tenant orchestration Skill
       └── 仓库管理    -> tenant repository provisioning / registry
```

桌面端分类栏和内容区使用两列布局；窄屏仅改变导航呈现为横向页签，不改变状态或 API。
分类切换和返回都经过统一的未保存变更检查。导航状态不属于服务端用户设置，也不进入任务快照。

## 状态机

主要任务状态：

```text
planning -> awaiting_approval -> running -> verifying -> completed
    |              |              |           |
    +--------------+--------------+-----------+-> needs_attention
                                   \-----------> failed
任一可取消的运行态 ------------------------------> cancelled
```

- `planning`：Planner 尚未成功生成计划。
- `awaiting_approval`：计划可编辑，但不会执行 Worker。
- `running`：至少存在待执行、排队或运行中的子任务。
- `verifying`：集成结果正在最终验收。
- `completed`：验收与交付成功。
- `needs_attention`：可恢复问题，例如冲突、服务重启、交付保护或不明确验收。
- `failed`：明确失败且当前流程不能继续。
- `cancelled`：用户取消并完成进程终止。

子任务至少经历 `pending -> running -> succeeded`，并可进入 `failed`、`blocked` 或
`cancelled`。任何异步结果都必须匹配当前 `dispatch_id`，否则视为晚到结果并丢弃。

## 并发、线程与锁

- `ThreadingHTTPServer` 为每个请求使用线程；HTTP 线程不得同步等待完整 Agent 任务。
- 每个租户拥有一个 `FastLab` 应用实例和租户级并发门；所有租户共享一个全局
  `AgentRunGate`。
- 全局门使用 FIFO 等待队列。生产默认 `global=4`、`per-user=1`；规划、执行、续接、重试和
  验收使用相同槽位。
- SQLite 每次操作使用短连接，启用 WAL 和外键；不要把数据库事务保持到 CLI 或 Git 调用
  完成。
- 应用实例缓存、活动进程表、取消标记和并发门分别由窄粒度锁保护。持锁期间不得调用网络、
  CLI 或等待线程结束。
- 服务只部署一个 Python 进程。增加 systemd 进程数会绕过内存中的全局门并破坏 SQLite/
  Worktree 所有权，因此不受支持。

## ExecutionRunner 契约

Runner 是 CLI 与底层隔离技术之间的适配层。稳定能力包括：

- 启动前 `check()`：验证依赖和严格沙箱可用；失败即阻止生产启动。
- 为一次调用准备命令、`cwd` 和显式环境；`cwd` 只能位于当前租户的登记仓库或任务
  Worktree，并且只能挂载当前执行器 runtime。
- 用稳定运行键登记活动进程，以便取消、超时和停机清理完整进程树。
- 完成后幂等清理运行登记和临时资源。
- 不解析 Agent 输出、不修改数据库、不决定任务状态。

`ProcessCommandRunner` 是唯一后端：独立 HOME、临时目录和 Provider runtime，白名单环境，
独立进程组，CLI 严格沙箱。启动检查使用 `check_dependencies` 参数；生产默认开启。
Runner 接口保留用于测试与后续扩展，当前不包含容器实现。

认证来源属于用户设置而不是 Runner 类型。CLI 模式使用每用户独立的 Codex/Claude HOME；
API Key 模式才注入对应 Provider Key。两种模式都必须经过相同 Runner 沙箱和路径校验。

## 关闭顺序

收到 SIGTERM/SIGINT 后：

1. 停止接收新 HTTP 请求和新 Dispatch。
2. 标记应用正在关闭，唤醒等待队列。
3. 请求终止所有活动 Runner 的完整进程树。
4. 等待后台线程在有界时间内退出。
5. 关闭 HTTP server 和数据库短连接，退出进程。

下次启动会把遗留的 `planning`、`running`、`verifying` 状态改为
`needs_attention`，不会自动重复执行可能已产生副作用的工作。

## 扩展规则

新增 Runner、路由、状态或配置前阅读[开发指南](development.md)。所有扩展必须保持兼容
入口、明确所有权、失败关闭，并为外部行为增加契约测试。

## Agent 定义与执行关联

AgentLibrary 负责定义校验、版本存储、归档、计划引用解析和运行记录；不启动 CLI。
应用层负责选择定义、保存任务快照及确认前分配，Runtime 负责将快照内容注入调用。

```text
Agent 定义 vN → 任务角色 / 子任务配置快照 → 执行实例
                                             ├─ 单次运行 → 新 CLI 会话
                                             └─ 后续运行 → 显式续接原会话
```

每次规划、执行、验收调用都先建立 queued 记录，获取现有并发槽位后进入 running；
完成写入 succeeded/failed/cancelled/timed_out，重启遗留调用标记 interrupted。
实例 ID 使用子任务 ID 或 task-id:planner/verifier；运行 ID 独立生成，不覆盖历史尝试。
CLI 会话 ID 是 Provider 标识，不作为任务或 Agent 定义 ID。日志事件关联对应运行 ID。
历史任务没有定义快照时继续使用原角色与执行器配置，不回填或伪造历史调用。
