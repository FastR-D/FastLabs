# 安装与配置

## 本地开发

最低要求：Python 3.10+、Git。Node 22 仅用于前端测试。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

打开 `http://127.0.0.1:4180`。未配置环境文件时默认为 `local` 模式，只允许本机开发；不要
通过反向代理把该模式暴露给其他用户。

普通本机启动与多用户模式使用相同的托管仓库页面，仓库存放在
`<data-dir>/repositories/<repository-id>/`。在网页创建空仓库或填写 HTTPS Git URL 即可。

旧开发脚本可以显式用 `--workspace` 登记默认仓库。参数可以指向 Git 根目录或其任意子目录；
此时进入兼容路径模式，不是新安装的推荐用法：

```bash
python server.py --workspace /path/inside/repository
```

不传 `--workspace` 时不会要求填写任何本机路径。

## 生产前置条件

Linux 生产主机还需要：

- 专用 `fastlab` 系统用户；
- 固定版本的 Codex CLI 与 Claude Code；
- `bubblewrap`、`socat` 和 CLI 所需的 Node runtime；
- 同机 FastResearch 和 Nginx；
- 仅 `fastlab` 用户可读的环境文件与数据目录。

默认 `process` 后端不要求 Docker。若组织需要更强的租户边界，再安装 Docker 并显式设置
`FASTLAB_EXECUTION_BACKEND=docker`。

生产配置：

```bash
sudo install -d -o fastlab -g fastlab -m 0700 \
  /srv/fastlab/data /srv/fastlab/tenants /srv/fastlab/runtime
sudo install -d -o root -g fastlab -m 0750 /srv/fastlab/config
sudo install -o root -g fastlab -m 0600 fastlab.env.example \
  /srv/fastlab/config/fastlab.env
```

编辑配置并生成独立主密钥：

```bash
/srv/fastlab/app/.venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

然后按照[部署指南](deployment.md)执行启动检查、沙箱发布门禁和 systemd 安装。

## 环境文件和优先级

默认环境文件是 `<data-dir>/fastlab.env`，可通过 `--env-file` 指定。启动时先预解析
`--data-dir`/`--env-file` 并加载文件，再解析其余参数，因此文件中的 `FASTLAB_PORT` 会生效。

优先级从高到低：

1. 显式 CLI 参数；
2. 服务进程已有环境变量；
3. 环境文件；
4. 代码默认值。

环境文件只接受[配置参考](configuration.md)列出的白名单键；未知键会阻止启动。不要把引号、
命令替换或 shell 脚本放入该文件，它是简单的 `KEY=VALUE` 格式，不是 shell。

旧环境文件中的 `FEISHU_*` 配置必须删除；本版本不恢复飞书集成，也不会忽略这些未知键。

## 设置 Agent、模型与认证

```ini
FASTLAB_CODEX_BIN=/opt/fastlab-cli/bin/codex
FASTLAB_CLAUDE_BIN=/opt/fastlab-cli/bin/claude
```

管理员安装 CLI 并配置模型允许列表。用户在网页设置页选择 Planner、Worker、Verifier 的
Agent 与模型，并可编辑自己的 orchestration Skill。Planner 始终只读生成计划。

认证可按 Provider 选择 CLI 登录或个人 API Key。新用户未填写 Key 时默认使用自己 runtime
中的 Codex CLI 登录；Claude CLI 也使用该用户独立 runtime。生产启用前，管理员需在对应用户
runtime 完成 CLI 登录，或让用户在网页保存加密 API Key。不得复制宿主 HOME 或其他用户的
CLI 登录文件。

## Windows 与 macOS

这些系统可以使用 `local` 模式开发和运行假 CLI 测试。生产多用户直连沙箱以 Linux 为唯一
支持目标；原生 Windows/macOS 不能证明与 Linux `bubblewrap` 相同的发布隔离，不得把缺失
沙箱静默当作成功。
