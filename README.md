# FastLab

FastLab 是挂载在 FastResearch 下的多用户 Agent 任务台。它把需求规划、并行子任务、
Git Worktree、自动验收和安全交付组织成一条可追踪的流程。每个 FastResearch 账号拥有
独立的数据、仓库登记、Worktree、CLI runtime 和加密模型凭证。

每个用户可以在设置页选择 Planner、Worker 和 Verifier 的 Codex/Claude Agent 与模型，维护
自己的 orchestration Skill，并选择独立 CLI 登录或个人 API Key。未填写 API Key 时默认使用
该用户空间中的 Codex CLI 登录。模型通过服务器允许列表生成的下拉框选择；选择“CLI 默认模型”
时不显式传递模型名称。

设置采用应用内独立视图：左上方“← 返回”会回到进入前的首页或任务，桌面端通过左侧分类
导航分别管理 Agent、模型凭证、Skill 和仓库，窄屏下显示为横向页签；右侧一次只显示当前分类。
多用户模式的仓库由 FastLab 自动存放在个人大工作目录中，用户只需创建空仓库或填写 HTTPS
Git URL 克隆，不需要知道服务器文件路径。
普通本机启动也使用 `<data-dir>/repositories/` 和同一套仓库管理界面；只有显式使用旧
`--workspace` 参数时才进入现有路径兼容模式。
托管仓库在无运行中任务时可二次确认“永久删除仓库”，同时清理登记和 FastLab 生成的磁盘目录，
不提供会遗留孤立目录的“仅移除记录”；外部兼容路径不会开放文件删除，只能移除登记。

首版生产部署直接调用 `codex exec` 与 `claude -p`，不要求 Docker。该模式面向约 50 名
可信内部用户：4U8G 主机默认同时运行 4 个 Agent、每用户最多 1 个，其余请求按 FIFO
排队。它不是容器级租户安全边界；不完全可信用户必须改用 Docker 或 VM 隔离。

## 最短启动方式

开发环境需要 Python 3.10+ 和 Git：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

生产环境还必须安装固定版本的 Codex CLI、Claude Code、`bubblewrap` 和 `socat`，完成
FastResearch 同域认证和沙箱发布门禁后再启动：

```bash
cp fastlab.env.example /srv/fastlab/config/fastlab.env
chmod 600 /srv/fastlab/config/fastlab.env
/srv/fastlab/app/.venv/bin/python /srv/fastlab/app/server.py \
  --data-dir /srv/fastlab/data \
  --env-file /srv/fastlab/config/fastlab.env
```

默认只监听 `127.0.0.1:4180`。生产入口为 FastResearch 同域的 `/lab/`，参考
[`deploy/nginx-fastlab.conf`](deploy/nginx-fastlab.conf) 和
[`deploy/fastlab.service.example`](deploy/fastlab.service.example)。

## 文档

当前分支的功能完成度和后续顺序见 [`plan.md`](plan.md)。从[文档索引](docs/README.md)
开始阅读完整资料。常用入口：

- [需求与功能不变量](docs/requirements.md)
- [架构与运行模型](docs/architecture.md)
- [设置中心](docs/settings.md)
- [安装配置](docs/getting-started.md)与[完整配置参考](docs/configuration.md)
- [部署](docs/deployment.md)、[运维](docs/operations.md)与[安全边界](docs/security.md)
- [HTTP API](docs/api.md)与[FastResearch 登录契约](docs/fastresearch-auth.md)
- [开发与测试](docs/development.md)

## 快速验证

```bash
python -m unittest discover -s tests -v
npm test
# 安装文档固定的 Playwright 版本和 Chromium 后：
npm run test:e2e
python deploy/smoke_test.py
```

FastLab 当前是单主机、单进程、SQLite 部署，不支持多个服务实例共享同一数据目录或
Worktree。
