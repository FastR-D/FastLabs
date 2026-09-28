# 生产部署

本指南针对单台 Linux 4U8G、同机 FastResearch、Nginx 同域 `/lab/`、systemd 单进程和默认
`process` Runner，这是当前唯一执行后端。

## 1. 创建专用账号和目录

以下路径可按组织规范调整，但配置、数据和 runtime 不应放在可被其他服务用户读取的位置：

```bash
sudo useradd --system --home /srv/fastlab/runtime/service \
  --shell /usr/sbin/nologin fastlab
sudo install -d -o root -g fastlab -m 0750 /srv/fastlab/config
sudo install -d -o fastlab -g fastlab -m 0700 \
  /srv/fastlab/data /srv/fastlab/tenants /srv/fastlab/runtime \
  /srv/fastlab/runtime/service /srv/fastlab/runtime/tmp
```

应用放在 `/srv/fastlab/app`，由 root 发布并对 `fastlab` 只读。不要把 `fastlab` 加入 docker、
sudo 或其他特权组。

## 2. 安装系统依赖和固定 CLI

安装 Python 3.10+、Git、Node runtime、`bubblewrap`、`socat`、SQLite 工具和 curl。不同发行版
包名可能不同；Ubuntu/Debian 示例：

```bash
sudo apt-get install python3 python3-venv git nodejs bubblewrap socat sqlite3 curl
```

Codex/Claude 必须使用发布单中批准的精确版本，禁止 `latest`、范围版本或启动时自动更新：

```bash
sudo npm install --global --prefix /opt/fastlab-cli \
  @openai/codex@<approved-codex-version> \
  @anthropic-ai/claude-code@<approved-claude-version>
sudo chown -R root:root /opt/fastlab-cli
sudo chmod -R go-w /opt/fastlab-cli
/opt/fastlab-cli/bin/codex --version
/opt/fastlab-cli/bin/claude --version
```

把精确版本、安装源、校验信息和版本输出记录在发布单中。`ProcessCommandRunner` 为 Claude
子进程固定注入 `DISABLE_AUTOUPDATER=1` 及非必要流量/子进程环境清理设置；root 所有的只读
安装目录是第二道保护。

## 3. 安装应用

```bash
cd /srv/fastlab/app
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements.txt
```

生产发布建议使用版本目录和只读 `current` 符号链接，具体见[发布与回滚](release-rollback.md)。

## 4. 配置

```bash
sudo install -o root -g fastlab -m 0600 fastlab.env.example \
  /srv/fastlab/config/fastlab.env
```

至少修改 URL、租户目录、主密钥和固定 CLI 路径，并保留：

```ini
FASTLAB_AUTH_MODE=research
FASTLAB_GLOBAL_CONCURRENCY=4
FASTLAB_MAX_CONCURRENCY_PER_USER=1
```

主密钥生成方法见[开始使用](getting-started.md)。不要把主密钥存入 shell history、发布包或
普通日志。配置完整说明见[配置参考](configuration.md)。

## 5. 安装 systemd 服务

检查 [`deploy/fastlab.service.example`](../deploy/fastlab.service.example) 中的实际 Python、CLI、
FastResearch unit 和目录路径，再安装：

```bash
sudo install -o root -g root -m 0644 deploy/fastlab.service.example \
  /etc/systemd/system/fastlab.service
sudo systemctl daemon-reload
sudo systemctl enable fastlab.service
sudo systemctl start fastlab.service
sudo systemctl status fastlab.service
```

服务收到 SIGTERM/SIGINT 时停止接收新任务，终止活动 CLI 进程树并清理后台线程。systemd
`KillMode=control-group` 是最终兜底。

## 6. 配置 Nginx

把 [`deploy/nginx-fastlab.conf`](../deploy/nginx-fastlab.conf) 放入 FastResearch 的同一
`server {}`，不要创建新的跨域站点。检查并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

FastLab 仍只监听 `127.0.0.1:4180`，TLS 由 Nginx 终止。

## 7. 发布门禁

上线前全部满足：

1. `curl http://127.0.0.1:8787/api/health` 与
   `curl http://127.0.0.1:4180/api/health` 返回 `ok`。
2. 浏览器从 FastResearch 登录后可以访问 `/lab/`，跨 origin 写请求返回 403。
3. Python 3.10/3.13、Node 22、Ruff、coverage、HTTP、前端和部署 smoke test 通过。
4. Linux 实机沙箱测试无法读取另一租户 sentinel、服务环境文件和主密钥；任一读取成功都
   阻止发布，重新设计并验证更强的隔离方案。
5. 以真实 Codex/Claude 并发 4 压测 30 分钟。内存超过 80%、swap 持续增长、OOM 或控制面
   明显阻塞时，把 `FASTLAB_GLOBAL_CONCURRENCY` 降为 `2` 后重新验证。
6. 创建发布前一致性备份并验证可列出内容；确认 FastResearch/FastNews 无代码改动。
