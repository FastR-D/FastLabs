# 飞书机器人配置指南

FastLab 使用飞书企业自建应用的 WebSocket 长连接。网站仍只监听 `127.0.0.1`，不需要公网 IP、域名或 HTTP 回调地址；运行 FastLab 的电脑必须保持在线。

## 1. 创建并启用机器人

1. 打开[飞书开放平台](https://open.feishu.cn/app)，创建“企业自建应用”。
2. 在“添加应用能力”中启用“机器人”，设置名称和头像。
3. 在应用可用范围中包含准备使用 FastLab 的成员。私聊用户还必须加入 FastLab 的本地白名单。

飞书后台界面调整时，可对照官方的[机器人快速开发说明](https://open.feishu.cn/document/develop-an-echo-bot/introduction)和[应用配置说明](https://open.feishu.cn/document/develop-an-echo-bot/faq)。

## 2. 申请消息权限

在“开发配置 → 权限管理”中申请下列权限，并等待管理员审批：

| 权限 | 权限代码 | 用途 |
| --- | --- | --- |
| 获取用户发给机器人的单聊消息 | `im:message.p2p_msg:readonly` | 接收私聊命令 |
| 获取群聊中 @ 机器人的消息 | `im:message.group_at_msg:readonly` | 接收群聊命令 |
| 以应用身份发消息 | `im:message:send_as_bot` | 回复文字、发送和更新任务卡片 |

## 3. 配置长连接事件与回调

1. 进入“事件与回调”，订阅方式选择“使用长连接接收事件”，不要填写本机 HTTP 地址。
2. 在事件订阅中添加 `im.message.receive_v1`。
3. 在回调订阅中添加 `card.action.trigger`，用于确认、取消、重试和重新验收按钮。
4. 保存配置。卡片按钮机制可参考飞书的[卡片交互说明](https://open.feishu.cn/document/common-capabilities/message-card/add-card-interaction/interaction-module)。

## 4. 发布应用并取得凭证

1. 在“版本管理与发布”中创建并发布版本。权限、事件或可用范围变更后需要重新发布。
2. 在“凭证与基础信息”复制 App ID 和 App Secret。
3. 取得允许操作机器人的用户 Open ID。可以在飞书的[通过手机号或邮箱获取用户 ID](https://open.feishu.cn/document/server-docs/contact-v3/user/batch_get_id)调试台中选择当前应用并取得 `open_id`。Open ID 与应用绑定，换应用后需要重新获取。

## 5. 写入本地配置

推荐把配置写在 `.fastlab/fastlab.env`：

```bash
mkdir -p .fastlab
cp fastlab.env.example .fastlab/fastlab.env
chmod 600 .fastlab/fastlab.env
```

编辑配置文件：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=替换为新密钥
FASTLAB_FEISHU_ALLOWED_OPEN_IDS=ou_xxx,ou_yyy

# 可选：网页来源的任务也通知到这个飞书会话
# FASTLAB_FEISHU_DEFAULT_CHAT_ID=oc_xxx
```

然后直接启动：

```bash
python server.py
```

`.fastlab/` 已被 Git 忽略。配置以明文保存在本机，不要复制、截图或提交该文件。进程环境中已经存在的同名变量优先于配置文件；也可以通过 `--env-file /path/to/file` 指定其他文件。

如果不使用配置文件，也可以继续使用环境变量：

```bash
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="..."
export FASTLAB_FEISHU_ALLOWED_OPEN_IDS="ou_xxx,ou_yyy"
export FASTLAB_FEISHU_DEFAULT_CHAT_ID="oc_xxx" # 可选
```

Windows PowerShell 可以使用 `$env:FEISHU_APP_ID = "cli_xxx"` 等对应写法。

## 6. 验证连接

1. 打开本地网站的“设置”，飞书状态应为“已连接”，并显示正确的白名单人数。
2. 访问 `http://127.0.0.1:8787/api/health`，确认 `feishu.configured` 和 `feishu.connected` 均为 `true`，且 `error` 为 `null`。
3. 私聊机器人发送 `帮助`；群聊发送 `@机器人 帮助`。收到命令列表说明收发链路正常。

任务完成后需要新增要求时，发送 `继续 <任务ID> <追加要求>`，FastLab 会从已交付代码创建新任务。`追加 <任务ID> <子任务ID> <说明>` 只用于仍保留 Worktree 和会话的原子任务；发送 `帮助 详细` 可查看全部格式。

私聊只接受白名单用户；群聊还必须 @ 机器人。App Secret 不进入 SQLite、日志或健康接口。

## 常见问题

- 显示“未配置”：检查 `.fastlab/fastlab.env` 的位置和键名。
- 显示“白名单为空”：补充 `FASTLAB_FEISHU_ALLOWED_OPEN_IDS` 并重启。
- 能解析机器人身份但连接失败：检查是否选择长连接、应用是否发布以及网络是否可访问飞书。
- 机器人不回复：确认 Open ID 属于当前应用，私聊用户在白名单中；群聊必须 @ 机器人。
- 能收消息但不能发卡片：检查 `im:message:send_as_bot` 和卡片回调是否审批并随新版本发布。
