# ADR-0002：可信内部部署默认使用直连 CLI Runner

- 状态：部分被 [ADR-0004](0004-managed-repositories-process-only.md) 取代（2026-09-28）；下文保留历史决策
- 日期：2026-09-25

## 背景

首版希望在 4U8G 单机上简单部署，并直接调用 `codex exec`、`claude -p`。Docker 为每次 Agent
提供更清晰挂载边界，但增加镜像、daemon、权限和运维成本。CLI 自带沙箱可以降低误操作风险，
却不等同于不可信租户隔离。

## 决策

`FASTLAB_EXECUTION_BACKEND=process` 为默认，Docker Runner 作为可选后端保留。Process Runner
必须使用租户/执行器独立 HOME 和 runtime、白名单环境、租户登记仓库或当前任务 Worktree
cwd、独立进程组和严格
CLI 沙箱；依赖或探针失败时阻止启动，禁止无沙箱回退。

该决定只适用于约 50 名可信内部成员。不完全可信用户、越界 sentinel 失败或监管强隔离要求
必须切换 Docker/VM。

## 后果

- 首版无需 Docker，安装和资源开销较小。
- 必须固定 CLI 版本并持续运行 Linux 实机隔离门禁。
- 同一 Unix 服务账号和 CLI 文件工具仍构成剩余风险，不能宣传为强租户隔离。
- ExecutionRunner 契约允许后续不改变业务流程地切换容器或 VM。
