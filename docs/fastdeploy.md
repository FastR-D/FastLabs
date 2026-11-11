# FastLabs 服务器部署

`.github/workflows/tag-ci.yml` 在 main、PR 和版本标签上验证并构建；只有推送 `v*` 标签会通过 FastDeploy 部署到 `fastrd`。普通提交的 commit message 包含 `v` 不会触发部署。手动运行验证 workflow 也不会直接部署。

普通运行参数直接写在该 workflow 的 `runtime-env`，使用字面 `KEY=value`，不要加 shell 引号。敏感值用该文件 `runtime-secrets` 中的同名仓库 Secrets，使用 `toJSON` 保留特殊字符。SSH 五项来自 FastR-D 组织的 `FASTRD_SSH_*` Secrets；请勿把凭证写入 YAML 或构建产物。

构建 job 上传同一次运行的 `release.tar.gz` 和 `manifest.json`。FastDeploy 和打包器均固定到完整提交 `cc223a0b83b0ef2389974dd70a4b74148cd1af87`，服务器不拉源码或重新安装依赖。代码相对路径由 [FastDeploy 应用配置](https://github.com/FastR-D/FastDeploy/blob/cc223a0b83b0ef2389974dd70a4b74148cd1af87/config/apps.json) 注入，业务数据使用其中记录的原持久化路径。

发布示例（版本号每次必须唯一）：

```bash
git tag v2026.11.11.1
git push origin v2026.11.11.1
```

生产 Environment 仅允许 `v*` 标签和 main 上的手动回滚。切换后自动检查就绪，失败恢复旧代码和运行配置；数据库不会随代码回滚。需要回滚时，在 main 手动执行 `.github/workflows/rollback.yml`，输入部署日志中的准确当前 release ID；若当前版本已变化则拒绝执行。

适配 main 提交不会重启当前业务。首个生产标签仍需验证登录、集成功能及数据兼容性。完整运行时、共享目录、旧服务接管和检查限制见 [FastDeploy README](https://github.com/FastR-D/FastDeploy#readme)。

该项目首次标签才创建用户级服务，当前仅监听服务器 loopback，未配置新的公网 Nginx 路由。
