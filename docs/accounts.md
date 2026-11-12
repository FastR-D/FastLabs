# 托管账号

设置 `FASTLAB_PUBLIC_URL` 后启用托管模式，未登录用户需先登录才能访问任务。未设置时继续使用本机任务台和设备配对。

托管模式关闭公开自主注册。新用户通过 FastCAS 登录、填写姓名、邮箱及本地密码，再确认创建并绑定。管理员也可在「用户管理」创建本地账号。已有邮箱必须先登录原账号再绑定。

FastCAS 已验证的 `role=admin` 在登录或绑定成功时自动提升平台新账号和已有账号；普通角色保留平台已有管理员权限。管理员可以管理账号和全站任务、仓库及设置，普通用户只能访问自己的任务，并使用已登记的仓库创建任务。历史未分配任务由管理员管理。

用户管理支持搜索、创建、编辑、重置密码、启用/停用及删除。凭据、角色和状态变化会撤销会话。删除保留任务历史并停止登录。系统保护当前管理员和最后一名启用的管理员。

## FastCAS 配置

使用独立的机密客户端，与本地设备配对客户端分开：

```text
FASTLAB_PUBLIC_URL=https://labs.example.org
FASTLAB_WEB_FASTCAS_ISSUER=https://cas.example.org
FASTLAB_WEB_FASTCAS_CLIENT_ID=fastlabs-web
FASTLAB_WEB_FASTCAS_CLIENT_SECRET=<client secret>
```

客户端启用 `authorization_code`、`refresh_token` 和 `openid profile email`，配置：

- 回调：`/api/auth/fastcas/callback`
- 绑定事件：`/api/auth/fastcas/events`
- 后端退出：`/api/auth/fastcas/backchannel-logout`

以上路径均位于 `FASTLAB_PUBLIC_URL`。空账号库可用 `FASTLAB_ADMIN_EMAIL` 和 `FASTLAB_ADMIN_PASSWORD` 初始化本地管理员，也可由 FastCAS 管理员完成首次注册。
