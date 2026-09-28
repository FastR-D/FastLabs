# Claude 执行权限

## 生产默认策略

FastLab 以 `claude -p` 非交互模式启动 Claude，不使用
`--dangerously-skip-permissions`、`bypassPermissions` 或自动退出沙箱重试。默认直连 Runner 与
Claude 配置共同强制：

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false
  },
  "permissions": {
    "blockReadsOutsideWorkingDirectories": true
  }
}
```

同时：

- Planner 工作目录固定为当前租户登记仓库，Worker/Verifier 固定为当前任务 Worktree；
- Claude 使用租户独立 `runtime/claude/{home,tmp,config,cache,data}`；
- 只注入当前用户的 Anthropic 凭证，不注入 OpenAI Key、主密钥或 FastResearch 配置；
- 固定禁用自动更新、非必要流量并清理下游子进程环境；
- 项目 MCP、hooks 和非必要插件禁用；
- Linux 缺少 `bubblewrap`/`socat` 或沙箱不可用时，生产启动/运行失败关闭。

Planner 和 Verifier 采用只读工具策略；Worker 只允许完成任务所需的文件操作和精确 Bash 规则。
Claude 的 Bash 沙箱不应被当作覆盖所有文件工具、MCP 和 hook 的完整边界，详见
[安全模型](security.md)。

## 增加必要命令

任务日志明确显示必要命令被工具规则拦截时，管理员可以加入精确规则：

```ini
FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS=["Bash(npm run *)","Bash(npx vitest *)","Bash(python -m pytest *)"]
```

配置是 JSON 字符串数组，重启后生效。裸 `Bash`、`Bash(*)`、含换行规则和危险绕过会被拒绝。
额外规则不会扩大租户路径、系统沙箱或 Provider 环境边界。

常见可评审规则：

```text
Bash(npm run *)
Bash(npx vitest *)
Bash(python -m pytest *)
Bash(python -m unittest *)
Bash(cargo test *)
Bash(go test *)
```

依赖安装会执行第三方脚本、修改缓存并可能需要公网。首选由用户/管理员在任务前按锁文件安装；
不要为了单次安装开放全部 Bash、宿主 HOME 或 Worktree 外目录。

## 排障

1. **工具规则拒绝**：日志列出具体命令；评审后增加最窄精确规则。
2. **系统沙箱拒绝**：访问 Worktree 外目录、系统服务或网络；额外 Bash 规则不应扩大边界。
3. **沙箱不可用**：检查 `bubblewrap`、`socat`、user namespaces 和固定 Claude 版本，不能回退
   到无沙箱。
4. **项目 MCP/hook 被拒绝**：这是生产安全策略，不应为单个任务临时开放。

每次 Claude/OS 升级后重跑 Linux sentinel 门禁。任何跨租户或配置读取成功都必须阻止发布，
修复并重新验证隔离边界；当前没有内置容器后端可供切换。
