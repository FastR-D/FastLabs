# 备份与恢复

## 目标

- RPO：24 小时。每天至少一次，并在每次发布前创建一致性备份。
- RTO：4 小时。从发现故障到恢复登录、任务查询和可控重试不超过 4 小时。
- 恢复优先保证数据库、仓库和凭证一致；运行中的 CLI 会话不承诺继续执行。

## 备份范围

必须包含：

- `/srv/fastlab/data/fastlab-v2.db` 及一致 WAL 状态；
- `/srv/fastlab/tenants/` 下每租户 `fastlab.db`、repositories、tasks、worktrees、runtime、skills；
- `/srv/fastlab/config/fastlab.env` 或独立密钥系统中的等价配置；
- 精确应用版本、Python 依赖、Codex/Claude/OS 版本和发布清单；
- Nginx 与 systemd 生效配置。

备份包含源代码、用户仓库和密钥数据，必须加密、限制访问并记录保留/销毁策略。主密钥至少有
一个与数据备份分离控制的恢复副本。

## 一致性备份

发布前使用短维护窗口最容易证明跨 SQLite、Git 和文档一致：

1. 通知用户暂停提交，记录活动/排队任务。
2. `systemctl stop fastlab.service`，确认服务与 CLI 子进程已退出。
3. 把 data、tenants、config 和部署元数据复制到带时间戳的新备份目录；不要覆盖上一个可用备份。
4. 对所有 SQLite 数据库执行 `PRAGMA integrity_check`，记录 `ok`。
5. 生成文件清单和校验和，加密并传到独立备份存储。
6. `systemctl start fastlab.service`，验证两个健康端点和一次登录。

在线日备可以使用 SQLite backup API 分别备份数据库，再对租户文件做快照；如果底层没有原子
文件系统快照，在线备份不能保证数据库与正在变化的 Worktree 完全同刻。恢复后相关运行任务
应保持 `needs_attention`，不得自动重复执行。

只复制 `.db` 而忽略活跃 `-wal` 可能丢失已提交数据。不要在运行中用普通文件复制模拟 SQLite
一致性备份。

## 恢复演练

始终先恢复到旁路、访问受限的空目录，不直接覆盖生产唯一副本：

1. 校验备份清单、签名/校验和与解密权限。
2. 恢复 data、tenants、配置和匹配主密钥，设置目录 `0700`、文件 `0600` 和正确 owner。
3. 在不对外开放的环境对每个 SQLite 文件执行 `PRAGMA integrity_check`。
4. 启动匹配版本 FastLab，验证健康、FastResearch 登录、租户映射、仓库列表、历史任务和凭证
   状态。
5. 用测试租户运行假 CLI smoke；真实 Provider 验证需经过授权，不能泄露生产提示/仓库。
6. 检查 Git Worktree/分支；恢复时正在运行的任务按需要处理，不自动续跑。
7. 切换生产前再创建故障现场归档，保留回退路径。

## 恢复失败处理

- 凭证全部无法解密：优先检查主密钥是否与数据备份匹配，不要重复写入或生成新主密钥。
- SQLite 不完整：保留原始备份，只在副本上尝试恢复；升级为数据事件。
- Git Worktree 元数据路径失效：不要直接编辑数据库；先恢复原路径布局或按产品清理流程处理。
- 代码版本比数据库旧且拒绝 schema：部署匹配版本，不能让旧代码强行打开未知版本。

每季度演练应记录开始/结束时间、实际 RPO/RTO、缺失文件、人工步骤和改进负责人。
