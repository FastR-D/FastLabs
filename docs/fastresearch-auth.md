# FastResearch 登录契约

## 部署拓扑

FastResearch 和 FastLab 部署在同一主机、同一浏览器 origin：

```text
https://research.example.com/       -> FastResearch
https://research.example.com/lab/   -> FastLab (127.0.0.1:4180)
```

FastLab 不读取 FastResearch 数据库，也不要求修改 FastResearch 源码。FastResearch 部署只需把
已有面板入口指向 `https://research.example.com/lab/`。

## 会话验证

浏览器把现有 `fr_session` Cookie 发送到 `/lab/`。FastLab 从 Cookie 中取 token，并向
`FASTRESEARCH_API_URL` 发起：

```http
GET /api/content/me HTTP/1.1
Authorization: Bearer <fr_session token>
Cookie: fr_session=<fr_session token>
Accept: application/json
```

成功响应必须是 JSON，并包含稳定、非空的 `accountId`：

```json
{
  "accountId": "stable-account-id",
  "person": "显示名称"
}
```

- `accountId` 是租户身份主键；不得改用可变的显示名、邮箱或 Cookie token。
- `person` 只用于显示；空值回退为“FastResearch 用户”。
- 连接错误、超时、非 JSON 或缺少 `accountId` 都按未认证处理。
- 有效结果按 token 哈希最多缓存 30 秒，因此撤销会话最长可能有约 30 秒传播延迟。
- FastLab 不持久化原始会话 token。

启动检查调用 `GET /api/health`，响应必须包含真值 `ok`。该检查只证明 FastResearch 可达，
不替代实际会话验证。

## 浏览器行为

- 未认证的 API 请求返回 `401`：`{"error":"...","loginUrl":"..."}`。
- 未认证页面请求返回 `302`，Location 为 `FASTRESEARCH_PANEL_URL`。
- `GET /api/health` 不要求认证，供 systemd/Nginx 监控。
- 所有有副作用的请求要求 `Origin` 等于 `FASTLAB_PUBLIC_URL` 的 origin。Nginx 必须保留 Host，
  浏览器和代理不得把 FastLab 暴露成另一个 origin。
- Cookie 的 `Secure`、`HttpOnly`、`SameSite`、Domain 和 Path 由 FastResearch 管理。生产验证必须
  确认 Cookie 在 `/lab/` 下会被发送。

## 配置配对

```ini
FASTLAB_AUTH_MODE=research
FASTRESEARCH_API_URL=http://127.0.0.1:8787
FASTRESEARCH_COOKIE_NAME=fr_session
FASTRESEARCH_PANEL_URL=https://research.example.com/
FASTLAB_PUBLIC_URL=https://research.example.com/lab/
FASTLAB_PUBLIC_PATH=/lab
```

`FASTLAB_PUBLIC_URL` 必须是完整 HTTP(S) URL，其路径必须与 `FASTLAB_PUBLIC_PATH` 一致。外部
HTTPS 在 Nginx 终止时，PUBLIC_URL 仍应填写浏览器看到的 `https://` 地址。

## 契约变更

FastResearch 若要改变 Cookie 名、`/api/content/me` 路径或响应字段，必须先在 FastLab 增加
兼容实现与集成测试，再部署 FastResearch。不能同时发布两个互不兼容版本。联调至少覆盖：

1. 有效会话访问页面和 API。
2. 无 Cookie、过期 Cookie、伪造 Cookie。
3. FastResearch 不可达、超时和错误 JSON。
4. Cookie 在 `/lab/` 的实际 Path/SameSite 行为。
5. 跨 origin POST/PUT/DELETE 被拒绝。
