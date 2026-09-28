# 运维手册

## 服务检查

```bash
systemctl status fastlab.service
journalctl -u fastlab.service --since "30 minutes ago"
curl --fail --silent http://127.0.0.1:4180/api/health
curl --fail --silent http://127.0.0.1:8787/api/health
```

FastLab 健康响应至少应有 `ok: true`，生产模式应为 `research`，并提供活动租户与 Agent 队列
摘要。健康仅表示控制面可用；不能替代真实 CLI、沙箱、Git 和登录合成检查。

## 容量基线

4U8G 默认：

```ini
FASTLAB_GLOBAL_CONCURRENCY=4
FASTLAB_MAX_CONCURRENCY_PER_USER=1
```

50 名用户可以同时浏览和提交，但最多 4 个 Planner、Worker 或 Verifier 同时运行。等待项进入
全局 FIFO；单用户最多占 1 个运行槽。不要用多个 FastLab 服务进程扩大并发，这会绕过全局门
并让 SQLite/Worktree 出现多所有者。

监控至少包括：

- 进程 RSS、主机 available memory、swap 增量和 OOM 事件；
- 全局活跃/排队 Agent、最长排队时间和各租户占用；
- HTTP p95、5xx、FastResearch 认证失败和健康检查；
- CLI 退出码、超时、取消耗时、残留子进程；
- 磁盘使用、inode、SQLite WAL 大小和 Worktree 数量；
- `needs_attention`、失败验收、合并冲突和交付保护次数。

出现以下任一情况，把全局并发改为 `2` 并重启：内存持续超过 80%、swap 持续增长、OOM、
健康/页面明显阻塞。稳定需要 8 个以上 Agent 时升级主机或引入专用 Worker，不要关闭沙箱。

## 常规操作

配置修改后：

```bash
sudo systemctl restart fastlab.service
sudo systemctl status fastlab.service
```

重启会把未完成运行标记为 `needs_attention`，不会自动重复执行。通知用户检查对应任务并按
状态选择重试、续接或重新验收。

查看精确安装版本：

```bash
sudo -u fastlab /opt/fastlab-cli/bin/codex --version
sudo -u fastlab /opt/fastlab-cli/bin/claude --version
```

版本与发布单不一致时按配置漂移处理，不要让服务账号执行自动升级。

## 优雅停机

```bash
sudo systemctl stop fastlab.service
```

正常顺序是停止新派发、唤醒排队者、终止活动完整进程树、等待后台线程、关闭 HTTP。超过
`TimeoutStopSec` 后 systemd 会清理整个 control group。启动前若仍存在属于 `fastlab` 的旧
Codex/Claude 进程，先保存进程树和日志用于排障，再按事件流程处置；不要盲目杀死其他服务。

## 常见事件处置

### FastResearch 不可达

保持 FastLab 不对外提供未认证降级。检查 FastResearch unit、回环端口和 `/api/health`；恢复后
再重启 FastLab。不要切换到 `FASTLAB_AUTH_MODE=local` 暴露服务。

### Runner 启动检查失败

检查固定 CLI 路径/版本、`bubblewrap`、`socat`、user namespace 和目录权限。严格沙箱不可用时
停止发布；不能改成无沙箱参数。本项目已无容器后端，恢复前必须通过现有隔离门禁。

### 队列增长

先判断是正常长任务、Provider 限流、死进程还是资源压力。每用户 1 个槽可防止单人占满；不要
通过提高到 32 掩盖根因。必要时暂停接收新任务并通知用户。

### 磁盘告警

检查保留的失败/停止 Worktree、日志、runtime 缓存和备份，不要用通配符删除分支或租户目录。
优先从 UI 对确认不再需要的任务执行“清理临时数据”，并按备份保留策略归档。

### SQLite 异常

停止服务，保留 `.db`、`-wal`、`-shm` 和日志副本，运行只读 `PRAGMA integrity_check`。不要在唯一
副本上直接修复。按[备份恢复](backup-restore.md)恢复到旁路目录验证。

### 疑似密钥泄露

停止服务、限制数据目录访问、保全日志，轮换受影响 Provider Key。主密钥轮换当前没有在线
迁移流程；不要直接替换，否则所有密文不可解。按安全事件流程制定重加密或凭证重录方案。

## 定期工作

- 每日：检查备份结果、健康、磁盘、OOM、队列和失败任务。
- 每周：抽查 CLI 版本漂移、残留进程、目录权限和 Nginx 同域行为。
- 每月：审查依赖/CLI 安全更新并在预发布环境重跑沙箱门禁和并发压测。
- 每季度：执行完整恢复演练，验证 RTO 4 小时和主密钥恢复。
