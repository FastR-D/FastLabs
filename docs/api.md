# HTTP API 契约

## 通用约定

- 生产基地址为与 FastResearch 同域的 `/lab/`；下表路径均以 `/lab` 后的路径表示。
- JSON 请求使用 `Content-Type: application/json`，请求体上限 1 MiB。空请求体按 `{}` 处理。
- JSON 错误至少包含 `{"error":"可读说明"}`。调用方不得依赖完整中文错误文本。
- `research` 模式除 `GET /api/health` 外都要求有效 FastResearch 会话。
- POST、PUT、DELETE 还要求 `Origin` 与 `FASTLAB_PUBLIC_URL` 的 origin 完全一致。
- 未认证 API 返回 `401` 和 `loginUrl`；未认证页面返回 `302` 到 FastResearch 面板。
- 不存在返回 `404`；校验错误返回 `400`；意外错误
  返回 `500`。
- API 不支持跨域 CORS。`OPTIONS` 只声明允许方法，不放宽同源检查。

## 读取接口

| 方法与路径 | 响应 | 说明 |
| --- | --- | --- |
| `GET /api/health` | `200` 健康对象 | 匿名；包含 `ok`、模式、Runner/队列摘要 |
| `GET /api/me` | `200` 用户对象 | 人员、租户空间、仓库数和凭证状态 |
| `GET /api/repositories` | `200 {repositories, management}` | 当前空间托管仓库；所有模式均为 `managed` |
| `GET /api/settings/executors` | `200` 执行器设置 | 可用性、模型白名单、Planner 与角色默认值 |
| `GET /api/settings/skill` | `200` Skill 对象 | 当前用户的内容、来源、自定义状态和 SHA-256 |
| `GET /api/settings/credentials/{provider}` | `200` 状态 | `provider` 为 `openai` 或 `anthropic`；返回 `mode` 和掩码，不返回明文 |
| `GET /api/tasks` | `200 {tasks}` | 当前租户任务摘要，按现有顺序返回 |
| `GET /api/tasks/{taskId}` | `200` 任务详情 | 包含子任务、事件、文档和可用操作 |

## 创建和动作接口

| 方法与路径 | 主要请求字段 | 成功 |
| --- | --- | --- |
| `POST /api/tasks` | `title`, `goal`, `constraints`, `maxConcurrency`, `repositoryId`, `workingSubdir` | `202` 任务 |
| `POST /api/repositories` | `alias`, `cloneUrl?`, `isDefault` | `201` 仓库 |
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
| `PUT /api/repositories/{id}` | `alias`, `isDefault` | `200` 仓库 |

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
| `DELETE /api/repositories/{id}/managed` | `200` | 永久删除当前空间托管仓库的登记和磁盘目录；不支持 local 外部路径 |

## 错误与数据边界

错误返回 HTTP 状态码及可读的 `error`。API Key、主密钥和其他租户信息不得进入错误响应。

CLI 登录态位于当前用户独立 runtime，API 不读取、返回或复制该目录内容。

所有模式下 `management.mode` 为 `managed`，`POST /api/repositories` 拒绝 `path`。`cloneUrl`
为空表示创建空仓库；非空时仅接受无内嵌凭证的 HTTPS URL。响应中的 `path` 仅为租户内不透明相对标识，不得包含宿主目录。

## 兼容与测试

增加字段属于向后兼容；删除、重命名、改变类型、改变状态码或改变动作语义都需要版本决策、
Golden 测试和迁移说明。路由变更必须覆盖：成功、未认证、CSRF、非法 JSON、超大请求、
校验错误、未知 ID 和内部错误。

## 本次清理的接口变更

- `server.py` 只提供启动。旧 `agent_adapter`、`multitenancy` 导入已删除；改为从 `fastlab.agents`、`fastlab.tenant_registry`、`fastlab.tenancy`、`fastlab.runners` 等对应模块导入。
- `FastLab` 构造参数统一使用关键字，不再接受 `workspace`。`create_task` 不再接受路径选择；HTTP 请求只要包含 `workspace`，即返回 `400`。省略 `repositoryId` 时使用默认仓库。
- 创建和修改仓库的请求含 `path` 时返回 `400`。仅移除记录的 DELETE 接口已删除，返回 `404`；永久删除仍使用 `DELETE /api/repositories/{id}/managed`。
- 外部目录登记和其初始化确认错误已移除；`initialize` 动作仅适用于现有托管仓库。
- 多用户健康对象固定返回 `executionBackend: "process"`，不再返回 `docker`、`workerImage`。
- 数据库结构迁移继续保留；本次接口删减不自动迁移外部仓库或清空其他部署的数据。

## Agent 库与分配

所有接口沿用现有租户认证和同源检查；ID 只在当前租户解析。

| 方法与路径 | 输入 / 响应 |
| --- | --- |
| `GET /api/agents` | `{agents: [...]}`，仅返回未归档定义 |
| `POST /api/agents` | 完整定义；返回 `201` 和 ID、version=1 |
| `GET /api/agents/{id}` | 当前版本定义，包含归档状态 |
| `PUT /api/agents/{id}` | 完整定义及读取时的 `version`；版本不符返回 400，成功生成新版本 |
| `DELETE /api/agents/{id}` | 归档，返回 `{ok:true}`，不删除历史版本 |
| `POST /api/tasks/{id}/agents` | 确认前分配；`role` 为 worker/verifier，worker 另需 `subtaskId`；`agentId` 或 `temporaryAgent` 二选一 |

定义字段：`name`（必填，最多 80 字符）、`description`（2000）、`instructions`（20000）、
`skills`（40000，纯文本）、`roles`（planner/worker/verifier 的非空数组）、
`executor`（codex/claude）、`model`（空为默认）、`reasoningEffort`（默认 high）。
不接收权限、CLI 路径或凭证字段。模型及执行能力通过原有管理员策略校验。
复制/另存使用 POST 创建新 ID；任务内编辑通过 temporaryAgent 替换快照。

`POST /api/tasks` 增加可选 `agents: {planner?: id, worker?: id, verifier?: id}`。
Planner 子任务增加可选 `agentId`、`temporaryAgent`，不得同时使用；无新字段的旧计划仍受支持。
真实 Codex 输出使用严格 Schema 的 nullable 字段，输入验证继续接受旧计划省略字段。
`GET /api/tasks/{id}` 增加 `runs` 调用历史；角色和子任务配置增加 `agent` 定义快照。
原有执行器/模型覆盖接口保留，实际调用配置与定义来源分别记录。
