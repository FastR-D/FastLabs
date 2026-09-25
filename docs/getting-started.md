# 安装与配置

## 运行要求

- Python 3.10+
- Git
- Docker
- 管理员构建的 FastLab Worker 镜像（包含 Codex CLI、Claude CLI 和 Git）

服务器模式通过容器调用 CLI。每位用户在设置页提供自己的 API Key，FastLab 使用主密钥认证加密保存。

## macOS 与 Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p .fastlab
cp fastlab.env.example .fastlab/fastlab.env
python server.py
```

本地兼容模式打开 `http://127.0.0.1:4180`。服务器模式通过 FastResearch 同站点的 `/lab/` 进入。

## Windows PowerShell

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force .fastlab
Copy-Item fastlab.env.example .fastlab\fastlab.env
python server.py
```

生产多用户模式以 Linux Docker 主机为部署目标。Windows 可使用本地兼容模式或 WSL2 做开发验证。

## 选择 Planner

每次任务只使用一个 Planner。编辑 `.fastlab/fastlab.env`，选择 Codex 或 Claude，保存后重启 FastLab。

Codex Planner：

```ini
FASTLAB_PLANNER_BACKEND=codex
FASTLAB_CODEX_BIN=codex
FASTLAB_CLAUDE_BIN=claude
```

Claude Planner：

```ini
FASTLAB_PLANNER_BACKEND=claude
FASTLAB_CODEX_BIN=codex
FASTLAB_CLAUDE_BIN=claude
```

Planner 与 Worker 使用当前用户在 FastLab 中配置的 API Key，并受服务器模型白名单限制。Planner 只读生成计划，不执行任务。

## Agent 总超时

Planner、Worker 和 Verifier 默认每次最多运行 3600 秒。可在本地环境文件中修改；设为 0 表示不限制：

```ini
FASTLAB_AGENT_TIMEOUT=3600
```

超时会终止并清理该 Agent 的整个容器，不会改变全局或用户并发设置。

## 本地兼容模式登记仓库

`--workspace` 是可选快捷方式，可以传入 Git 仓库或仓库内的任意子目录；FastLab 会解析到 Git 顶层并登记为默认仓库。

```bash
python server.py --workspace /path/inside/repository
```

服务器多用户模式不接受 `--workspace` 作为共享仓库；管理员应将仓库放入用户专属 `repositories/`，用户在页面填写相对路径。

不传 `--workspace` 也可以正常启动，再从网页“设置”登记一个或多个仓库。

## 本地环境文件

默认配置文件是 `.fastlab/fastlab.env`。也可以指定其他文件：

```bash
python server.py --env-file /path/to/fastlab.env
```

进程中已经存在的同名环境变量优先。`.fastlab/` 已被 Git 忽略，但其中包含加密凭证、主密钥配置和运行数据库，不要复制、截图或提交。

Claude 命令权限见[Claude 执行权限](claude-permissions.md)。服务器多用户变量以仓库根目录的 `fastlab.env.example` 为准。
