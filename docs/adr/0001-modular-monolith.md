# ADR-0001：采用轻依赖模块化单体

- 状态：部分被 [ADR-0004](0004-managed-repositories-process-only.md) 取代（2026-09-28）；下文保留历史决策
- 日期：2026-09-25

## 背景

FastLab 的 HTTP、Store、Git、调度、Agent 和启动逻辑曾集中在大型 `server.py` 中。系统规模尚
不需要微服务，但单文件让并发、状态和兼容性变更难以审查与测试。现有脚本和测试又依赖根目录
模块的公共符号，直接改包会造成不必要迁移。

## 决策

采用 stdlib/SQLite 为主的模块化单体。实现按配置、contracts、存储、文档、Git、Agent、运行、
规划、调度、验收、租户、集成、Runner、应用、HTTP 和 bootstrap 划分；`FastLab` 保持兼容
门面。根目录 `server.py`、`agent_adapter.py`、`multitenancy.py` 继续 re-export 既有符号。

模块遵循单向依赖，耗时 Agent 在子进程，服务仍以一个 systemd 进程部署。

## 后果

- 可以逐步提取并用契约测试证明无行为变化。
- 新 Runner、认证集成和路由有明确扩展点。
- 暂时存在兼容入口和内部模块两层 API，需要防止逻辑复制。
- 不获得微服务级独立扩缩容；这是当前容量和运维复杂度下的有意取舍。
