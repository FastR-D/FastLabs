# FastLab

FastLab 是挂载在 FastResearch 下的多用户 Agent 任务台。它复用 FastResearch 的成员会话，每个账号拥有独立的仓库登记、任务数据库、Worktree、运行状态和加密模型凭证。Planner、Worker 和 Verifier 均在受限 Docker 容器中运行。

## 服务器启动

需要 Python 3.10+、Git、Docker，以及包含 `codex`、`claude` 和 Git 的管理员维护 Worker 镜像。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p .fastlab
cp fastlab.env.example .fastlab/fastlab.env
chmod 600 .fastlab/fastlab.env
python server.py
```

服务默认只监听 `127.0.0.1:4180`。生产环境应把同一站点的 `/lab/` 反向代理到该端口：

```nginx
location = /lab { return 308 /lab/; }
location /lab/ {
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_pass http://127.0.0.1:4180/;
}
```

FastResearch 只需在部署环境中把已有 `VITE_FASTLAB_URL` 设置为 `https://research.example.com/lab/`；FastLab 不要求修改 FastResearch 源码。

## 用户空间

用户首次携带有效 `fr_session` 访问时，FastLab 使用 FastResearch `/api/content/me` 返回的稳定 `accountId` 创建隔离目录：

```text
<FASTLAB_TENANT_ROOT>/<account-id-hash>/
├── repositories/
├── tasks/
├── worktrees/
└── runtime/
```

管理员把 Git 仓库预先放入对应用户的 `repositories/`，用户在设置页只登记相对路径。绝对路径、`..` 和越界符号链接会被拒绝。

用户在设置页分别保存 OpenAI 和 Anthropic API Key。凭证使用 `FASTLAB_CREDENTIAL_MASTER_KEY` 认证加密，接口只返回掩码；每次容器只获得当前用户、当前执行器所需的凭证。

## 安全边界

- 业务页面和 API 必须通过 FastResearch 成员认证；只有 `/api/health` 公开。
- 数据库、仓库、任务文档、Worktree 和 CLI runtime 按租户分开。
- Agent 容器非 root、只读根文件系统、移除 capabilities，且不挂载 Docker socket、FastLab 数据库、主机 HOME 或其他用户目录。
- 全局并发和每用户并发同时生效，由服务器环境变量控制。
- 旧 `.fastlab/fastlab.db` 不迁移、不修改；多用户注册和凭证使用 `.fastlab/fastlab-v2.db`。

## 本地兼容模式

`FASTLAB_AUTH_MODE=local` 保留原来的无认证回环开发方式，不启用多用户注册和容器强制策略。它只能用于本机开发，不应通过反向代理暴露。

## 测试

```bash
python -m unittest discover -s tests -v
```

更详细的任务、仓库、执行权限和故障排查说明见 `docs/`。
