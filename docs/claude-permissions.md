# Claude 执行权限

## 默认策略

新版 Claude CLI 下，Planner 和验收 Agent 使用 `dontAsk`：允许的只读和测试命令直接执行，其他命令直接拒绝，不会等待人工审批；Claude Worker 使用 `auto`，由 Claude 自动判断 Worktree 内的本地操作。

服务器模式使用管理员 Worker 镜像中的 Claude CLI，并固定采用现代 `auto` / `dontAsk` 权限模式；本地兼容模式仍会读取本机 `claude --help` 并安全回退。FastLab 在任何模式中都不会使用 `--dangerously-skip-permissions` 或 `bypassPermissions`。

服务器模式首先使用 FastLab Docker 边界，再为 Claude Worker 启用 Claude Code 自带的 Bash 沙箱：

- Bash 命令只能写当前任务 Worktree 和会话临时目录。
- 命令不能自动退出沙箱重试。
- 普通本地构建、测试和格式化命令由 `auto` 模式判断，可在沙箱内无人值守运行。
- 宿主机其他用户目录、FastLab 数据库、Docker socket 和 HOME 不会挂载到容器。

本地兼容模式下，原生 Windows 不支持 Claude Code 的 Bash 系统沙箱；需要系统沙箱时，应在 WSL2 中运行。

## 增加必要命令

如果任务日志明确显示某个必要命令被权限策略拦截，可以在 `.fastlab/fastlab.env` 中追加精确规则：

```ini
FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS=["Bash(npm run *)","Bash(npx vitest *)","Bash(python -m pytest *)"]
```

保存后重启 FastLab。该配置必须是 JSON 字符串数组。

常见规则示例：

```text
Bash(npm run *)
Bash(npx vitest *)
Bash(python -m pytest *)
Bash(python -m unittest *)
Bash(cargo test *)
Bash(go test *)
```

额外规则只应用于写任务，不会扩大 Planner 或验收 Agent 的只读工具范围。FastLab 拒绝裸 `Bash` 和 `Bash(*)`；规则也不能扩大 Docker 挂载边界。

## 仍然被拦截时

先在日志中区分两类问题：

1. **权限规则拒绝**：错误会列出具体命令，并提示 `FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS`；增加精确规则通常可以解决。
2. **系统沙箱拒绝**：命令需要访问网络、Worktree 外目录、Docker、浏览器或系统服务。额外 Bash 规则不会扩大系统沙箱边界，应先判断这项访问是否必要。

依赖安装通常需要网络，也会修改缓存目录。首选在任务开始前由用户安装依赖；不要为了让一次安装通过而开放全部 Bash 或主目录写权限。
