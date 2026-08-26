"""Feishu long-connection entry point for FastLab.

Credentials never enter FastLab's database; this module only reads them from
the process environment.
"""

import asyncio
import os
import re
import threading
from datetime import datetime, timezone

from feishu_help import concise_help, detailed_help_card


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _attr(value, *names, default=None):
    current = value
    for name in names:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(name)
        else:
            current = getattr(current, name, None)
    return default if current is None else current


class FeishuGateway:
    def __init__(self, app):
        self.app = app
        self.app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        allowlist = os.environ.get("FASTLAB_FEISHU_ALLOWED_OPEN_IDS", "")
        self.allowed_open_ids = {item.strip() for item in allowlist.split(",") if item.strip()}
        self.configured = bool(self.app_id and self.app_secret)
        self.connected = False
        self.last_connected_at = None
        self.last_error = None
        self._channel = None
        self._loop = None
        self._stop_event = None
        self._thread = None
        self._stopping = False

    def status(self):
        return {
            "configured": self.configured,
            "connected": self.connected,
            "lastConnectedAt": self.last_connected_at,
            "error": self._safe_error(self.last_error),
            "allowlistCount": len(self.allowed_open_ids),
        }

    def start(self):
        if not self.configured:
            return
        if not self.allowed_open_ids:
            self.last_error = "已配置飞书凭证，但 FASTLAB_FEISHU_ALLOWED_OPEN_IDS 为空。"
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="fastlab-feishu")
        self._thread.start()

    def _thread_main(self):
        try:
            # lark-channel-sdk 1.2 captures an event loop while its WebSocket
            # module is imported. Import it before asyncio.run() starts this
            # thread's application loop, otherwise the SDK later tries to run
            # an already-running loop from its worker thread.
            channel_classes = self._load_sdk()
            asyncio.run(self._run(*channel_classes))
        except Exception as exc:
            self.connected = False
            self.last_error = self._safe_error(exc)

    @staticmethod
    def _load_sdk():
        try:
            from lark_channel import FeishuChannel, PolicyConfig
        except ImportError:
            try:
                from lark_oapi.channel import FeishuChannel, PolicyConfig
            except ImportError as exc:
                raise RuntimeError(
                    "缺少 lark-channel-sdk；请执行 `python -m pip install -r requirements.txt`。"
                ) from exc
        return FeishuChannel, PolicyConfig

    def _safe_error(self, value):
        text = str(value or "")
        if self.app_secret:
            text = text.replace(self.app_secret, "[redacted]")
        return text or None

    async def _run(self, FeishuChannel, PolicyConfig):
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        policy = PolicyConfig(
            dm_policy="allowlist",
            allow_from=sorted(self.allowed_open_ids),
            require_mention=True,
        )
        self._channel = FeishuChannel(
            app_id=self.app_id,
            app_secret=self.app_secret,
            transport="ws",
            policy=policy,
        )
        self._channel.on("message", self._on_message)
        self._channel.on("cardAction", self._on_card_action)
        self._channel.on("reconnected", self._on_connected)
        self._channel.on("error", self._on_error)
        outbox = None
        try:
            # connect() intentionally blocks for the lifetime of a WebSocket
            # connection. start_background() returns after the handshake, so
            # connection status and the notification outbox are only enabled
            # once the transport is genuinely ready.
            await self._channel.start_background(timeout=30)
            self.connected = True
            self.last_connected_at = _now()
            self.last_error = None
            outbox = asyncio.create_task(self._outbox_loop())
            await self._stop_event.wait()
        finally:
            self.connected = False
            if outbox is not None:
                outbox.cancel()
                try:
                    await outbox
                except asyncio.CancelledError:
                    pass

    def _on_connected(self, *_args):
        self.connected = True
        self.last_connected_at = _now()
        self.last_error = None

    async def _on_error(self, error):
        self.last_error = self._safe_error(error)

    async def _on_message(self, message):
        event_id = str(_attr(message, "message_id", default="") or _attr(message, "id", default=""))
        if not event_id or not self.app.store.claim_inbound_event("feishu", event_id, "message"):
            return
        sender_id = str(_attr(message, "sender_id", default=""))
        chat_id = str(_attr(message, "chat_id", default=""))
        if sender_id not in self.allowed_open_ids:
            return
        if str(_attr(message, "chat_type", default="")) != "p2p" and not bool(
            _attr(message, "mentioned_bot", default=False)
        ):
            return
        text = str(_attr(message, "content_text", default="") or "").strip()
        if not text:
            await self._reply(chat_id, "只支持文本命令。", event_id)
            return
        try:
            result = self.handle_command(
                text,
                {"conversationId": chat_id, "operatorId": sender_id, "messageId": event_id},
            )
            await self._reply(chat_id, result, event_id)
        except Exception as exc:
            await self._reply(chat_id, "命令未执行：%s" % exc, event_id)

    def handle_command(self, text, context):
        text = re.sub(r"^@\S+\s*", "", text.strip())
        first, _, rest = text.partition(" ")
        command = first.strip()
        arguments = rest.strip()
        if command == "帮助":
            if not arguments:
                return concise_help()
            if arguments == "详细":
                return {"card": detailed_help_card()}
            raise ValueError("用法：帮助 或 帮助 详细")
        if command == "仓库":
            if arguments:
                raise ValueError("用法：仓库")
            return self._repository_summary()
        if command == "执行器":
            if arguments:
                raise ValueError("用法：执行器")
            return self._executor_summary()
        if command == "创建":
            return self._create_from_command(arguments, context)
        if command == "状态":
            task = self._task_by_short_id(arguments)
            return self._task_summary(task)
        if command == "确认":
            task = self._task_by_short_id(arguments)
            self.app.start_task(task["id"])
            return "任务 %s 已开始执行。" % task["id"][:8].upper()
        if command == "取消":
            task = self._task_by_short_id(arguments)
            self.app.cancel_task(task["id"])
            return "已请求停止任务 %s。" % task["id"][:8].upper()
        if command == "调整":
            short_id, separator, feedback = arguments.partition(" ")
            if not separator or not feedback.strip():
                raise ValueError("用法：调整 <任务ID> <反馈>")
            task = self._task_by_short_id(short_id)
            self.app.replan_task(task["id"], feedback.strip())
            return "已按反馈重新规划任务 %s。" % task["id"][:8].upper()
        if command == "继续":
            short_id, separator, message = arguments.partition(" ")
            if not separator or not message.strip():
                raise ValueError("用法：继续 <任务ID> <追加要求>")
            task = self._task_by_short_id(short_id)
            continued = self.app.continue_task(
                task["id"],
                message.strip(),
                channel_context={"channel": "feishu", **context},
            )
            return "已从任务 %s 创建继续任务 %s。" % (
                task["id"][:8].upper(), continued["id"][:8].upper()
            )
        if command == "分配":
            head, *options = [item.strip() for item in arguments.split("|")]
            parts = head.split(" ", 2)
            if len(parts) != 3 or not all(parts):
                raise ValueError(
                    "用法：分配 <任务ID> <子任务> <Codex|Claude> [| 模型 | 推理等级]"
                )
            task = self._task_by_short_id(parts[0])
            subtask = next((item for item in task["subtasks"]
                            if item["plan_key"].upper() == parts[1].upper()), None)
            if not subtask:
                raise ValueError("找不到子任务 %s。" % parts[1])
            executor = self._executor_by_name(parts[2])
            self.app.update_subtask_executor(
                subtask["id"], executor["id"],
                options[0] if options else None,
                options[1] if len(options) > 1 else None,
            )
            return "%s 已分配给 %s。" % (subtask["plan_key"], executor["name"])
        if command == "验收":
            head, *options = [item.strip() for item in arguments.split("|")]
            task_id, separator, executor_name = head.partition(" ")
            if not separator or not executor_name.strip():
                raise ValueError(
                    "用法：验收 <任务ID> <Codex|Claude> [| 模型 | 推理等级]"
                )
            task = self._task_by_short_id(task_id)
            executor = self._executor_by_name(executor_name)
            self.app.update_task_verifier(
                task["id"], executor["id"],
                options[0] if options else None,
                options[1] if len(options) > 1 else None,
            )
            return "任务 %s 将由 %s 完成最终验收。" % (
                task["id"][:8].upper(), executor["name"]
            )
        if command in {"追加", "重试"}:
            parts = arguments.split(" ", 2)
            minimum = 3 if command == "追加" else 2
            if len(parts) < minimum:
                raise ValueError("用法：%s <任务ID> <子任务> %s" % (
                    command, "<说明>" if command == "追加" else ""
                ))
            task = self._task_by_short_id(parts[0])
            subtask = next(
                (item for item in task["subtasks"] if item["plan_key"].upper() == parts[1].upper()),
                None,
            )
            if not subtask:
                raise ValueError("找不到子任务 %s。" % parts[1])
            if command == "追加":
                try:
                    self.app.message_subtask(subtask["id"], parts[2])
                except ValueError as exc:
                    raise ValueError(
                        "%s 若原 Agent 现场已清理，请改用：继续 <任务ID> <追加要求>。"
                        % exc
                    ) from exc
                return "已将说明加入 %s 的调度队列。" % subtask["plan_key"]
            try:
                self.app.retry_subtask(subtask["id"])
            except ValueError as exc:
                if task.get("delivered_commit"):
                    guidance = "任务已交付，请使用：继续 <任务ID> <追加要求>。"
                elif subtask.get("session_id") and subtask.get("worktree"):
                    guidance = "若要补充新要求，请使用：追加 <任务ID> <子任务> <说明>。"
                else:
                    guidance = "重试只适用于失败、阻塞或取消的子任务。"
                raise ValueError("%s %s" % (exc, guidance)) from exc
            return "已安排重试 %s。" % subtask["plan_key"]
        return "未识别命令。\n\n" + concise_help()

    def _repository_summary(self):
        repositories = self.app.list_repositories()
        if not repositories:
            return "尚未登记仓库。请在运行 FastLab 的电脑上打开网页设置并添加仓库。"
        rows = ["可用仓库："]
        for repository in repositories:
            labels = []
            if repository.get("is_default"):
                labels.append("默认")
            if not repository.get("available", True):
                labels.append("不可用")
            suffix = "（%s）" % "、".join(labels) if labels else ""
            rows.append("• %s%s" % (repository["alias"], suffix))
        rows.append("创建格式：创建 <仓库别名> <目标>")
        return "\n".join(rows)

    def _executor_summary(self):
        rows = ["可用执行器："]
        for executor in self.app.list_executors():
            if executor["available"]:
                rows.append("• %s" % executor["name"])
        if len(rows) == 1:
            rows.append("暂无可用执行器，请检查 Codex 登录或 Claude Code 安装。")
        return "\n".join(rows)

    def _executor_by_name(self, value):
        needle = str(value or "").strip().casefold()
        aliases = {"claude code": "claude", "claude": "claude", "codex": "codex"}
        executor_id = aliases.get(needle, needle)
        matches = [item for item in self.app.list_executors()
                   if item["available"] and item["id"] == executor_id]
        if len(matches) != 1:
            raise ValueError("执行器不可用；请先发送“执行器”查看。")
        return matches[0]

    def _create_from_command(self, arguments, context):
        lines = [line.strip() for line in arguments.splitlines() if line.strip()]
        if not lines:
            raise ValueError("用法：创建 <仓库别名>，并另起一行填写目标。")
        alias, _, inline_goal = lines[0].partition(" ")
        fields = {
            "目标": inline_goal.strip(), "限制": "",
            "并发": str(self.app.agent_gate.snapshot()["limit"]),
            "标题": "", "目录": ""
        }
        for line in lines[1:]:
            key, separator, value = re.sub(r"：", ":", line).partition(":")
            if separator and key in fields:
                fields[key] = value.strip()
            elif not fields["目标"]:
                fields["目标"] = line
            else:
                fields["目标"] += "\n" + line
        repository = self.app.store.find_repository(alias)
        if not repository:
            aliases = ", ".join(item["alias"] for item in self.app.list_repositories())
            raise ValueError("未知仓库别名。可选：%s" % (aliases or "无"))
        task = self.app.create_task(
            fields["标题"], fields["目标"], fields["限制"], fields["并发"],
            repository_id=repository["id"], source_channel="feishu",
            channel_context={"channel": "feishu", **context},
            working_subdir=fields["目录"],
        )
        return (
            "任务 %s 已创建，FastLab 正在用 orchestration Skill 生成计划并分配执行器；完成后请选择验收执行器。"
            % task["id"][:8].upper()
        )

    def _task_by_short_id(self, value):
        prefix = re.sub(r"[^a-fA-F0-9]", "", str(value or ""))
        if len(prefix) < 4:
            raise ValueError("请提供至少 4 位任务 ID。")
        matches = [task for task in self.app.list_payload() if task["id"].startswith(prefix.lower())]
        if len(matches) != 1:
            raise ValueError("找不到唯一匹配的任务。")
        return self.app.task_payload(matches[0]["id"])

    @staticmethod
    def _task_summary(task):
        done = sum(1 for item in task["subtasks"] if item["status"] == "succeeded")
        return "任务 %s · %s\n仓库：%s\n目录：%s\n状态：%s\n进度：%s%%（%s/%s 子任务）%s" % (
            task["id"][:8].upper(), task["title"], task.get("repository_alias", "未知"),
            task.get("working_subdir") or "仓库根目录", task["status"], task["progress"], done,
            len(task["subtasks"]), "\n问题：" + task["error"] if task.get("error") else "",
        )

    async def _on_card_action(self, event):
        value = _attr(event, "value", default=None) or _attr(event, "action", "value", default={}) or {}
        action = str(value.get("action", "")) if isinstance(value, dict) else ""
        task_id = str(value.get("taskId", "")) if isinstance(value, dict) else ""
        raw = _attr(event, "raw", default={}) or {}
        raw_event_id = ""
        if isinstance(raw, dict):
            raw_event_id = str(
                raw.get("event_id")
                or (raw.get("header") or {}).get("event_id")
                or (raw.get("event") or {}).get("event_id")
                or ""
            )
        event_id = str(
            _attr(event, "event_id", default="")
            or raw_event_id
            or _attr(event, "open_message_id", default="")
            or "%s:%s:%s:%s" % (
                _attr(event, "message_id", default=""), task_id, action,
                _attr(event, "operator", "open_id", default=""),
            )
        )
        if not event_id or not self.app.store.claim_inbound_event("feishu", event_id, action, task_id):
            return {"toast": {"type": "info", "content": "操作已处理"}}
        operator = str(
            _attr(event, "operator", "open_id", default="")
            or _attr(event, "operator_id", default="")
        )
        if operator not in self.allowed_open_ids:
            return {"toast": {"type": "error", "content": "没有操作权限"}}
        try:
            if action == "start":
                self.app.start_task(task_id)
            elif action == "cancel":
                self.app.cancel_task(task_id)
            elif action == "verify":
                self.app.verify_task(task_id)
            elif action == "retry":
                self.app.retry_subtask(str(value.get("subtaskId", "")))
            else:
                raise ValueError("未知操作。")
            return {"toast": {"type": "success", "content": "FastLab 已接收操作"}}
        except Exception as exc:
            return {"toast": {"type": "error", "content": str(exc)[:80]}}

    async def _reply(self, chat_id, text, reply_to=None):
        if not self._channel:
            return None
        options = {"reply_to": reply_to} if reply_to else None
        message = text if isinstance(text, dict) else {"markdown": str(text)}
        return await self._channel.send(chat_id, message, options)

    async def _outbox_loop(self):
        while not self._stopping:
            for item in self.app.store.pending_outbox():
                if item["channel"] != "feishu":
                    self.app.store.finish_outbox(item["id"], "不支持的通知渠道。")
                    continue
                try:
                    task = self.app.task_payload(item["task_id"])
                    card = self._task_card(task)
                    binding = next((entry for entry in self.app.store.channel_bindings(task["id"])
                                    if entry["channel"] == "feishu"
                                    and entry["conversation_id"] == item["destination"]), None)
                    if binding and binding.get("message_id"):
                        try:
                            result = await self._channel.update_card(binding["message_id"], card)
                        except Exception:
                            result = await self._channel.send(item["destination"], {"card": card})
                    else:
                        result = await self._channel.send(item["destination"], {"card": card})
                    if not bool(_attr(result, "success", default=True)):
                        raise RuntimeError(str(_attr(result, "error", default="飞书发送失败")))
                    message_id = str(
                        _attr(result, "message_id", default="")
                        or _attr(result, "data", "message_id", default="")
                    )
                    if message_id:
                        self.app.store.bind_task_channel(
                            task["id"], "feishu", item["destination"], message_id=message_id
                        )
                    self.app.store.finish_outbox(item["id"])
                except Exception as exc:
                    self.app.store.finish_outbox(item["id"], str(exc))
            await asyncio.sleep(1.5)

    @staticmethod
    def _task_card(task):
        actions = []
        if task["status"] == "awaiting_approval":
            ready = bool(task.get("subtasks")) and all(
                item.get("executor") for item in task["subtasks"]
            ) and bool((task.get("role_settings") or {}).get("verifier", {}).get("executor"))
            if ready:
                actions.append((
                    "确认执行", "primary", {"action": "start", "taskId": task["id"]}
                ))
            actions.append(("取消", "default", {"action": "cancel", "taskId": task["id"]}))
        elif task["status"] in {"running", "planning", "verifying"}:
            actions.append(("停止", "danger", {"action": "cancel", "taskId": task["id"]}))
        elif task["status"] == "needs_attention" and all(
            item["status"] == "succeeded" for item in task["subtasks"]
        ):
            actions.append(("重新验收", "primary", {"action": "verify", "taskId": task["id"]}))
        elif task["status"] in {"needs_attention", "failed"}:
            for subtask in task["subtasks"]:
                if subtask["status"] in {"failed", "blocked", "cancelled"}:
                    actions.append((
                        "重试 %s" % subtask["plan_key"],
                        "primary",
                        {"action": "retry", "taskId": task["id"], "subtaskId": subtask["id"]},
                    ))
        elements = [{
            "tag": "markdown",
            "content": "**%s**\n仓库：`%s`\n目录：`%s`\n状态：%s\n进度：%s%%\n任务 ID：`%s`%s" % (
                task["title"], task.get("repository_alias", "未知"),
                task.get("working_subdir") or "仓库根目录", task["status"], task["progress"],
                task["id"][:8].upper(),
                "\n问题：" + task["error"] if task.get("error") else "",
            ),
        }]
        if task["status"] == "awaiting_approval" and task.get("subtasks"):
            rows = []
            for subtask in task["subtasks"]:
                rows.append("- `%s` %s → **%s**" % (
                    subtask["plan_key"], subtask["title"],
                    (subtask.get("executor_snapshot") or {}).get("name") or "待指定",
                ))
            verifier = (task.get("role_settings") or {}).get("verifier", {})
            elements.append({
                "tag": "markdown",
                "content": (
                    "**执行清单**\n" + "\n".join(rows)
                    + "\n\n**验收执行器**：" + (verifier.get("name") or "待指定")
                    + "\n\n发送 `执行器` 查看可用项；用 `分配` 调整计划，用 `验收` 选择最终检查。"
                ),
            })
        if actions:
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": style,
                    "behaviors": [{"type": "callback", "value": value}],
                } for label, style, value in actions],
            })
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "FastLab · 任务更新"},
                "template": "blue" if task["status"] not in {"failed", "needs_attention"} else "orange",
            },
            "body": {"elements": elements},
        }

    def stop(self):
        self._stopping = True
        if self._loop and self._channel:
            try:
                future = asyncio.run_coroutine_threadsafe(self._stop_channel(), self._loop)
                future.result(timeout=4)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4)

    async def _stop_channel(self):
        try:
            if self._channel:
                await self._channel.disconnect()
        finally:
            if self._stop_event:
                self._stop_event.set()
