# 开发指南

## 开发原则

- 只通过根目录兼容入口支持旧调用；新实现导入 `fastlab.*` 聚焦模块。
- 保持 HTTP、SQLite、状态、事件、文档和 Git 外部行为，重构与行为变更分开提交。
- 领域服务不导入 HTTP；Runner 不写数据库；Store 不启动 Agent；前端不复制后端状态机。
- 不在失败路径静默降低安全策略，不用宽泛异常掩盖取消、超时或数据损坏。

## 环境与测试

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
npm test
python deploy/smoke_test.py
```

首次运行浏览器验收时安装 CI 固定版本并下载 Chromium：

```bash
npm install --no-save --no-package-lock @playwright/test@1.63.0
npx playwright install chromium
npm run test:e2e
```

CI 还会在 Python 3.10/3.13、Node 22 上执行 Ruff、coverage、Playwright 和部署 smoke test；
独立的 Linux 发布门禁安装固定 Codex/Claude 后执行强制沙箱测试。
本机沙箱不允许监听端口时 HTTP 测试可以明确 skip；CI 中出现
`sandbox forbids local ports` 会被当作失败。

## 新增 ExecutionRunner

实现 `fastlab.contracts.ExecutionRunner`：

1. `check()` 验证依赖、CLI 版本和安全边界；不满足时失败关闭。
2. `wrap()` 只接受租户 `repositories/` 或 `worktrees/` 中的 `cwd`，返回命令、有效 cwd 和
   白名单环境。
3. `started()` 以运行键登记真实子进程/容器。
4. `interrupt()` 终止完整进程树，且可重复调用。
5. `finished()` 清理单次登记；`shutdown()`/`close()` 清理全部活动运行。

必须测试路径逃逸、环境泄漏、Provider 密钥选择、租户 HOME、超时、取消、父子进程、并发调用、
幂等清理和启动失败。若是弱于 Docker/VM 的后端，在安全文档和 ADR 中明确定位。

## 新增或修改路由

1. 在 HTTP 层解析请求，调用 `FastLab` 兼容门面或应用服务；不要在 Handler 中实现 Git/调度。
2. 复用统一 JSON、错误、认证和同源检查。
3. 更新 [API 契约](api.md)。
4. 增加成功、校验、未知 ID、未认证、CSRF 和 JSON Golden 测试。
5. 前端只在 `web/modules/api.js` 增加 transport，渲染模块不直接 `fetch`。

删除/重命名字段、改变状态码或动作语义不是普通重构，必须先做版本/迁移决策。

## 新增状态或事件

先画出合法入边、出边、取消/重启行为和 UI 展示，再修改领域常量及转换逻辑。至少覆盖：

- 正常转换与非法转换；
- 服务重启恢复；
- 当前 `dispatch_id` 与晚到结果；
- 文档投影、事件 payload 和前端操作按钮；
- 删除、重试、续接、验收和交付的交互。

不要只修改字符串集合；状态是数据库、调度器、API、文档和 UI 的共同契约。

## 新增配置

同时修改并测试：

- `fastlab/config.py` 的环境白名单与解析/校验；
- `fastlab.env.example`；
- [配置参考](configuration.md)；
- systemd/部署文件（若生产相关）；
- 环境白名单一致性测试。

配置默认值必须安全且确定。安全开关只能由管理员环境控制，不在网页暴露。

## Store、文档和 Git

- Store 变更先用旧数据库 fixture 证明兼容，再考虑迁移。
- 文档格式用 Golden 测试，保证重建幂等。
- Git 命令必须显式指定 cwd/参数，不拼 shell；路径经 `resolve` 后验证所属租户与工作子目录。
- 新清理逻辑只处理数据库精确记录的分支/Worktree，禁止通配符删除。
- 交付测试必须证明任何保护失败都不修改目标工作树。

## 前端

前端保持无构建链原生 ES 模块：

- `api.js`：HTTP 与错误归一化；
- `state.js`：客户端状态、轮询和编辑保护；
- `presentation.js`：纯格式化/视图模型；
- `render.js`：DOM 渲染；
- `events.js`：事件委托和命令；
- `app.js`：启动与组合。

纯模块用 Node `node:test`，核心浏览器流程用 Playwright。轮询不得覆盖正在编辑的表单、展开状态、
页面位置和内部滚动位置。

## 提交前检查

```bash
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
npm test
npm run test:e2e
python deploy/smoke_test.py
git diff --check
```

再确认变更只位于 FastLab，且没有提交 `.fastlab/`、API Key、环境文件、数据库或任务 Worktree。
