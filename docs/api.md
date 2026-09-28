# HTTP API 契约

## 通用约定

- 生产基地址为与 FastResearch 同域的 `/lab/`；下表路径均以 `/lab` 后的路径表示。
- JSON 请求使用 `Content-Type: application/json`，请求体上限 1 MiB。空请求体按 `{}` 处理。
- JSON 错误至少包含 `{"error":"可读说明"}`。调用方不得依赖完整中文错误文本。
- `research` 模式除 `GET /api/health` 外都要求有效 FastResearch 会话。
- POST、PUT、DELETE 还要求 `Origin` 与 `FASTLAB_PUBLIC_URL` 的 origin 完全一致。
- 未认证 API 返回 `401` 和 `loginUrl`；未认证页面返回 `302` 到 FastResearch 面板。
- 不存在返回 `404`；校验错误返回 `400`；未初始化仓库返回带稳定 `code` 的 `409`；意外错误
  返回 `500`。
- API 不支持跨域 CORS。`OPTIONS` 只声明允许方法，不放宽同源检查。

## 读取接口

| 方法与路径 | 响应 | 说明 |
| --- | --- | --- |
| `GET /api/health` | `200` 健康对象 | 匿名；包含 `ok`、模式、Runner/队列摘要 |
| `GET /api/me` | `200` 用户对象 | 人员、租户空间、仓库数和凭证状态 |
| `GET /api/repositories` | `200 {repositories, management}` | 当前空间仓库；research 和默认 local 为 `managed`，显式旧 `--workspace` 为 `local_path` |
| `GET /api/settings/executors` | `200` 执行器设置 | 可用性、模型白名单、Planner 与角色默认值 |
| `GET /api/settings/skill` | `200` Skill 对象 | 当前用户的内容、来源、自定义状态和 SHA-256 |
| `GET /api/settings/credentials/{provider}` | `200` 状态 | `provider` 为 `openai` 或 `anthropic`；返回 `mode` 和掩码，不返回明文 |
| `GET /api/tasks` | `200 {tasks}` | 当前租户任务摘要，按现有顺序返回 |
| `GET /api/tasks/{taskId}` | `200` 任务详情 | 包含子任务、事件、文档和可用操作 |

## 创建和动作接口

| 方法与路径 | 主要请求字段 | 成功 |
| --- | --- | --- |
| `POST /api/tasks` | `title`, `goal`, `constraints`, `maxConcurrency`, `repositoryId`, `workingSubdir`; `workspace` 仅本地兼容 | `202` 任务 |
| `POST /api/repositories` | 多用户模式：`alias`, `cloneUrl?`, `isDefault`；local 兼容：`alias`, `path`, `initialize` | `201` 仓库 |
| `POST /api/repositories/{id}/initialize` | `{}` | `200` 仓库 |
| `POST /api/tasks/{id}/replan` | `feedback` | `202` 任务 |
| `POST /api/tasks/{id}/start` | `{}` | `202` 任务 |
| `POST /api/tasks/{id}/cancel` | `{}` | `202` 任务 |
| `POST /api/tasks/{id}/verify` | `{}` | `202` 任务 |
| `POST /api/tasks/{id}/accept-verification` | `evidence` | `202` 任务 |
| `POST /api/tasks/{id}/rerun` | `{}` | `202` 新任务 |
| `POST /api/tasks/{id}/continue` | `message` | `202` 新任务 |
| `POST /api/tasks/{id}/deliver` | `{}` | `202` 任务 |
| `POST /api/tasks/{id}/cleanup` | `{}` | `202` 清理结果 |
| `POST /api/subtasks/{id}/retry` | `executor`, `model`, `reasoningEffort`, `message` | `202` 任务 |
| `POST /api/subtasks/{id}/message` | `message` | `202` 任务 |
| `POST /api/subtasks/{id}/revise` | `message`, `executor`, `model`, `reasoningEffort` | `202` 任务 |

创建任务的最小请求：

```json
{
  "goal": "为搜索接口增加分页",
  "repositoryId": "0123456789abcdef",
  "workingSubdir": "backend",
  "maxConcurrency": 3
}
```

`maxConcurrency` 是任务内部上限，不会扩大管理员配置的全局或每用户上限。

## 修改接口

| 方法与路径 | 主要请求字段 | 成功 |
| --- | --- | --- |
| `PUT /api/settings/executors` | `planner`, `worker`, `verifier`；各含 `executor`, `model`, `reasoningEffort` | `200` 设置 |
| `PUT /api/settings/skill` | `content` | `200` Skill 元数据与内容 |
| `PUT /api/settings/auth/{provider}` | `mode`：`cli` 或 `api_key` | `200` 凭证状态 |
| `PUT /api/settings/credentials/{provider}` | `key` | `200` 掩码状态 |
| `PUT /api/subtasks/{id}` | `title`, `instructions`, `executor`, `model`, `reasoningEffort` | `200` 任务 |
| `PUT /api/subtasks/{id}/executor` | `executor`, `model`, `reasoningEffort` | `200` 任务 |
| `PUT /api/tasks/{id}` | `title` | `200` 任务 |
| `PUT /api/tasks/{id}/verifier` | `executor`, `model`, `reasoningEffort` | `200` 任务 |
| `PUT /api/repositories/{id}` | `alias`, `isDefault`；`path` 仅 local 兼容 | `200` 仓库 |

用户只能在服务器已安装的 Codex/Claude 和管理员模型允许列表内选择。CLI 路径、执行后端、
并发、安全策略和主密钥仍只能由管理员配置。保存个人 Key 会自动把对应 Provider 切换到
`api_key`；选择 `api_key` 但尚未保存 Key 返回 `400`。

## 删除接口

| 方法与路径 | 成功 | 影响 |
| --- | --- | --- |
| `DELETE /api/settings/credentials/{provider}` | `200 {ok:true}` | 删除当前租户该 Provider 密钥并切回 CLI |
| `DELETE /api/settings/skill` | `200` Skill 对象 | 用内置模板替换当前用户 Skill |
| `DELETE /api/tasks/{id}/events` | `200` | 清空该任务日志事件，不删除任务 |
| `DELETE /api/tasks/{id}` | `200` | 删除记录、文档和该任务临时 Git 资源 |
| `DELETE /api/repositories/{id}` | `200` | 仅供旧 local 外部路径模式删除登记；托管模式拒绝 |
| `DELETE /api/repositories/{id}/managed` | `200` | 永久删除当前空间托管仓库的登记和磁盘目录；不支持 local 外部路径 |

## 特殊错误

旧 local 路径模式的仓库需要明确初始化时返回：

```json
{
  "error": "说明",
  "code": "repository_initialization_required",
  "path": "租户内相对路径",
  "reason": "not_git"
}
```

`reason` 当前为 `not_git` 或 `no_commits`。生产 `research` 模式由 FastLab 自动创建或克隆仓库，
不会进入这个用户确认流程，也不会在错误中泄露宿主目录。

调用方应以 `code` 分支，不应解析 `error`。API Key、主密钥和其他租户信息不得进入任何错误
响应。

CLI 登录态位于当前用户独立 runtime，API 不读取、返回或复制该目录内容。

多用户模式下 `management.mode` 为 `managed`，`POST /api/repositories` 拒绝 `path`。`cloneUrl`
为空表示创建空仓库；非空时仅接受无内嵌凭证的 HTTPS URL。响应中的 `path` 即使为兼容保留也
只能是租户内不透明相对标识，不得包含宿主目录。

## 兼容与测试

增加字段属于向后兼容；删除、重命名、改变类型、改变状态码或改变动作语义都需要版本决策、
Golden 测试和迁移说明。路由变更必须覆盖：成功、未认证、CSRF、非法 JSON、超大请求、
校验错误、未知 ID 和内部错误。
