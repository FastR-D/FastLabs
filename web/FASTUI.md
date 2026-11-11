# FastUI 0.1.2

部署前运行 `python web/supply_fastui.py`。离线供应：`python web/supply_fastui.py --artifact /path/to/ui-foundation-0.1.2.tgz`。脚本从固定官方 npm tarball URL 下载或读取同字节 tarball，校验 SHA-256 后生成 `web/fastui/`，含版本、包摘要、逐文件摘要和许可证；供应目录不手工编辑。升级时从公开 registry metadata 确认实际 URL，并下载核准版本和完整摘要。

提交/部署生成目录后，服务运行只读取本地文件，不需要 npm/CDN。颜色模式初始化在 HTML 内容之前执行，`fastlabs-theme` 未配置时跟随系统；共享控件通过显式 `fastui-*` 类 opt-in，宿主业务布局使用共享 token。任务表单字段、事件委托和调度状态不变。
