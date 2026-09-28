# FastLab 文档索引

文档中的“必须”表示发布或兼容性要求，“建议”表示默认运维实践。代码和文档发生差异
时，先把差异视为缺陷；不要在没有契约测试和迁移说明的情况下让实现静默改变。
根目录 [`plan.md`](../plan.md) 和本地 `tips.md` 是原样保留的历史计划与设想，可能包含已移除功能，
不作为当前接口和部署依据。当前行为以本索引中的专题文档和代码测试为准；需求中的“计划支持”不等于已经上线。

## 产品与设计

| 文档 | 内容 |
| --- | --- |
| [需求](requirements.md) | 角色、用例、功能不变量、非功能要求与非目标 |
| [架构](architecture.md) | 模块边界、依赖、状态机、并发、锁和数据流 |
| [任务流程](task-workflow.md) | 用户可见的规划、执行、验收和交付流程 |
| [设置中心](settings.md) | 设置页分类、返回行为、响应式布局和表单状态 |
| [仓库与 Git](repositories-and-git.md) | 仓库登记、Worktree、分支和安全交付 |
| [ADR](adr/README.md) | 已接受的关键架构决策 |

## 接口与数据

| 文档 | 内容 |
| --- | --- |
| [HTTP API](api.md) | 路由、认证、请求、状态码和兼容规则 |
| [FastResearch 登录契约](fastresearch-auth.md) | Cookie、`/api/content/me`、同域代理和失效行为 |
| [配置参考](configuration.md) | CLI 参数、环境变量、默认值和校验 |
| [数据模型与迁移](data-model.md) | SQLite、租户目录、文档投影和迁移规则 |

## 开发、部署与运维

| 文档 | 内容 |
| --- | --- |
| [开始使用](getting-started.md) | 本地开发和服务器前置条件 |
| [开发指南](development.md) | 新增 Runner、路由、状态、配置和测试的方法 |
| [部署](deployment.md) | Linux、systemd、Nginx、CLI 固定版本和发布门禁 |
| [运维](operations.md) | 健康检查、容量、告警、任务恢复和故障处理 |
| [安全](security.md) | 威胁模型、直连模式边界、密钥和沙箱要求 |
| [Claude 权限](claude-permissions.md) | Claude 非交互与沙箱策略 |
| [备份恢复](backup-restore.md) | RPO 24 小时、RTO 4 小时的备份与演练 |
| [发布回滚](release-rollback.md) | 发布检查、回滚顺序和数据兼容约束 |
| [常见问题](troubleshooting.md) | 用户和管理员排障入口 |
