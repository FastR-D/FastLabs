# FastLab

FastLab 是运行在个人电脑上的本地 Agent 任务台。用户从网页或飞书提交目标，Codex 或 Claude Planner 只读生成任务计划；确认后，FastLab 把子任务分发给本机 Codex/Claude，在独立 Git Worktree 中按依赖并行执行、合并并验收。

## 快速启动

需要 Python 3.10+、Git，以及已安装并登录的 Codex CLI 或 Claude CLI。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p .fastlab
cp fastlab.env.example .fastlab/fastlab.env
python server.py
```

打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)，进入“设置”登记仓库，然后创建任务。

Windows、Planner 选择、环境文件和启动参数参考[安装与配置](docs/getting-started.md)。

## 基本流程

1. 选择仓库，填写任务目标。
2. Planner 生成子任务、依赖、执行器和验收标准。
3. 用户检查计划，可以直接修改子任务简介、具体要求、执行器和模型，并选择最终验收设置。
4. 确认后，FastLab 并行运行无依赖子任务。
5. 结果合入任务集成分支，通过验收后安全交付到目标目录。

Planner 只负责规划；并发、进程、Worktree、取消、重试、合并和验收由 FastLab 管理。详细操作参考[任务流程](docs/task-workflow.md)。

失败子任务可以把本轮新增要求、执行器和模型一起提交后重试。已完成但现场仍保留的子任务可以“修改并重跑”，带着新增要求改用新会话和新模型；“继续原会话”只发送追加说明，不更换执行器或模型。所有操作只有点击对应确认按钮后才会启动。任务交付清理后应使用“继续修改”。

## 详细文档

- 安装、Windows、Planner 和环境变量参考[安装与配置](docs/getting-started.md)。
- 创建、确认、追加、重试、验收和状态恢复参考[任务流程](docs/task-workflow.md)。
- 多仓库、子目录、Git 初始化、Worktree、交付和清理参考[仓库与 Git](docs/repositories-and-git.md)。
- Claude 命令被拦截和精确权限配置参考[Claude 执行权限](docs/claude-permissions.md)。
- 飞书应用、权限、长连接、白名单和机器人命令参考[飞书机器人配置指南](docs/feishu-bot.md)。
- 常见错误和处理方法参考[常见问题](docs/troubleshooting.md)。
- 当前完成情况和后续目标参考[实施计划](plan.md)。

## 项目边界

FastLab 是单用户、单机运行的轻量调度服务，不是 Codex App 或完整 Orca Runtime。它不提供远程 Worker、消息队列、动态 Agent 创建、外部 Skill 管理、tmux 或公网网站；网页只监听 `127.0.0.1`，手机通过飞书控制。

## 测试

```bash
python -m unittest discover -s tests -v
```

运行数据和本机凭证保存在 `.fastlab/`，不要提交。
