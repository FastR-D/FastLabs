# 发布与回滚

## 发布原则

- 应用、Python 依赖和两种 CLI 使用不可变、可追溯版本；不从 `latest` 启动。
- 当前是单实例维护窗口发布，不做多副本滚动升级。
- 本次模块化重构没有数据库迁移；若未来新增迁移，必须遵循数据模型文档并单独评审。
- 发布失败时先保护数据和活动任务，再恢复旧代码；不要混用新旧模块文件。

## 发布前检查

1. 代码审查确认只改 FastLab，兼容入口存在，FastResearch/FastNews 无新增修改。
2. Python 3.10/3.13、Node 22、Ruff、coverage、HTTP、前端和 smoke 全部通过。
3. Linux 严格沙箱 sentinel 测试通过且没有 skip。
4. 记录应用 commit、Python lock/requirements、Codex/Claude 版本、OS 和配置摘要（不含密钥）。
5. 从活动环境文件删除遗留 `FEISHU_*`；未知键必须继续让启动失败，不能放宽白名单。
6. 预发布环境完成真实 CLI 并发 4 的 30 分钟压测。
7. 创建并验证发布前一致性备份。

## 推荐目录

```text
/srv/fastlab/releases/<release-id>/    不可变应用与 venv
/srv/fastlab/current -> releases/<release-id>
/srv/fastlab/config/                   跨版本配置
/srv/fastlab/data/                     跨版本数据
/srv/fastlab/tenants/                  跨版本租户数据
/opt/fastlab-cli/releases/<cli-id>/    固定 CLI
```

systemd 可以把 WorkingDirectory/ExecStart 指向 `current`。发布包准备完成、权限和依赖验证后才在
停机窗口切换符号链接。

## 发布步骤

1. 把新版本解压到全新的 release 目录，创建全新 venv 并安装依赖。
2. 在旁路数据目录运行单元、前端和部署 smoke；验证 root 兼容入口。
3. 停止 FastLab，确认活动 CLI 已清理，创建发布前备份。
4. 切换 `current` 和固定 CLI 路径，执行 `systemctl start fastlab`。
5. 验证服务状态、两个健康端点、FastResearch 登录、仓库/任务读取和一项测试租户任务。
6. 观察至少 30 分钟：5xx、队列、内存、swap、CLI 退出、needs_attention 和残留进程。
7. 保留上一版本和备份，直到观察期及组织保留期结束。

## 回滚触发

- 健康/登录失败、持续 5xx 或控制面阻塞；
- 数据库/文档不兼容、任务状态异常或交付保护退化；
- 沙箱、环境清理、租户路径或密钥隔离失败；
- 取消/超时留下进程树，或资源达到 OOM 阈值；
- 无法在短时间内通过只降并发安全缓解的问题。

## 无 schema 变更时回滚

1. 停止服务并归档故障版本日志、进程和当前数据快照。
2. 把 `current` 与 CLI 路径切回发布前记录的精确版本。
3. 启动服务，检查健康、登录和任务读取。
4. 运行旧版本 smoke，并检查运行中任务已转为 `needs_attention` 而非自动重放。
5. 记录影响范围和需要用户手工处理的任务。

如果新版本已经写入旧版本不理解的数据，即使没有声明迁移也不要盲目启动旧代码；先在备份副本
验证。需要恢复发布前备份时遵循[备份恢复](backup-restore.md)，并明确这会丢失备份后的写入。

## 配置和 CLI 单独回滚

配置变化同样需要版本记录。只回滚 CLI 时仍须重跑该 Provider 的严格沙箱门禁；只回滚配置时
先检查环境白名单和 PUBLIC_URL/path 配对。永远不要用关闭沙箱、切 `local` 模式或提高并发作为
回滚手段。
