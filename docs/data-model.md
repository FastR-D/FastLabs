# 数据模型与迁移规则

## 磁盘布局

生产建议布局：

```text
/srv/fastlab/data/
└── fastlab-v2.db                 全局租户与加密凭证注册表

/srv/fastlab/tenants/<sha256(accountId)>/
├── fastlab.db                    当前租户任务数据库
├── repositories/                 FastLab 托管的用户仓库
│   └── <repository-id>/          系统生成目录；用户不输入物理路径
├── skills/                       用户级 Skill 副本
│   └── orchestration/SKILL.md
├── tasks/<task-id>/
│   ├── task.md
│   ├── progress.md
│   └── acceptance.md
├── worktrees/<task-id>/          integration 与 sub-NN Worktree
└── runtime/                      Codex/Claude HOME、缓存和会话状态
```

租户键是稳定 `accountId` 的 SHA-256 十六进制值，不包含显示名。目录和数据库权限为 `0700`
和 `0600`。不得把 runtime、仓库或 Worktree 共享给另一个租户。

`local` 模式默认直接使用 `<data-dir>/fastlab.db`、`repositories/`、`tasks/` 和 `worktrees/`；
仓库同样按 ID 托管。旧本地数据库不会自动导入多用户注册数据库；外部路径记录不再可用，
管理员需要另行迁移仓库，不能通过更改路径配置继续使用。数据库结构迁移仍在启动时执行。

## 全局注册数据库

`fastlab-v2.db` 使用 SQLite WAL、外键和固定 schema version `2`：

- `metadata(key, value)`：包含 `schema_version`。
- `tenants(account_id, tenant_key, person, created_at, updated_at)`：租户映射。
- `credentials(account_id, provider, ciphertext, preview, updated_at)`：Fernet 密文与掩码。

数据库已有表但缺少 FastLab v2 metadata，或 schema version 不受支持时，启动必须失败，不能
猜测或覆盖。

## 租户任务数据库

每个 `fastlab.db` 保持现有表名与字段语义：

- `repositories`：仓库 ID、别名、服务端内部路径、默认标记和时间戳；多用户页面不接收或显示路径。
- `settings`：JSON 设置，例如角色模型和全局并发的现有投影。
- `tasks`：目标、限制、仓库/子目录、状态、Git 基线、计划、交付和取消信息。
- `subtasks`：计划键、顺序、依赖、执行器快照、模型、会话、Dispatch、分支和结果。
- `events`：任务/子任务事件、消息、受限 JSON payload 和时间戳。

事件 payload 有长度上限，过长内容保存截断标记和预览；所有写入前必须进行密钥脱敏。
外键删除任务时级联删除子任务和事件，但磁盘文档/Worktree/分支由应用层显式清理。

每用户 Planner/Worker/Verifier 默认值保存在租户数据库的 `settings` JSON，不放进全局注册
库。创建任务时把解析后的角色选择、完整 Skill 和 SHA-256 写入任务的 `role_settings_json`
快照，保证后续修改个人设置不会改变旧任务。公开 API 与任务 Markdown 不返回 Skill 全文。

全局注册库另有 `credential_modes(account_id, provider, mode, updated_at)`，`mode` 为 `cli` 或
`api_key`。密钥仍只保存在加密的 `credentials` 表；CLI 模式不保存伪密钥。

## 文档投影

`task.md`、`progress.md` 和 `acceptance.md` 是 SQLite 的确定性人类可读投影：

- `task.md`：目标、限制、计划与运行选择。
- `progress.md`：子任务状态、尝试和事件摘要。
- `acceptance.md`：验收标准、结果和人工证据。

启动时可以从数据库重建投影。不要让 Agent 直接把这些文件当作状态存储，也不要通过手工编辑
文档修改任务。格式变化需要 Golden 测试，确保未改变既有内容语义。

## 兼容与迁移规则

本次设置功能复用现有 `settings` 与 `role_settings_json`，并以 `CREATE TABLE IF NOT EXISTS`
幂等增加 `credential_modes`；用户 Skill 目录在首次启动时从内置模板创建，不覆盖已有文件。

将来需要 schema 变更时：

1. 写 ADR 和数据迁移说明，定义前向/回滚兼容窗口。
2. 先增加旧数据库 fixture 与迁移后 Golden 测试。
3. 在事务中按版本逐步迁移；迁移前创建一致性备份。
4. 不删除旧列、不重写 Git 仓库、不改变租户键。
5. 发布程序必须能识别未知新版本并拒绝启动，防止旧代码写坏新数据。
6. 若回滚代码不能读取新 schema，必须先恢复发布前完整备份。

## SQLite 约束

- 仅支持单主机单服务进程；不能放在 NFS，也不能让多个 FastLab 实例共享数据库和 Worktree。
- 备份必须使用 SQLite backup API 或在服务停止后复制 `.db`、`-wal`、`-shm` 一致集合；只复制
  主 `.db` 可能丢失已提交 WAL 数据。
- 恢复时必须同时恢复匹配的 `FASTLAB_CREDENTIAL_MASTER_KEY`，否则凭证不可解密。
- 数据库和文档不是 Git 仓库内容，不应提交到业务仓库。
