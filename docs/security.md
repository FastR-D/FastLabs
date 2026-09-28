# 安全模型

## 定位与信任假设

默认 `process` Runner 是可信内部团队的轻量首版，不是容器级强租户隔离。用户、业务仓库和
任务提示可能出错，但组织假设成员不会主动利用同一 Unix 服务账号攻击其他租户。

若用户不完全可信、仓库来源不可控、存在监管隔离要求，或 Linux 实机 sentinel 测试出现任何
越界读取，必须停止直连发布并切换 Docker/VM；不能靠增加提示词弥补隔离边界。

OpenAI 建议不能共享数据的用户/工作负载使用独立环境并限制挂载；Claude 文档也明确 Bash
沙箱不天然覆盖所有文件工具、MCP 和 hooks。参考
[OpenAI sandbox security](https://developers.openai.com/api/docs/guides/agents-api/environments/security) 和
[Claude sandbox environments](https://code.claude.com/docs/en/sandbox-environments)。

## 保护资产

- FastResearch 会话、FastLab 主密钥和用户 Provider API Key；
- 其他租户仓库、任务、日志、Worktree 和 CLI runtime；
- FastLab 服务配置、宿主 HOME、系统文件和其他服务；
- Git 目标分支及用户未提交工作；
- 服务可用性、并发槽和 4U8G 主机资源。

## 直连 Runner 必须控制

- `cwd` 必须位于当前租户的 `repositories/`（只读 Planner）或 `worktrees/`（Worker/Verifier），
  解析符号链接后仍不能离开这两个根目录。
- 每租户、每执行器使用独立的 `runtime/{codex|claude}/{home,tmp,config,cache,data}`，权限
  `0700`；不得使用服务进程真实 HOME。
- 子进程环境从固定白名单构造；仅在 API Key 模式注入当前用户、当前 Provider 密钥，CLI
  模式使用该租户的独立登录态；不得继承主密钥、
  FastResearch 配置、另一 Provider 密钥或宿主凭证。
- 每次启动独立进程组；取消、超时、关闭终止完整进程树并幂等清理。
- 项目 MCP、hooks、非必要插件和危险权限绕过禁用。CLI 不得使用
  `--dangerously-skip-permissions` 或等价选项。
- Codex 使用 `workspace-write`、无交互审批和 Linux `bubblewrap`，可写范围仅 Worktree 与当前
  runtime。
- Claude 使用 `-p` 非交互模式，`sandbox.enabled=true`、`sandbox.failIfUnavailable=true`、
  `allowUnsandboxedCommands=false`、`permissions.blockReadsOutsideWorkingDirectories=true`，并设置
  精确工具 allow/deny。Runner 固定禁用自动更新、非必要流量并清理子进程环境。
- Linux 缺少 `bubblewrap`/`socat`、CLI 不支持严格参数或探针失败时，启动失败关闭。

CLI 沙箱不是完整网络防火墙。Provider 调用和任务可能需要出站访问；如项目要求域名级 egress
控制，应在主机防火墙、代理或容器/VM 层实施，并单独验证 DNS、重定向和 localhost 行为。

## 认证与 Web

- 生产只使用 `FASTLAB_AUTH_MODE=research`，除健康检查外每个请求验证 FastResearch 会话。
- 有副作用请求要求同源 `Origin`，Nginx 只在 FastResearch 同一 origin 暴露 `/lab/`。
- FastLab 只监听回环，不配置 CORS，不信任浏览器提交的租户 ID 或磁盘路径。
- 错误响应、事件和 Agent 输出必须脱敏；API 永不返回明文 Provider Key、主密钥或其他租户
  的路径。现有任务契约会显示当前租户自己的仓库/Worktree 路径，不能把它当作安全凭证。

## 密钥与文件权限

- `/srv/fastlab/config/fastlab.env` 为 `root:fastlab 0600`，租户/data/runtime/skills 为
  `fastlab:fastlab 0700`，数据库为 `0600`。
- 主密钥只用于 Fernet 加密凭证，离线备份且与数据备份分开控制访问。
- Provider Key 在一次 CLI 调用需要时才解密，只注入对应 Provider 环境。
- CLI 固定安装在 root 所有、服务账号只读的目录；禁用自动更新。
- 备份视同生产密钥数据，加密、限制访问、记录恢复操作并按期销毁。

## Git 与路径

- `research` 模式不接受用户提供的仓库路径；FastLab 在租户 `repositories/` 下生成不透明目录。
  克隆只接受无内嵌凭证、查询参数或片段的 HTTPS URL，并通过 Git 协议白名单禁用其他协议。
  `local` 兼容路径仍拒绝越界、`..`、文件和符号链接逃逸。
- Agent 可读完整当前仓库，但任务结束前检查工作子目录外改动；越界改动不提交、不交付。
- Worktree/分支清理使用数据库记录的精确名称，不使用通配符。
- 托管仓库永久删除只接受数据库中的精确 ID，并再次验证目录是仓库根的直接非符号链接子目录、
  名称等于 ID；外部 local 路径和活动任务仓库不得删除文件。
- 交付前验证目标干净、分支未切换、历史未改写；新提交在隔离 Worktree 合并并重新验收。

## 发布阻断测试

Linux 实机为两个租户创建不可猜测 sentinel，并准备仅服务进程可读的配置/主密钥测试路径。
让 Codex 和 Claude 的文件工具及 Bash 分别尝试读取：

1. 另一租户 sentinel；
2. `/srv/fastlab/config/fastlab.env`；
3. 服务 HOME、主密钥测试路径和其他 Provider runtime；
4. Worktree 外写文件和启动逃逸子进程。

预期均失败，且取消后无残留子进程。任何内容成功返回、可写或沙箱不可用都阻止发布，记录
CLI/OS 版本并切换 Docker/VM。不能把测试 skip 当作通过。

## 已知剩余风险

- 同一服务账号下的直连 CLI 依赖 CLI 与 bubblewrap 实现正确，隔离强度低于独立容器/VM。
- 模型可能执行仓库内恶意指令、产生破坏性业务代码或把允许读取的数据发给 Provider。
- 依赖安装与构建工具可能执行第三方脚本；固定版本、锁文件和供应链扫描仍由业务项目负责。
- 单机 SQLite 和内存队列存在单点故障，备份不能提供零数据损失。
