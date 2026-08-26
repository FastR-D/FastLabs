# 安装与配置

## 运行要求

- Python 3.10+
- Git
- 已安装并登录的 Codex CLI 或 Claude CLI

FastLab 只调用本机 CLI，不使用远程模型 API 或 Codex SDK，也不保存模型密钥。

## macOS 与 Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p .fastlab
cp fastlab.env.example .fastlab/fastlab.env
python server.py
```

打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)。首次启动后进入“设置”登记仓库。

## Windows PowerShell

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force .fastlab
Copy-Item fastlab.env.example .fastlab\fastlab.env
python server.py
```

原生 Windows 可以运行 FastLab、Codex CLI 和 Claude CLI，但 Claude Code 的系统级 Bash 沙箱只支持 macOS、Linux 和 WSL2。原生 Windows 上的 Claude 命令使用精确权限规则。

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

Planner 与 Worker 使用本机 CLI 的登录状态和默认模型。Planner 只读生成计划，不执行任务。

## Agent 总超时

Planner、Worker 和 Verifier 默认每次最多运行 3600 秒。可在本地环境文件中修改；设为 0 表示不限制：

```ini
FASTLAB_AGENT_TIMEOUT=3600
```

超时会终止该 Agent 的整个进程组，不会改变全局并发设置。

## 启动时登记仓库

`--workspace` 是可选快捷方式，可以传入 Git 仓库或仓库内的任意子目录；FastLab 会解析到 Git 顶层并登记为默认仓库。

```bash
python server.py --workspace /path/inside/repository
```

不传 `--workspace` 也可以正常启动，再从网页“设置”登记一个或多个仓库。

## 本地环境文件

默认配置文件是 `.fastlab/fastlab.env`。也可以指定其他文件：

```bash
python server.py --env-file /path/to/fastlab.env
```

进程中已经存在的同名环境变量优先。`.fastlab/` 已被 Git 忽略，但其中可能包含飞书密钥和运行数据库，不要复制、截图或提交。

飞书相关变量见[飞书机器人配置指南](feishu-bot.md)，Claude 命令权限见[Claude 执行权限](claude-permissions.md)。
