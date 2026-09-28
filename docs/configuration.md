# 配置参考

## CLI 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--workspace PATH` | 空 | 旧 local 开发兼容入口；省略时使用 `<data-dir>/repositories/` 托管仓库 |
| `--port PORT` | `FASTLAB_PORT` 或 `4180` | 回环监听端口 |
| `--data-dir PATH` | 仓库 `.fastlab/` | 全局注册数据库和本地数据位置 |
| `--env-file PATH` | `<data-dir>/fastlab.env` | 白名单环境文件；显式给出但不存在时启动失败 |
| `--codex-bin PATH` | 环境或 `codex` | `local` 模式覆盖 Codex 命令 |
| `--claude-bin PATH` | 环境或 `claude` | `local` 模式覆盖 Claude 命令 |

服务固定监听 `127.0.0.1`；公网绑定必须由 Nginx 完成。

## 认证与租户

| 变量 | 默认值 | 要求 |
| --- | --- | --- |
| `FASTLAB_AUTH_MODE` | `local` | 生产必须为 `research`；仅允许这两个值 |
| `FASTRESEARCH_API_URL` | `http://127.0.0.1:8787` | FastResearch 内部 origin，不带尾随路径 |
| `FASTRESEARCH_COOKIE_NAME` | `fr_session` | 浏览器会话 Cookie 名 |
| `FASTRESEARCH_PANEL_URL` | PUBLIC_URL 的 origin | 未登录跳转地址 |
| `FASTLAB_PUBLIC_URL` | 无 | `research` 必填，完整 http(s) URL，例如 `https://host/lab/` |
| `FASTLAB_PUBLIC_PATH` | `/lab` | URL 绝对路径，必须与 PUBLIC_URL 路径一致 |
| `FASTLAB_TENANT_ROOT` | `<data-dir>/tenants` | 租户目录；生产建议 `/srv/fastlab/tenants`，权限 `0700` |
| `FASTLAB_CREDENTIAL_MASTER_KEY` | 无 | `research` 必填，Fernet 兼容 32 字节 URL-safe base64 密钥 |

主密钥不是 Provider Key。丢失或改变主密钥会导致已有用户凭证无法解密，必须与数据备份一起
离线保存。

## 执行后端

| 变量 | 默认值 | 要求 |
| --- | --- | --- |
| `FASTLAB_EXECUTION_BACKEND` | `process` | `process` 或 `docker`；仅管理员可改，重启生效 |
| `FASTLAB_CODEX_BIN` | `codex` | 固定版本 Codex CLI 的绝对路径或 PATH 命令 |
| `FASTLAB_CLAUDE_BIN` | `claude` | 固定版本 Claude Code 的绝对路径或 PATH 命令 |
| `FASTLAB_WORKER_IMAGE` | 空 | 仅 Docker 后端必填，使用不可变 tag/digest，禁止 `latest` |
| `FASTLAB_CONTAINER_CPUS` | `2` | 仅 Docker 后端，每容器 CPU 限制 |
| `FASTLAB_CONTAINER_MEMORY` | `4g` | 仅 Docker 后端，每容器内存限制 |
| `FASTLAB_CONTAINER_PIDS` | `256` | 仅 Docker 后端，PID 限制 |

`process` 后端在 Linux 必须通过 Runner 启动检查：`bubblewrap` 与 `socat` 存在，Codex/Claude
严格沙箱参数可用。检查失败必须阻止生产启动，不能回退到无沙箱命令。

## Agent 策略

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FASTLAB_PLANNER_BACKEND` | `codex` | `codex` 或 `claude` |
| `FASTLAB_CODEX_MODELS` | 空 | 逗号分隔模型白名单；空值保持 CLI 默认行为 |
| `FASTLAB_CLAUDE_MODELS` | `sonnet,opus` | 逗号分隔 Claude 模型白名单 |
| `FASTLAB_AGENT_TIMEOUT` | `3600` | 单次 Planner/Worker/Verifier 总秒数；`0` 表示不限制 |
| `FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS` | 空数组 | JSON 字符串数组，只接受精确工具规则 |

这些变量是管理员允许范围和旧租户兼容默认值，不是用户选择 Planner/Worker 的唯一入口。
网页可以在管理员已安装且允许的 Codex/Claude 与模型中选择，但不能修改 CLI 路径、模型
允许列表、权限或并发上限。模型使用严格下拉框，选项随 Agent 切换并直接来自上述允许列表；
“CLI 默认模型”的值为空，表示使用对应 CLI 的默认模型。

认证来源按用户、按 Provider 保存：Codex CLI、Claude CLI 或个人 API Key。未配置个人 Key 的
新用户默认使用 Codex CLI；每用户 CLI 登录态保存在自己的 runtime 中，不能使用共享宿主
HOME。保存 Key 会切换到 API Key 模式，删除 Key 会切回对应 CLI。

额外 Claude 规则示例：

```ini
FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS=["Bash(npm run *)","Bash(python -m unittest *)"]
```

裸 `Bash`、`Bash(*)` 和带换行的规则会被拒绝。禁止
`--dangerously-skip-permissions`/`bypassPermissions`。

## 容量

| 变量 | 4U8G 生产值 | 范围/语义 |
| --- | --- | --- |
| `FASTLAB_GLOBAL_CONCURRENCY` | `4` | `1..32`，所有租户共享的 Planner/Worker/Verifier 槽位 |
| `FASTLAB_MAX_CONCURRENCY_PER_USER` | `1` | `1..32`，单租户同时运行上限 |
| `FASTLAB_PORT` | `4180` | `--port` 未显式给出时使用 |

网页中的任务并发是第三层、更小范围的上限，不能扩大管理员限制。4U8G 不应直接把全局值设为
8 或 32。压测触发内存/响应阈值时只把部署值降为 `2`，无需改代码。

## 环境白名单一致性

环境文件不接受任意键。新增配置必须同时修改：

1. `fastlab/config.py` 的 `LOCAL_ENV_KEYS`；
2. 本文和 `fastlab.env.example`；
3. 默认值/校验实现；
4. 白名单一致性测试。

进程级环境可以包含 systemd 所需变量，但 Runner 仍会从显式白名单构造 Agent 子进程环境，
不会把 FastResearch、主密钥、另一 Provider 密钥或宿主 HOME 透传给 CLI。

从旧版本升级时必须从活动环境文件删除全部 `FEISHU_*` 键。飞书功能已移除，未知键导致启动
失败是有意的安全行为；不要通过放宽白名单或忽略未知键恢复它们。若出于审计需要保留旧值，
应移到受控密钥归档，而不是 FastLab 环境文件。
