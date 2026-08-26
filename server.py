#!/usr/bin/env python3
"""FastLab: a local multi-Agent task gateway for the web and Feishu."""

import argparse
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

from agent_adapter import (
    AgentAdapter,
    ClaudeCLIAdapter,
    ClaudePlanner,
    CodexCLIAdapter,
    CodexPlanner,
    Planner,
    RESUME_CAPABILITY,
    VERIFY_CAPABILITY,
    WRITE_CAPABILITY,
)
from feishu_gateway import FeishuGateway
from feishu_help import help_payload as feishu_help_payload


APP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = APP_ROOT / "web"
PLANNER_SCHEMA = APP_ROOT / "schemas" / "planner.schema.json"
VERIFIER_SCHEMA = APP_ROOT / "schemas" / "verifier.schema.json"
PLANNER_SKILL = APP_ROOT / "skills" / "orchestration" / "SKILL.md"
ROLE_NAMES = ("planner", "worker", "verifier")
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
TASK_STATES = {
    "planning",
    "awaiting_approval",
    "running",
    "verifying",
    "completed",
    "needs_attention",
    "failed",
    "cancelled",
}
SUBTASK_STATES = {"pending", "running", "succeeded", "failed", "blocked", "cancelled"}
LOCAL_ENV_KEYS = {
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FASTLAB_FEISHU_ALLOWED_OPEN_IDS",
    "FASTLAB_FEISHU_DEFAULT_CHAT_ID",
    "FASTLAB_PLANNER_BACKEND",
    "FASTLAB_CODEX_BIN",
    "FASTLAB_CLAUDE_BIN",
    "FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS",
    "FASTLAB_AGENT_TIMEOUT",
}
DEFAULT_GLOBAL_CONCURRENCY = 4
MAX_GLOBAL_CONCURRENCY = 32
DEFAULT_AGENT_TIMEOUT = 3600
EXECUTOR_LABELS = {"codex": "Codex", "claude": "Claude Code"}
MAX_EVENT_PAYLOAD = 20000


class RepositoryInitializationRequired(ValueError):
    """Signal that repository setup needs an explicit user confirmation."""

    def __init__(self, path, reason):
        self.path = str(path)
        self.reason = reason
        if reason == "not_git":
            message = "该文件夹不是 Git 仓库。是否初始化并创建初始提交？"
        else:
            message = "该 Git 仓库还没有任何提交。是否创建初始提交？"
        super().__init__(message)


class DeliveryReverificationRequired(RuntimeError):
    """Signal that target-side commits were reconciled and must be verified."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_error(value, limit=1200):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[-limit:]


def slug(value, limit=52):
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
    return (cleaned or "task")[:limit]


def resolve_executable(value):
    """Resolve commands once so Windows PATHEXT launchers (for example .cmd) work."""
    raw = os.path.expanduser(str(value or "").strip())
    if not raw:
        return None
    if os.path.dirname(raw) or Path(raw).is_absolute():
        candidate = Path(raw)
        return str(candidate.resolve()) if candidate.is_file() else None
    return shutil.which(raw)


def executable_command(binary):
    """Use the current interpreter for Python command doubles on every platform."""
    binary = str(binary)
    if Path(binary).suffix.lower() == ".py":
        return [sys.executable, binary]
    return [binary]


def process_group_options(platform=None):
    platform = os.name if platform is None else platform
    if platform == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def load_local_env(path, required=False):
    """Load FastLab's small, explicit environment-file surface.

    Existing process environment values win, which keeps one-off overrides
    possible without changing the file. Values are never logged.
    """
    path = Path(path).expanduser()
    if not path.exists():
        if required:
            raise ValueError("找不到环境配置文件：%s" % path)
        return []
    if not path.is_file():
        raise ValueError("环境配置路径不是文件：%s" % path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        print(
            "警告：%s 可被其他用户读取，建议执行 chmod 600。" % path,
            file=sys.stderr,
        )
    loaded = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError("%s:%s 不是有效的 KEY=VALUE 配置。" % (path, line_number))
        if key.startswith("FASTLAB_PLANNER_") and key != "FASTLAB_PLANNER_BACKEND":
            continue
        if key not in LOCAL_ENV_KEYS:
            raise ValueError("%s:%s 不支持配置 %s。" % (path, line_number, key))
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def parse_claude_extra_allowed_tools(value):
    raw = str(value or "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS 必须是 JSON 字符串数组。"
        ) from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ValueError(
            "FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS 必须是非空字符串组成的 JSON 数组。"
        )
    tools = []
    for item in parsed:
        rule = item.strip()
        if rule in {"Bash", "Bash(*)"}:
            raise ValueError(
                "FastLab 不允许通过环境变量开放全部 Bash；请配置具体命令规则。"
            )
        if "\n" in rule or "\r" in rule:
            raise ValueError("Claude 命令权限规则不能包含换行。")
        if rule not in tools:
            tools.append(rule)
    return tuple(tools)


class AgentRunCancelled(RuntimeError):
    pass


class AgentRunTimeout(RuntimeError):
    pass


def parse_agent_timeout(value):
    raw = str(DEFAULT_AGENT_TIMEOUT if value is None else value).strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("FASTLAB_AGENT_TIMEOUT 必须是大于等于 0 的秒数。") from exc
    if timeout < 0:
        raise ValueError("FASTLAB_AGENT_TIMEOUT 必须是大于等于 0 的秒数。")
    return timeout


class AgentRunGate:
    """Cancellation-aware global concurrency limiter."""

    def __init__(self, limit=DEFAULT_GLOBAL_CONCURRENCY):
        self.limit = max(1, min(MAX_GLOBAL_CONCURRENCY, int(limit)))
        self.active = 0
        self.waiters = deque()
        self.condition = threading.Condition()

    def set_limit(self, limit):
        with self.condition:
            self.limit = max(1, min(MAX_GLOBAL_CONCURRENCY, int(limit)))
            self.condition.notify_all()

    def acquire(self, cancelled):
        token = {"id": object()}
        with self.condition:
            self.waiters.append(token)
            while True:
                if cancelled():
                    try:
                        self.waiters.remove(token)
                    except ValueError:
                        pass
                    self.condition.notify_all()
                    raise AgentRunCancelled("Agent 运行在排队期间被取消。")
                if self.waiters[0] is token and self.active < self.limit:
                    self.waiters.remove(token)
                    self.active += 1
                    return
                self.condition.wait(timeout=0.1)

    def release(self):
        with self.condition:
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    def wake_all(self):
        with self.condition:
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {
                "active": self.active,
                "queued": len(self.waiters),
                "limit": self.limit,
            }


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.repaired_event_payloads = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    path TEXT NOT NULL UNIQUE,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    parent_task_id TEXT,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    constraints_text TEXT NOT NULL DEFAULT '',
                    workspace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    max_concurrency INTEGER NOT NULL DEFAULT 3,
                    base_branch TEXT,
                    base_commit TEXT,
                    integration_branch TEXT,
                    integration_worktree TEXT,
                    delivered_commit TEXT,
                    delivered_at TEXT,
                    cleaned_at TEXT,
                    plan_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subtasks (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    plan_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    weight INTEGER NOT NULL DEFAULT 1,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    branch TEXT,
                    worktree TEXT,
                    session_id TEXT,
                    dispatch_id TEXT,
                    exit_code INTEGER,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, plan_key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    subtask_id TEXT,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT,
                    source_message_id TEXT,
                    operator_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, channel, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS inbound_events (
                    channel TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    action TEXT,
                    task_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(channel, event_id)
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON notification_outbox(status, id);
                """
            )
            self._ensure_column(db, "tasks", "repository_id", "TEXT")
            self._ensure_column(db, "tasks", "parent_task_id", "TEXT")
            self._ensure_column(db, "tasks", "working_subdir", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "tasks", "source_channel", "TEXT NOT NULL DEFAULT 'web'")
            self._ensure_column(db, "tasks", "role_settings_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "tasks", "planner_thread_id", "TEXT")
            self._ensure_column(db, "tasks", "verifier_thread_id", "TEXT")
            self._ensure_column(db, "tasks", "delivered_commit", "TEXT")
            self._ensure_column(db, "tasks", "delivered_at", "TEXT")
            self._ensure_column(db, "tasks", "cleaned_at", "TEXT")
            self._ensure_column(db, "subtasks", "model", "TEXT")
            self._ensure_column(db, "subtasks", "reasoning_effort", "TEXT")
            self._ensure_column(db, "subtasks", "pending_message", "TEXT")
            self._ensure_column(db, "subtasks", "dispatch_id", "TEXT")
            self._ensure_column(db, "subtasks", "executor", "TEXT")
            self._ensure_column(db, "subtasks", "executor_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
            legacy_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(subtasks)")
            }
            if "agent_profile_id" in legacy_columns:
                db.execute(
                    "UPDATE subtasks SET executor=CASE "
                    "WHEN agent_profile_id IN ('builtin-codex','codex') THEN 'codex' "
                    "WHEN agent_profile_id IN ('builtin-claude','claude') THEN 'claude' "
                    "ELSE executor END WHERE executor IS NULL"
                )
            self._ensure_column(db, "channel_bindings", "source_message_id", "TEXT")
            defaults = {
                "models": {
                    "planner": {"model": "", "reasoningEffort": "high"},
                    "worker": {"model": "", "reasoningEffort": "high"},
                    "verifier": {"model": "", "reasoningEffort": "high"},
                },
                "feishu.defaultConversation": "",
                "agent.globalConcurrency": DEFAULT_GLOBAL_CONCURRENCY,
            }
            now = utc_now()
            for key, value in defaults.items():
                db.execute(
                    "INSERT OR IGNORE INTO settings (key,value_json,updated_at) VALUES (?,?,?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            self.repaired_event_payloads = self._repair_event_payloads(db)
            interrupted = db.execute(
                "SELECT id FROM tasks WHERE status IN ('planning', 'running', 'verifying')"
            ).fetchall()
            for row in interrupted:
                db.execute(
                    "UPDATE tasks SET status='needs_attention', error=?, updated_at=? WHERE id=?",
                    ("服务上次退出时任务仍在运行，请检查后重试。", utc_now(), row["id"]),
                )
                db.execute(
                    "UPDATE subtasks SET status='failed', error=?, updated_at=? "
                    "WHERE task_id=? AND status='running'",
                    ("服务退出导致执行中断。", utc_now(), row["id"]),
                )

    @staticmethod
    def _ensure_column(db, table, column, definition):
        existing = {row["name"] for row in db.execute("PRAGMA table_info(%s)" % table)}
        if column not in existing:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))

    @staticmethod
    def _load_json(value, label):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("数据库中的 %s 不是有效 JSON：%s" % (label, exc)) from exc

    @staticmethod
    def _encode_event_payload(payload):
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded) <= MAX_EVENT_PAYLOAD:
            return encoded
        return json.dumps(
            {
                "_fastlabTruncated": True,
                "originalLength": len(encoded),
                "preview": encoded[:MAX_EVENT_PAYLOAD // 4],
            },
            ensure_ascii=False,
        )

    @classmethod
    def _repair_event_payloads(cls, db):
        repaired = 0
        rows = db.execute(
            "SELECT id,payload_json FROM events WHERE payload_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                replacement = cls._encode_event_payload({
                    "_fastlabRecovered": True,
                    "reason": "旧版本截断了日志 JSON",
                    "preview": str(row["payload_json"])[:MAX_EVENT_PAYLOAD // 4],
                })
                db.execute(
                    "UPDATE events SET payload_json=? WHERE id=?",
                    (replacement, row["id"]),
                )
                repaired += 1
        return repaired

    def create_task(self, title, goal, constraints_text, workspace, max_concurrency,
                    repository_id=None, source_channel="web", role_settings=None,
                    working_subdir="", parent_task_id=None, base_branch=None,
                    base_commit=None):
        task_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks "
                "(id,parent_task_id,title,goal,constraints_text,workspace,status,max_concurrency,"
                "repository_id,source_channel,role_settings_json,working_subdir,base_branch,"
                "base_commit,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'planning',?,?,?,?,?,?,?,?,?)",
                (
                    task_id, parent_task_id, title, goal, constraints_text, workspace,
                    max_concurrency,
                    repository_id, source_channel, json.dumps(role_settings or {}, ensure_ascii=False),
                    working_subdir, base_branch, base_commit, now, now,
                ),
            )
        return task_id

    def update_task(self, task_id, **values):
        allowed = {
            "title",
            "status",
            "base_branch",
            "base_commit",
            "integration_branch",
            "integration_worktree",
            "delivered_commit",
            "delivered_at",
            "cleaned_at",
            "plan_json",
            "error",
            "cancel_requested",
            "planner_thread_id",
            "verifier_thread_id",
            "repository_id",
            "role_settings_json",
            "working_subdir",
            "parent_task_id",
        }
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        statement = ", ".join("%s=?" % key for key, _ in pairs)
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET %s WHERE id=?" % statement,
                [value for _, value in pairs] + [task_id],
            )

    def update_subtask(self, subtask_id, **values):
        allowed = {
            "title",
            "instructions",
            "status",
            "branch",
            "worktree",
            "session_id",
            "exit_code",
            "attempt",
            "error",
            "model",
            "reasoning_effort",
            "pending_message",
            "dispatch_id",
            "executor",
            "executor_snapshot_json",
        }
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        statement = ", ".join("%s=?" % key for key, _ in pairs)
        with self.connect() as db:
            db.execute(
                "UPDATE subtasks SET %s WHERE id=?" % statement,
                [value for _, value in pairs] + [subtask_id],
            )

    def update_subtask_for_dispatch(self, subtask_id, dispatch_id, **values):
        """Fence a Worker result to its currently running Dispatch."""
        allowed = {
            "status", "session_id", "exit_code", "error", "pending_message",
        }
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return False
        pairs.append(("updated_at", utc_now()))
        statement = ", ".join("%s=?" % key for key, _ in pairs)
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE subtasks SET %s WHERE id=? AND dispatch_id=? AND status='running'"
                % statement,
                [value for _, value in pairs] + [subtask_id, dispatch_id],
            )
            return bool(cursor.rowcount)

    def replace_subtasks(self, task_id, subtasks):
        now = utc_now()
        with self.connect() as db:
            db.execute("DELETE FROM subtasks WHERE task_id=?", (task_id,))
            for ordinal, item in enumerate(subtasks, 1):
                db.execute(
                    "INSERT INTO subtasks "
                    "(id,task_id,plan_key,ordinal,title,instructions,weight,dependencies_json,"
                    "executor,executor_snapshot_json,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?, 'pending',?,?)",
                    (
                        "%s:%s" % (task_id, item["key"]),
                        task_id,
                        item["key"],
                        ordinal,
                        item["title"],
                        item["instructions"],
                        item["weight"],
                        json.dumps(item["dependencies"], ensure_ascii=False),
                        item.get("executor"),
                        "{}",
                        now,
                        now,
                    ),
                )

    def add_event(self, task_id, kind, message, subtask_id=None, payload=None):
        encoded = self._encode_event_payload(payload) if payload is not None else None
        with self.connect() as db:
            db.execute(
                "INSERT INTO events (task_id,subtask_id,kind,message,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, subtask_id, kind, compact_error(message, 8000), encoded, utc_now()),
            )
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (utc_now(), task_id))

    def get_task(self, task_id, include_events=True):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            task["plan"] = self._load_json(
                task.pop("plan_json"), "任务 %s 的计划" % task_id
            ) if task.get("plan_json") else None
            task["role_settings"] = self._load_json(
                task.pop("role_settings_json") or "{}", "任务 %s 的角色设置" % task_id
            )
            task["cancel_requested"] = bool(task["cancel_requested"])
            children = db.execute(
                "SELECT * FROM subtasks WHERE task_id=? ORDER BY ordinal", (task_id,)
            ).fetchall()
            task["subtasks"] = []
            for child in children:
                item = dict(child)
                item["dependencies"] = self._load_json(
                    item.pop("dependencies_json"), "子任务 %s 的依赖" % item["id"]
                )
                item["executor_snapshot"] = self._load_json(
                    item.pop("executor_snapshot_json") or "{}",
                    "子任务 %s 的执行器设置" % item["id"],
                )
                task["subtasks"].append(item)
            task["events"] = []
            if include_events:
                rows = db.execute(
                    "SELECT * FROM (SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 300) "
                    "ORDER BY id",
                    (task_id,),
                ).fetchall()
                for event in rows:
                    item = dict(event)
                    item["payload"] = self._load_json(
                        item.pop("payload_json"), "日志事件 %s" % item["id"]
                    ) if item.get("payload_json") else None
                    task["events"].append(item)
            task["progress"] = self.progress(task["subtasks"])
            return task

    def list_tasks(self):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self.get_task(row["id"], include_events=False) for row in rows]

    def set_setting(self, key, value):
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings (key,value_json,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    def get_setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return self._load_json(row["value_json"], "设置 %s" % key) if row else default

    def add_repository(self, alias, path, is_default=False, repository_id=None):
        now = utc_now()
        repository_id = repository_id or uuid.uuid4().hex
        with self.connect() as db:
            if is_default:
                db.execute("UPDATE repositories SET is_default=0")
            db.execute(
                "INSERT INTO repositories (id,alias,path,is_default,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (repository_id, alias, path, int(bool(is_default)), now, now),
            )
        return self.get_repository(repository_id)

    def update_repository(self, repository_id, alias, path, is_default=False):
        now = utc_now()
        with self.connect() as db:
            if is_default:
                db.execute("UPDATE repositories SET is_default=0")
            cursor = db.execute(
                "UPDATE repositories SET alias=?,path=?,is_default=?,updated_at=? WHERE id=?",
                (alias, path, int(bool(is_default)), now, repository_id),
            )
            if not cursor.rowcount:
                raise KeyError("找不到仓库。")
        return self.get_repository(repository_id)

    def get_repository(self, repository_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["is_default"] = bool(item["is_default"])
        return item

    def find_repository(self, value):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM repositories WHERE id=? OR alias=? COLLATE NOCASE OR path=?",
                (str(value), str(value), str(value)),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["is_default"] = bool(item["is_default"])
        return item

    def list_repositories(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM repositories ORDER BY is_default DESC, alias COLLATE NOCASE"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["is_default"] = bool(item["is_default"])
            items.append(item)
        return items

    def delete_repository(self, repository_id):
        with self.connect() as db:
            active = db.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE repository_id=? "
                "AND status NOT IN ('completed','cancelled','failed')",
                (repository_id,),
            ).fetchone()["total"]
            if active:
                raise ValueError("该仓库仍有未结束任务，不能删除。")
            cursor = db.execute("DELETE FROM repositories WHERE id=?", (repository_id,))
            if not cursor.rowcount:
                raise KeyError("找不到仓库。")

    def clear_task_events(self, task_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError("找不到任务。")
            db.execute("DELETE FROM events WHERE task_id=?", (task_id,))
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (utc_now(), task_id))

    def delete_task(self, task_id):
        with self.connect() as db:
            db.execute("DELETE FROM inbound_events WHERE task_id=?", (task_id,))
            cursor = db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            if not cursor.rowcount:
                raise KeyError("找不到任务。")

    def bind_task_channel(self, task_id, channel, conversation_id, operator_id=None,
                          message_id=None, source_message_id=None):
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO channel_bindings "
                "(task_id,channel,conversation_id,message_id,source_message_id,operator_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(task_id,channel,conversation_id) DO UPDATE SET "
                "message_id=COALESCE(excluded.message_id,channel_bindings.message_id),"
                "source_message_id=COALESCE(excluded.source_message_id,channel_bindings.source_message_id),"
                "operator_id=COALESCE(excluded.operator_id,channel_bindings.operator_id),"
                "updated_at=excluded.updated_at",
                (
                    task_id, channel, conversation_id, message_id, source_message_id,
                    operator_id, now, now,
                ),
            )

    def channel_bindings(self, task_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM channel_bindings WHERE task_id=?", (task_id,)
            )]

    def claim_inbound_event(self, channel, event_id, action=None, task_id=None):
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO inbound_events (channel,event_id,action,task_id,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (channel, event_id, action, task_id, utc_now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def transition_task(self, task_id, status, kind, message, error=None, payload=None,
                        subtask_id=None):
        now = utc_now()
        encoded_payload = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, now, task_id),
            )
            db.execute(
                "INSERT INTO events (task_id,subtask_id,kind,message,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, subtask_id, kind, compact_error(message, 8000), encoded_payload, now),
            )
            destinations = [dict(row) for row in db.execute(
                "SELECT channel,conversation_id FROM channel_bindings WHERE task_id=?", (task_id,)
            )]
            if not destinations:
                default_row = db.execute(
                    "SELECT value_json FROM settings WHERE key='feishu.defaultConversation'"
                ).fetchone()
                default = self._load_json(
                    default_row["value_json"], "设置 feishu.defaultConversation"
                ) if default_row else ""
                if default:
                    destinations.append({"channel": "feishu", "conversation_id": default})
            outbox_payload = json.dumps(
                {"taskId": task_id, "status": status, "kind": kind, "message": message},
                ensure_ascii=False,
            )
            for destination in destinations:
                db.execute(
                    "INSERT INTO notification_outbox "
                    "(task_id,channel,destination,payload_json,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,'pending',?,?)",
                    (task_id, destination["channel"], destination["conversation_id"], outbox_payload, now, now),
                )

    def pending_outbox(self, limit=20):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM notification_outbox WHERE status='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._load_json(
                item.pop("payload_json"), "通知 %s" % item["id"]
            )
            result.append(item)
        return result

    def enqueue_notification(self, task_id, kind, message):
        now = utc_now()
        payload = json.dumps(
            {"taskId": task_id, "kind": kind, "message": message}, ensure_ascii=False
        )
        with self.connect() as db:
            destinations = [dict(row) for row in db.execute(
                "SELECT channel,conversation_id FROM channel_bindings WHERE task_id=?", (task_id,)
            )]
            if not destinations:
                default_row = db.execute(
                    "SELECT value_json FROM settings WHERE key='feishu.defaultConversation'"
                ).fetchone()
                default = self._load_json(
                    default_row["value_json"], "设置 feishu.defaultConversation"
                ) if default_row else ""
                if default:
                    destinations.append({"channel": "feishu", "conversation_id": default})
            for destination in destinations:
                db.execute(
                    "INSERT INTO notification_outbox "
                    "(task_id,channel,destination,payload_json,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,'pending',?,?)",
                    (task_id, destination["channel"], destination["conversation_id"], payload, now, now),
                )

    def finish_outbox(self, item_id, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE notification_outbox SET status=?,attempts=attempts+1,error=?,updated_at=? WHERE id=?",
                ("pending" if error else "sent", compact_error(error) if error else None, utc_now(), item_id),
            )

    @staticmethod
    def progress(subtasks):
        if not subtasks:
            return 0
        total = sum(max(1, int(item["weight"])) for item in subtasks)
        finished = sum(
            max(1, int(item["weight"])) for item in subtasks if item["status"] == "succeeded"
        )
        return round(finished * 100 / total)


class FastLab:
    def __init__(self, workspace=None, data_dir=None, codex_bin=None, agent_adapter=None,
                 claude_bin=None, planner=None):
        self.workspace = None
        self.default_repository_id = None
        self.data_dir = Path(data_dir or APP_ROOT / ".fastlab").expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "fastlab.db")
        default_feishu_conversation = os.environ.get("FASTLAB_FEISHU_DEFAULT_CHAT_ID", "").strip()
        if default_feishu_conversation:
            self.store.set_setting("feishu.defaultConversation", default_feishu_conversation)
        self.merge_lock = threading.Lock()
        self.repository_lock = threading.Lock()
        self.scheduler_lock = threading.Lock()
        self.schedulers = {}
        self.thread_lock = threading.Lock()
        self.background_threads = set()
        self.task_locks_lock = threading.Lock()
        self.task_locks = {}
        self.shutting_down = False
        self.agent_timeout = parse_agent_timeout(os.environ.get("FASTLAB_AGENT_TIMEOUT"))
        self.git_bin = shutil.which("git")
        self._validate_environment()
        self._initialize_repositories(workspace)
        codex_adapter = None
        if agent_adapter is not None:
            if not isinstance(agent_adapter, AgentAdapter):
                required = {"run", "interrupt", "health", "models"}
                if not all(hasattr(agent_adapter, name) for name in required):
                    raise TypeError("agent_adapter 不符合 AgentAdapter 接口。")
            codex_adapter = agent_adapter
        else:
            requested_codex = codex_bin or os.environ.get("FASTLAB_CODEX_BIN") or "codex"
            resolved = resolve_executable(requested_codex)
            if resolved:
                codex_adapter = CodexCLIAdapter(resolved, self.data_dir / "runtime")
        self.codex_bin = getattr(codex_adapter, "binary", None)
        requested_claude = claude_bin or os.environ.get("FASTLAB_CLAUDE_BIN") or "claude"
        self.claude_bin = resolve_executable(requested_claude)
        self.executors = {}
        if codex_adapter:
            self.executors["codex"] = codex_adapter
        if self.claude_bin:
            claude_allowed_tools = list(ClaudeCLIAdapter.DEFAULT_ALLOWED_TOOLS)
            claude_allowed_tools.extend(parse_claude_extra_allowed_tools(
                os.environ.get("FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS")
            ))
            self.executors["claude"] = ClaudeCLIAdapter(
                self.claude_bin,
                allowed_tools=claude_allowed_tools,
                models=["sonnet", "opus"],
            )
        self.planner = planner or self._planner_from_environment()
        if not isinstance(self.planner, Planner) and not all(
            hasattr(self.planner, name) for name in ("plan", "interrupt", "health", "settings")
        ):
            raise TypeError("planner 不符合 Planner 接口。")
        self.active_run_adapters = {}
        self.active_run_lock = threading.Lock()
        global_limit = self._normalize_global_concurrency(
            self.store.get_setting("agent.globalConcurrency", DEFAULT_GLOBAL_CONCURRENCY)
        )
        self.agent_gate = AgentRunGate(global_limit)
        self._rebuild_documents()
        self.feishu = FeishuGateway(self)
        self.feishu.start()

    def _planner_from_environment(self):
        backend = str(os.environ.get("FASTLAB_PLANNER_BACKEND") or "codex").strip().lower()
        model = ""
        if backend == "codex":
            return CodexPlanner(
                self.executors.get("codex"), model, PLANNER_SCHEMA, PLANNER_SKILL,
                effort="high",
            )
        if backend == "claude":
            return ClaudePlanner(
                self.executors.get("claude"), model, PLANNER_SCHEMA, PLANNER_SKILL,
                effort="high",
            )
        raise ValueError(
            "FASTLAB_PLANNER_BACKEND 只能是 codex 或 claude，当前为：%s" % backend
        )

    def _validate_environment(self):
        if self.git_bin is None:
            raise ValueError("找不到 Git，请先安装 Git 并重新打开终端。")

    def _initialize_repositories(self, workspace):
        if workspace:
            resolved = self._resolve_workspace(workspace)
            existing = self.store.find_repository(str(resolved))
            if existing:
                self.store.update_repository(
                    existing["id"], existing["alias"], existing["path"], is_default=True
                )
            else:
                self.store.add_repository(
                    self._available_alias(resolved.name), str(resolved), is_default=True
                )
        self._migrate_task_repositories()
        self._sync_default_repository()

    def _available_alias(self, value):
        base_alias = slug(value, 36)
        alias = base_alias
        index = 2
        while self.store.find_repository(alias):
            alias = "%s-%s" % (base_alias, index)
            index += 1
        return alias

    def _migrate_task_repositories(self):
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT id,workspace FROM tasks WHERE repository_id IS NULL"
            ).fetchall()]
        migrated = {}
        for row in rows:
            workspace = row["workspace"]
            match = migrated.get(workspace) or self.store.find_repository(workspace)
            if not match:
                try:
                    resolved = self._resolve_workspace(workspace)
                    match = self.store.add_repository(
                        self._available_alias(resolved.name), str(resolved)
                    )
                except (ValueError, RuntimeError):
                    match = None
            if match:
                migrated[workspace] = match
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE tasks SET repository_id=? WHERE id=?", (match["id"], row["id"])
                    )

    def _sync_default_repository(self):
        repositories = self.store.list_repositories()
        default = next((item for item in repositories if item["is_default"]), None)
        if default is None and repositories:
            first = repositories[0]
            default = self.store.update_repository(
                first["id"], first["alias"], first["path"], is_default=True
            )
        self.default_repository_id = default["id"] if default else None
        self.workspace = Path(default["path"]) if default else None

    def _resolve_workspace(self, value):
        source = self.workspace if value is None else value
        if source is None:
            raise ValueError("尚未登记 Git 仓库，请先在设置页添加仓库。")
        workspace = Path(source).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("工作目录不存在：%s" % workspace)
        inside = self._git(
            ["rev-parse", "--is-inside-work-tree"], cwd=workspace, check=False
        )
        if inside.returncode != 0:
            raise ValueError("工作目录不是 Git 仓库：%s" % workspace)
        root = self._git(["rev-parse", "--show-toplevel"], cwd=workspace).stdout.strip()
        if not root:
            raise ValueError("无法确定 Git 仓库根目录：%s" % workspace)
        return Path(root).expanduser().resolve()

    def _git(self, arguments, cwd=None, check=True, timeout=None):
        try:
            result = subprocess.run(
                [self.git_bin] + list(arguments),
                cwd=str(cwd or self.workspace),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Git 命令超时。") from exc
        if check and result.returncode:
            raise RuntimeError(compact_error(result.stdout) or "Git 命令失败")
        return result

    @staticmethod
    def _normalize_global_concurrency(value):
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("全局并发必须是整数。") from exc
        if not 1 <= number <= MAX_GLOBAL_CONCURRENCY:
            raise ValueError("全局并发必须在 1–%s 之间。" % MAX_GLOBAL_CONCURRENCY)
        return number

    def _executor_adapter(self, executor, capability=None):
        name = str(executor or "").strip().lower()
        if name not in EXECUTOR_LABELS:
            raise ValueError("执行器只能选择 Codex 或 Claude。")
        adapter = self.executors.get(name)
        if adapter is None:
            raise ValueError("%s 尚未安装。" % EXECUTOR_LABELS[name])
        if capability and not adapter.supports(capability):
            raise ValueError("%s 不支持当前操作。" % EXECUTOR_LABELS[name])
        health = adapter.health()
        if not health.get("available", True):
            raise ValueError("%s 当前不可用：%s" % (
                EXECUTOR_LABELS[name], health.get("error") or "请检查登录或安装"
            ))
        return name, adapter

    def _executor_snapshot(self, executor, model=None, effort=None):
        name, adapter = self._executor_adapter(executor)
        return {
            "executor": name,
            "name": EXECUTOR_LABELS[name],
            "model": str(model or "").strip(),
            "reasoningEffort": str(effort or "high"),
            "kind": adapter.kind,
        }

    def list_executors(self):
        result = []
        for name in ("codex", "claude"):
            adapter = self.executors.get(name)
            if adapter is None:
                result.append({
                    "id": name, "name": EXECUTOR_LABELS[name], "available": False,
                    "kind": "codex-cli" if name == "codex" else "claude-cli",
                    "models": [], "error": "未安装。",
                })
                continue
            try:
                health = adapter.health()
                result.append({
                    "id": name, "name": EXECUTOR_LABELS[name], "kind": adapter.kind,
                    "available": bool(health.get("available", True)),
                    "models": adapter.models(), "error": health.get("error"),
                })
            except Exception as exc:
                result.append({
                    "id": name, "name": EXECUTOR_LABELS[name], "kind": adapter.kind,
                    "available": False, "models": [], "error": compact_error(exc, 300),
                })
        return result

    def executor_settings(self):
        runs = self.agent_gate.snapshot()
        return {
            "planner": self.role_settings()["planner"],
            "executors": self.list_executors(),
            "globalConcurrency": runs["limit"],
            "maxGlobalConcurrency": MAX_GLOBAL_CONCURRENCY,
            "runs": runs,
            "orchestrationSkill": {
                "name": "orchestration",
                "source": "https://github.com/stablyai/orca/tree/main/skills/orchestration",
                "mode": "FastLab adaptation",
            },
        }

    def update_executor_settings(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("设置格式无效。")
        if "globalConcurrency" in payload:
            limit = self._normalize_global_concurrency(payload["globalConcurrency"])
            self.store.set_setting("agent.globalConcurrency", limit)
            self.agent_gate.set_limit(limit)
        if "planner" in payload:
            raise ValueError("Planner 配置来自环境变量；请修改配置文件后重启 FastLab。")
        return self.executor_settings()

    def role_settings(self):
        planner = dict(self.planner.settings())
        backend = planner.get("backend") or "unknown"
        names = {"codex": "Codex Planner", "claude": "Claude Planner"}
        return {
            "planner": {
                **planner,
                "name": names.get(backend, "Planner"),
                "reasoningEffort": getattr(self.planner, "effort", "") or "high",
                "readOnly": True,
            }
        }

    def list_repositories(self):
        return [self._repository_status(item) for item in self.store.list_repositories()]

    def _repository_status(self, repository):
        result = dict(repository)
        try:
            resolved = self._resolve_workspace(repository["path"])
            if str(resolved) != repository["path"]:
                raise ValueError("路径不再指向已登记的 Git 根目录。")
            head = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=resolved, check=False
            )
            has_commit = head.returncode == 0 and bool(head.stdout.strip())
            result.update({
                "available": True,
                "has_commit": has_commit,
                "initializable": not has_commit,
                "error": None if has_commit else "仓库还没有初始提交。",
            })
        except (OSError, ValueError, RuntimeError) as exc:
            path = Path(repository["path"]).expanduser()
            result.update({
                "available": False,
                "has_commit": False,
                "initializable": path.is_dir(),
                "error": compact_error(exc, 240),
            })
        return result

    def _repository_path_for_add(self, path, initialize=False):
        raw_path = str(path or "").strip()
        if not raw_path:
            raise ValueError("请填写仓库目录。")
        candidate = Path(raw_path).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError("仓库目录不存在或不是文件夹：%s" % candidate)
        inside = self._git(
            ["rev-parse", "--is-inside-work-tree"], cwd=candidate, check=False
        )
        if inside.returncode == 0:
            resolved = self._resolve_workspace(candidate)
            head = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=resolved, check=False
            )
            if head.returncode == 0 and head.stdout.strip():
                return resolved, False
            if not initialize:
                raise RepositoryInitializationRequired(resolved, "no_commits")
            self._create_initial_snapshot(resolved, initialize_git=False)
            return resolved, True
        if not initialize:
            raise RepositoryInitializationRequired(candidate, "not_git")
        self._create_initial_snapshot(candidate, initialize_git=True)
        return self._resolve_workspace(candidate), True

    def _create_initial_snapshot(self, workspace, initialize_git):
        workspace = Path(workspace).resolve()
        try:
            if initialize_git:
                self._git(["init"], cwd=workspace)
            git_dir = self._git(
                ["rev-parse", "--git-dir"], cwd=workspace
            ).stdout.strip()
            git_dir = Path(git_dir)
            if not git_dir.is_absolute():
                git_dir = workspace / git_dir
            exclude = git_dir.resolve() / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if ".fastlab/" not in {line.strip() for line in existing.splitlines()}:
                separator = "" if not existing or existing.endswith("\n") else "\n"
                with exclude.open("a", encoding="utf-8") as stream:
                    stream.write(separator + ".fastlab/\n")
            self._git(["add", "--all", "--", "."], cwd=workspace)
            self._git(
                [
                    "-c", "user.name=FastLab",
                    "-c", "user.email=fastlab@local",
                    "commit", "--allow-empty", "-m", "Initial snapshot by FastLab",
                ],
                cwd=workspace,
            )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                "Git 初始化未完成，请检查目录权限或文件后重试：%s"
                % compact_error(exc, 500)
            ) from exc

    def add_repository(self, alias, path, is_default=False, initialize=False):
        alias = str(alias or "").strip()
        if not re.match(r"^[a-zA-Z0-9._-]{1,40}$", alias):
            raise ValueError("仓库别名只能包含字母、数字、点、短横线和下划线。")
        with self.repository_lock:
            if self.store.find_repository(alias):
                raise ValueError("仓库别名已经存在。")
            raw_path = str(path or "").strip()
            if not raw_path:
                raise ValueError("请填写仓库目录。")
            candidate = Path(raw_path).expanduser().resolve()
            if self.store.find_repository(str(candidate)):
                raise ValueError("该仓库已经登记。")
            resolved, initialized = self._repository_path_for_add(path, bool(initialize))
            if self.store.find_repository(str(resolved)):
                raise ValueError("该仓库已经登记。")
            make_default = bool(is_default) or not self.store.list_repositories()
            result = self.store.add_repository(alias, str(resolved), make_default)
            if result["is_default"]:
                self.default_repository_id = result["id"]
                self.workspace = Path(result["path"])
            return {**self._repository_status(result), "initialized": initialized}

    def initialize_repository(self, repository_id):
        with self.repository_lock:
            repository = self.store.get_repository(repository_id)
            if not repository:
                raise KeyError("找不到仓库。")
            resolved, initialized = self._repository_path_for_add(
                repository["path"], initialize=True
            )
            if str(resolved) != repository["path"]:
                repository = self.store.update_repository(
                    repository_id, repository["alias"], str(resolved),
                    repository["is_default"],
                )
            return {**self._repository_status(repository), "initialized": initialized}

    def update_repository(self, repository_id, alias, path, is_default=False):
        alias = str(alias or "").strip()
        if not re.match(r"^[a-zA-Z0-9._-]{1,40}$", alias):
            raise ValueError("仓库别名格式无效。")
        resolved = self._resolve_workspace(path)
        current = self.store.get_repository(repository_id)
        if not current:
            raise KeyError("找不到仓库。")
        result = self.store.update_repository(
            repository_id, alias, str(resolved), bool(is_default) or current["is_default"]
        )
        if result["is_default"]:
            self.default_repository_id = result["id"]
            self.workspace = Path(result["path"])
        return self._repository_status(result)

    def delete_repository(self, repository_id):
        repository = self.store.get_repository(repository_id)
        if not repository:
            raise KeyError("找不到仓库。")
        self.store.delete_repository(repository_id)
        self._sync_default_repository()
        return {
            "ok": True,
            "repositoryId": self.default_repository_id,
            "workspace": str(self.workspace) if self.workspace else None,
        }

    def rerun_task(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] not in {"completed", "needs_attention", "failed", "cancelled"}:
                raise ValueError("只有已结束的任务可以重新运行。")
            return self.create_task(
                task["title"],
                task["goal"],
                task["constraints_text"],
                task["max_concurrency"],
                repository_id=task.get("repository_id"),
                source_channel=task.get("source_channel") or "web",
                working_subdir=task.get("working_subdir") or "",
                inherit_channels_from=(
                    task_id if task.get("source_channel") == "feishu" else None
                ),
            )

    def continue_task(self, task_id, message, channel_context=None):
        message = str(message or "").strip()
        if not message:
            raise ValueError("继续修改的追加要求不能为空。")
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] != "completed" or not task.get("delivered_commit"):
                raise ValueError(
                    "只有已经成功交付的任务可以继续修改；未交付任务请使用“追加”或“重试”。"
                )
            workspace = self._resolve_workspace(task["workspace"])
            current_branch = self._git(
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=workspace,
                check=False,
            ).stdout.strip()
            if not current_branch or current_branch != task.get("base_branch"):
                raise ValueError(
                    "目标目录必须位于原任务交付分支 `%s`，当前为 `%s`。"
                    % (task.get("base_branch") or "未知", current_branch or "分离 HEAD")
                )
            current_head = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=workspace
            ).stdout.strip()
            contains_delivery = self._git(
                ["merge-base", "--is-ancestor", task["delivered_commit"], current_head],
                cwd=workspace,
                check=False,
            )
            if contains_delivery.returncode:
                raise ValueError("目标分支已不再包含原任务的交付结果，不能创建继续任务。")
            continued_goal = (
                "原任务目标：\n%s\n\n本次继续修改：\n%s"
                % (task["goal"], message)
            )
            return self.create_task(
                "继续：%s" % task["title"],
                continued_goal,
                task["constraints_text"],
                task["max_concurrency"],
                repository_id=task.get("repository_id"),
                source_channel=task.get("source_channel") or "web",
                channel_context=channel_context,
                working_subdir=task.get("working_subdir") or "",
                parent_task_id=task_id,
                base_branch=current_branch,
                base_commit=current_head,
                inherit_channels_from=task_id,
            )

    def clear_task_logs(self, task_id):
        with self._task_lock(task_id):
            self.require_task(task_id)
            self.store.clear_task_events(task_id)
        return {"ok": True, "taskId": task_id}

    def _remove_task_data_tree(self, category, task_id):
        parent = (self.data_dir / category).resolve()
        candidate = self.data_dir / category / task_id
        if candidate.is_symlink():
            candidate.unlink()
            return
        if not candidate.exists():
            return
        resolved = candidate.resolve()
        try:
            resolved.relative_to(parent)
        except ValueError as exc:
            raise RuntimeError("拒绝删除 FastLab 数据目录之外的路径。") from exc
        shutil.rmtree(resolved)

    def _cleanup_task_git(self, task_id):
        task = self.require_task(task_id)
        if task.get("cleaned_at"):
            return []
        root = (self.data_dir / "worktrees" / task_id).resolve()
        paths = [task.get("integration_worktree")]
        paths.extend(item.get("worktree") for item in task["subtasks"])
        branches = [task.get("integration_branch")]
        branches.extend(item.get("branch") for item in task["subtasks"])
        if not any(paths) and not any(branches):
            self.store.update_task(task_id, cleaned_at=utc_now())
            return []

        workspace = self._resolve_workspace(task["workspace"])
        for raw_path in dict.fromkeys(value for value in paths if value):
            path = Path(raw_path)
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise RuntimeError(
                    "拒绝清理任务数据目录之外的 Worktree：%s" % path
                ) from exc
            self._git(
                ["worktree", "remove", "--force", str(path)],
                cwd=workspace,
                check=False,
            )

        self._remove_task_data_tree("worktrees", task_id)
        self._git(
            ["worktree", "prune", "--expire", "now"],
            cwd=workspace,
            check=False,
        )

        prefix = "fastlab/task-%s-" % task_id[:8]
        deleted = []
        for branch in dict.fromkeys(value for value in branches if value):
            if not branch.startswith(prefix):
                raise RuntimeError("拒绝删除不属于当前任务的分支：%s" % branch)
            exists = self._git(
                ["show-ref", "--verify", "--quiet", "refs/heads/%s" % branch],
                cwd=workspace,
                check=False,
            )
            if exists.returncode:
                continue
            result = self._git(
                ["branch", "-D", "--", branch], cwd=workspace, check=False
            )
            if result.returncode:
                raise RuntimeError(
                    "无法删除临时分支 `%s`：%s"
                    % (branch, compact_error(result.stdout))
                )
            deleted.append(branch)

        for item in task["subtasks"]:
            self.store.update_subtask(item["id"], branch=None, worktree=None)
        self.store.update_task(
            task_id,
            integration_worktree=None,
            cleaned_at=utc_now(),
        )
        return deleted

    def delete_task(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] in {"planning", "running", "verifying"}:
                raise ValueError("任务仍在运行，请先停止任务。")
            deleted_branches = self._cleanup_task_git(task_id)
            self.store.delete_task(task_id)
            self._remove_task_data_tree("tasks", task_id)

        with self.task_locks_lock:
            self.task_locks.pop(task_id, None)
        return {
            "ok": True,
            "taskId": task_id,
            "branchesPreserved": False,
            "deletedBranches": deleted_branches,
        }

    def health_payload(self):
        stored_default = (
            self.store.get_repository(self.default_repository_id)
            if self.default_repository_id else None
        )
        default = self._repository_status(stored_default) if stored_default else None
        ready = bool(default and default["available"])
        executors = self.list_executors()
        return {
            "ok": True,
            "workspace": str(self.workspace) if self.workspace else None,
            "repositoryId": self.default_repository_id,
            "setupRequired": not ready,
            "python": sys.version.split()[0],
            "pythonSupported": sys.version_info >= (3, 10),
            "planner": self.planner.health(),
            "agentRuns": {
                **self.agent_gate.snapshot(),
                "timeoutSeconds": self.agent_timeout,
            },
            "executors": executors,
            "orchestration": {
                "ready": PLANNER_SKILL.is_file(),
                "skill": "orchestration",
                "source": "https://github.com/stablyai/orca/tree/main/skills/orchestration",
            },
            "feishu": self.feishu.status() if self.feishu else {
                "configured": False, "connected": False, "error": None
            },
        }

    def create_task(self, title, goal, constraints_text, max_concurrency, workspace=None,
                    repository_id=None, source_channel="web", channel_context=None,
                    working_subdir="", parent_task_id=None, base_branch=None,
                    base_commit=None, inherit_channels_from=None):
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("请填写任务目标。")
        if len(goal) > 20000 or len(str(constraints_text or "")) > 10000:
            raise ValueError("任务内容过长。")
        try:
            concurrency = int(max_concurrency or DEFAULT_GLOBAL_CONCURRENCY)
        except (TypeError, ValueError) as exc:
            raise ValueError("任务并发必须是整数。") from exc
        if not 1 <= concurrency <= MAX_GLOBAL_CONCURRENCY:
            raise ValueError("任务并发必须在 1–%s 之间。" % MAX_GLOBAL_CONCURRENCY)
        display_title = str(title or "").strip() or goal.splitlines()[0][:80]
        repository = self._resolve_repository(repository_id, workspace)
        task_workspace = Path(repository["path"])
        normalized_subdir = self._normalize_working_subdir(task_workspace, working_subdir)
        role_settings = self.role_settings()
        planner_health = self.planner.health()
        if not planner_health.get("available", True):
            raise ValueError("Planner 当前不可用：%s" % (
                planner_health.get("error") or "请检查环境变量和本地 CLI"
            ))
        task_id = self.store.create_task(
            display_title,
            goal,
            str(constraints_text or "").strip(),
            str(task_workspace),
            concurrency,
            repository_id=repository["id"],
            source_channel=source_channel,
            role_settings=role_settings,
            working_subdir=normalized_subdir,
            parent_task_id=parent_task_id,
            base_branch=base_branch,
            base_commit=base_commit,
        )
        if inherit_channels_from:
            self._copy_task_channels(inherit_channels_from, task_id)
        if channel_context:
            self.store.bind_task_channel(
                task_id,
                channel_context.get("channel", source_channel),
                channel_context["conversationId"],
                channel_context.get("operatorId"),
                source_message_id=channel_context.get("messageId"),
            )
        self.store.add_event(task_id, "task.created", "任务已创建，正在生成执行计划。")
        self.write_docs(task_id)
        self._spawn(self._plan_task, task_id, "")
        return self.task_payload(task_id)

    def _copy_task_channels(self, source_task_id, target_task_id):
        for binding in self.store.channel_bindings(source_task_id):
            self.store.bind_task_channel(
                target_task_id,
                binding["channel"],
                binding["conversation_id"],
                binding.get("operator_id"),
                source_message_id=binding.get("source_message_id"),
            )

    def _resolve_repository(self, repository_id=None, workspace=None):
        if repository_id:
            repository = self.store.get_repository(str(repository_id))
        elif workspace:
            resolved = str(self._resolve_workspace(workspace))
            repository = self.store.find_repository(resolved)
        else:
            repository = self.store.get_repository(self.default_repository_id)
        if not repository:
            raise ValueError("只能选择设置页中已登记的仓库。")
        status = self._repository_status(repository)
        if not status["available"]:
            raise ValueError("仓库当前不可用：%s" % status["error"])
        current = str(self._resolve_workspace(repository["path"]))
        if current != repository["path"]:
            repository = self.store.update_repository(
                repository["id"], repository["alias"], current, repository["is_default"]
            )
        return repository

    @staticmethod
    def _normalize_working_subdir(repository_root, value):
        raw = str(value or "").strip().replace("\\", "/")
        if raw in {"", "."}:
            return ""
        windows_path = PureWindowsPath(raw)
        posix_path = PurePosixPath(raw)
        if windows_path.drive or windows_path.is_absolute() or posix_path.is_absolute():
            raise ValueError("工作目录必须是仓库内的相对目录。")
        parts = [part for part in posix_path.parts if part not in {"", "."}]
        if not parts or ".." in parts:
            raise ValueError("工作目录不能包含 ..。")
        root = Path(repository_root).resolve()
        candidate = root.joinpath(*parts).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("工作目录不能通过符号链接离开仓库。") from exc
        if not candidate.is_dir():
            raise ValueError("工作目录不是文件夹：%s" % "/".join(parts))
        return "/".join(parts)

    def replan_task(self, task_id, feedback):
        with self._task_lock(task_id):
            return self._replan_task_locked(task_id, feedback)

    def _replan_task_locked(self, task_id, feedback):
        task = self.require_task(task_id)
        if task["status"] not in {"awaiting_approval", "failed", "needs_attention"}:
            raise ValueError("当前状态不能重新规划。")
        if any(item["attempt"] for item in task["subtasks"]):
            raise ValueError("已经执行过的任务不能重新规划，请新建任务。")
        planner_health = self.planner.health()
        if not planner_health.get("available", True):
            raise ValueError("Planner 当前不可用：%s" % (
                planner_health.get("error") or "请检查环境变量和本地 CLI"
            ))
        planner_only = self.role_settings()
        self.store.update_task(
            task_id, cancel_requested=0,
            role_settings_json=json.dumps(planner_only, ensure_ascii=False),
        )
        self._transition_task(
            task_id, "planning", "task.replan", "正在根据反馈重新规划。"
        )
        self._spawn(self._plan_task, task_id, str(feedback or "").strip())
        return self.task_payload(task_id)

    def _task_lock(self, task_id):
        with self.task_locks_lock:
            return self.task_locks.setdefault(task_id, threading.RLock())

    def _transition_task(self, task_id, status, kind, message, error=None, payload=None,
                         subtask_id=None):
        with self._task_lock(task_id):
            self.store.transition_task(
                task_id, status, kind, message, error=error, payload=payload,
                subtask_id=subtask_id,
            )
            self._write_docs_unlocked(task_id)

    def _update_subtask_state(self, task_id, subtask_id, *, kind=None, message=None,
                              notify=False, expected_dispatch_id=None, payload=None,
                              **values):
        """Publish a subtask state change as one task-visible operation."""
        with self._task_lock(task_id):
            if expected_dispatch_id:
                updated = self.store.update_subtask_for_dispatch(
                    subtask_id, expected_dispatch_id, **values
                )
                if not updated:
                    return False
            else:
                self.store.update_subtask(subtask_id, **values)
            event_payload = dict(payload or {})
            dispatch_id = expected_dispatch_id or values.get("dispatch_id")
            if dispatch_id:
                event_payload.update({
                    "task_id": task_id,
                    "subtask_id": subtask_id,
                    "dispatch_id": dispatch_id,
                })
            if kind and message:
                self.store.add_event(
                    task_id, kind, message, subtask_id=subtask_id,
                    payload=event_payload or None,
                )
                if notify:
                    self.store.enqueue_notification(task_id, kind, message)
            self._write_docs_unlocked(task_id)
            return True

    @staticmethod
    def _subtask_run_key(subtask_id, dispatch_id):
        task_id, separator, plan_key = str(subtask_id).partition(":")
        if separator:
            return "subtask-%s-dispatch-%s:%s" % (task_id, dispatch_id, plan_key)
        return "subtask-%s-dispatch-%s" % (subtask_id, dispatch_id)

    def _dispatch_is_current(self, task_id, subtask_id, dispatch_id):
        task = self.require_task(task_id)
        return any(
            item["id"] == subtask_id
            and item.get("dispatch_id") == dispatch_id
            and item["status"] == "running"
            for item in task["subtasks"]
        )

    def _rebuild_documents(self):
        for task in self.store.list_tasks():
            self.write_docs(task["id"])

    def _timeout_guard(self, key, runner):
        timed_out = threading.Event()
        if not self.agent_timeout:
            return None, timed_out

        def expire():
            timed_out.set()
            try:
                runner.interrupt(key)
            except Exception:
                pass

        timer = threading.Timer(self.agent_timeout, expire)
        timer.daemon = True
        timer.start()
        return timer, timed_out

    def _timeout_error(self, role):
        seconds = (
            int(self.agent_timeout)
            if float(self.agent_timeout).is_integer() else self.agent_timeout
        )
        return AgentRunTimeout("%s运行超过 %s 秒，已终止整个进程组。" % (role, seconds))

    @staticmethod
    def _finish_timeout_guard(timer):
        if timer is None:
            return
        timer.cancel()
        if timer is not threading.current_thread():
            timer.join(timeout=1)

    def _run_agent(self, task_id, key, cwd, prompt, sandbox, executor,
                   capability, **options):
        def cancelled():
            task = self.store.get_task(task_id, include_events=False)
            return self.shutting_down or task is None or task["cancel_requested"]

        _, adapter = self._executor_adapter(executor, capability)
        self.agent_gate.acquire(cancelled)
        try:
            if cancelled():
                raise AgentRunCancelled("Agent 运行在启动前被取消。")
            with self.active_run_lock:
                self.active_run_adapters[key] = adapter
            timer, timed_out = self._timeout_guard(key, adapter)
            try:
                try:
                    result = adapter.run(key, cwd, prompt, sandbox, **options)
                except Exception as exc:
                    if timed_out.is_set():
                        raise self._timeout_error("Agent ") from exc
                    raise
                if timed_out.is_set():
                    raise self._timeout_error("Agent ")
                return result
            finally:
                self._finish_timeout_guard(timer)
        finally:
            with self.active_run_lock:
                self.active_run_adapters.pop(key, None)
            self.agent_gate.release()

    def _run_planner(self, task_id, key, prompt, context):
        def cancelled():
            task = self.store.get_task(task_id, include_events=False)
            return self.shutting_down or task is None or task["cancel_requested"]

        self.agent_gate.acquire(cancelled)
        try:
            if cancelled():
                raise AgentRunCancelled("Planner 在启动前被取消。")
            with self.active_run_lock:
                self.active_run_adapters[key] = self.planner
            timer, timed_out = self._timeout_guard(key, self.planner)
            try:
                try:
                    result = self.planner.plan(prompt, context)
                except Exception as exc:
                    if timed_out.is_set():
                        raise self._timeout_error("Planner ") from exc
                    raise
                if timed_out.is_set():
                    raise self._timeout_error("Planner ")
                return result
            finally:
                self._finish_timeout_guard(timer)
        finally:
            with self.active_run_lock:
                self.active_run_adapters.pop(key, None)
            self.agent_gate.release()

    def _spawn(self, target, *arguments):
        def wrapped():
            try:
                target(*arguments)
            finally:
                with self.thread_lock:
                    self.background_threads.discard(threading.current_thread())

        with self.thread_lock:
            if self.shutting_down:
                return None
            thread = threading.Thread(target=wrapped, daemon=True)
            self.background_threads.add(thread)
        thread.start()
        return thread

    def _plan_task(self, task_id, feedback):
        task = self.require_task(task_id)
        available_executors = {
            item["id"] for item in self.list_executors() if item["available"]
        }
        prompt = self.planner_prompt(task, feedback, available_executors)
        session = {"id": None}

        def event(kind, message, payload):
            self.store.add_event(task_id, kind, message, payload=payload)
            if isinstance(payload, dict):
                session["id"] = (
                    payload.get("sessionId") or payload.get("thread_id") or session["id"]
                )

        try:
            workspace = self._resolve_workspace(task["workspace"])
            plan = self._run_planner(
                task_id,
                "task-%s-planner" % task_id,
                prompt,
                {
                    "key": "task-%s-planner" % task_id,
                    "cwd": workspace,
                    "repositoryPath": str(workspace),
                    "workingSubdir": task.get("working_subdir") or "",
                    "availableExecutors": sorted(available_executors),
                    "feedback": feedback or "",
                    "on_event": event,
                },
            )
            self.store.update_task(task_id, planner_thread_id=session["id"])
            if self.require_task(task_id)["cancel_requested"]:
                self._transition_task(
                    task_id, "cancelled", "task.cancelled", "任务已取消。"
                )
                return
            plan = self.validate_plan(plan, available_executors=available_executors)
            with self._task_lock(task_id):
                self.store.replace_subtasks(task_id, plan["subtasks"])
                for item in self.store.get_task(task_id, include_events=False)["subtasks"]:
                    self._assign_subtask_executor(
                        item,
                        next(value["executor"] for value in plan["subtasks"]
                             if value["key"] == item["plan_key"]),
                    )
                values = {
                    "plan_json": json.dumps(plan, ensure_ascii=False),
                    "error": None,
                }
                if not task["title"].strip():
                    values["title"] = plan["title"]
                self.store.update_task(task_id, **values)
                self._transition_task(
                    task_id, "awaiting_approval", "task.planned",
                    "执行计划和执行器分配已生成，等待用户确认并选择验收执行器。"
                )
        except Exception as exc:
            current = self.store.get_task(task_id)
            if current and current["cancel_requested"]:
                self._transition_task(
                    task_id, "cancelled", "task.cancelled", "任务已取消。"
                )
            else:
                self._transition_task(
                    task_id, "failed", "task.error", "规划失败：%s" % compact_error(exc),
                    error=compact_error(exc),
                )

    @staticmethod
    def _schema_for_adapter(adapter, path):
        if getattr(adapter, "kind", "") == "codex-cli":
            return path
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def planner_prompt(task, feedback, available_executors=None):
        extra = "\n用户对上一版计划的反馈：\n%s\n" % feedback if feedback else ""
        selected_executors = (
            set(available_executors) if available_executors is not None else {"codex"}
        )
        executor_lines = "\n".join(
            "- %s" % EXECUTOR_LABELS[item]
            for item in sorted(selected_executors)
        ) or "- 无可用 Worker；请明确报告无法生成可执行计划。"
        return """你是 FastLab 的只读任务规划器。必须遵守已注入的 orchestration Skill。

把目标拆成可以在独立 Git worktree 中完成的任务 DAG。使用 S1、S2… 作为 key，dependencies 只表达真实依赖，weight 取 1 到 5。每个子任务必须从可用执行器中选择 executor；不要创建或命名任何其他 Agent。title 是折叠时显示的简洁摘要（最多 80 个字符）；instructions 写清具体产出、范围、限制和检查方法，但不要重复整段顶层目标。验收标准必须可验证、可给出证据，优先设计无需 GUI 或公网的自动检查。严格输出符合给定 JSON Schema 的单个 JSON 对象。

目标仓库：{workspace}
主要工作目录：{working_subdir}
任务标题：{title}
任务目标：
{goal}

限制条件：
{constraints}

可用执行器：
{executors}
{extra}""".format(
            workspace=task["workspace"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            title=task["title"],
            goal=task["goal"],
            constraints=task["constraints_text"] or "无",
            executors=executor_lines,
            extra=extra,
        )

    @staticmethod
    def parse_json_result(value):
        text = (value or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text:
            raise ValueError("Agent 没有返回结构化结果。")
        return json.loads(text)

    @staticmethod
    def validate_plan(plan, available_executors=None):
        if not isinstance(plan, dict):
            raise ValueError("任务计划不是 JSON 对象。")
        required = ("title", "summary", "subtasks", "acceptance")
        if any(key not in plan for key in required):
            raise ValueError("任务计划缺少必要字段。")
        subtasks = plan.get("subtasks")
        if not isinstance(subtasks, list) or not 1 <= len(subtasks) <= 20:
            raise ValueError("子任务数量必须在 1 到 20 之间。")
        keys = [str(item.get("key", "")) for item in subtasks]
        if len(set(keys)) != len(keys) or any(not re.match(r"^S[1-9][0-9]*$", key) for key in keys):
            raise ValueError("子任务 key 必须是唯一的 S1、S2… 格式。")
        known = set(keys)
        graph = {}
        allowed_executors = set(available_executors or {"codex"})
        for item in subtasks:
            item["title"] = str(item.get("title", "")).strip()
            item["instructions"] = str(item.get("instructions", "")).strip()
            item["weight"] = max(1, min(5, int(item.get("weight", 1))))
            dependencies = [str(value) for value in item.get("dependencies", [])]
            if not item["title"] or not item["instructions"]:
                raise ValueError("子任务标题和说明不能为空。")
            if len(item["title"]) > 80:
                raise ValueError("子任务标题不能超过 80 个字符。")
            if item["key"] in dependencies or any(value not in known for value in dependencies):
                raise ValueError("子任务依赖引用无效。")
            item["dependencies"] = list(dict.fromkeys(dependencies))
            executor = str(item.get("executor") or "").strip().lower()
            if executor not in allowed_executors:
                raise ValueError("计划选择了不可用的执行器：%s" % (executor or "空"))
            item["executor"] = executor
            item.pop("skills", None)
            item.pop("agentProfile", None)
            graph[item["key"]] = item["dependencies"]
        visiting = set()
        visited = set()

        def visit(node):
            if node in visiting:
                raise ValueError("子任务依赖不能形成循环。")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for key in keys:
            visit(key)
        acceptance = plan.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            raise ValueError("至少需要一条验收标准。")
        ids = set()
        for index, item in enumerate(acceptance, 1):
            item["id"] = str(item.get("id") or "A%s" % index)
            item["criterion"] = str(item.get("criterion", "")).strip()
            if not item["criterion"] or item["id"] in ids:
                raise ValueError("验收标准无效或 ID 重复。")
            ids.add(item["id"])
        plan["title"] = str(plan.get("title", "")).strip()
        plan["summary"] = str(plan.get("summary", "")).strip()
        plan.pop("skills", None)
        plan.pop("verification", None)
        return plan

    def start_task(self, task_id):
        with self._task_lock(task_id):
            return self._start_task_locked(task_id)

    def _start_task_locked(self, task_id):
        task = self.require_task(task_id)
        if task["status"] != "awaiting_approval":
            raise ValueError("任务尚未准备好或已经开始。")
        for subtask in task["subtasks"]:
            executor = subtask.get("executor")
            if not executor:
                raise ValueError("子任务 %s 尚未分配执行器。" % subtask["plan_key"])
            self._executor_adapter(executor, WRITE_CAPABILITY)
        verifier = task["role_settings"].get("verifier") or {}
        verifier_executor = verifier.get("executor")
        if not verifier_executor:
            raise ValueError("请先指定最终验收执行器。")
        self._executor_adapter(verifier_executor, VERIFY_CAPABILITY)
        workspace = self._resolve_workspace(task["workspace"])
        self._normalize_working_subdir(workspace, task.get("working_subdir"))
        head = self._git(
            ["rev-parse", "--verify", "HEAD"], cwd=workspace, check=False
        )
        current_commit = head.stdout.strip()
        if head.returncode or not current_commit:
            raise ValueError(
                "目标仓库还没有任何提交。请先创建初始提交，再开始执行。"
            )
        dirty = self._git(
            ["status", "--porcelain", "--untracked-files=all"], cwd=workspace
        ).stdout.strip()
        if dirty:
            raise ValueError("目标仓库存在未提交改动。请先提交或暂存，再开始执行。")
        current_branch = self._git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workspace, check=False
        ).stdout.strip()
        if task.get("parent_task_id") and task.get("base_commit"):
            if current_branch != task.get("base_branch"):
                raise ValueError(
                    "继续任务创建后目标分支发生变化；请切回 `%s` 或重新创建继续任务。"
                    % task.get("base_branch")
                )
            if current_commit != task.get("base_commit"):
                raise ValueError(
                    "继续任务创建后目标分支 HEAD 已变化；请重新创建继续任务。"
                )
            base_branch = task["base_branch"]
            base_commit = task["base_commit"]
        else:
            base_branch = current_branch or "(detached HEAD)"
            base_commit = current_commit
        short_id = task_id[:8]
        integration_branch = "fastlab/task-%s-integration" % short_id
        integration_path = self.data_dir / "worktrees" / task_id / "integration"
        integration_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            ["worktree", "add", "-b", integration_branch, str(integration_path), base_commit],
            cwd=workspace,
        )
        self.store.update_task(
            task_id,
            base_branch=base_branch,
            base_commit=base_commit,
            integration_branch=integration_branch,
            integration_worktree=str(integration_path),
            cancel_requested=0,
            error=None,
        )
        self._transition_task(
            task_id, "running", "task.started", "任务已确认，开始调度子任务。"
        )
        self._ensure_scheduler(task_id)
        return self.task_payload(task_id)

    def _ensure_scheduler(self, task_id):
        with self.scheduler_lock:
            current = self.schedulers.get(task_id)
            if current and current.is_alive():
                return
            thread = self._spawn(self._scheduler, task_id)
            if thread:
                self.schedulers[task_id] = thread

    def _scheduler(self, task_id):
        try:
            while True:
                task = self.require_task(task_id)
                if task["cancel_requested"]:
                    self._finish_cancel(task)
                    return
                subtasks = task["subtasks"]
                running = [item for item in subtasks if item["status"] == "running"]
                if subtasks and all(item["status"] == "succeeded" for item in subtasks):
                    self._verify_task(task_id)
                    return
                status_by_key = {item["plan_key"]: item["status"] for item in subtasks}
                ready = [
                    item
                    for item in subtasks
                    if item["status"] == "pending"
                    and all(status_by_key.get(dep) == "succeeded" for dep in item["dependencies"])
                ]
                available = max(0, int(task["max_concurrency"]) - len(running))
                launched = 0
                for item in ready[:available]:
                    try:
                        prepared = self._prepare_subtask(task, item)
                        dispatch_id = uuid.uuid4().hex
                        self._update_subtask_state(
                            task_id, item["id"],
                            status="running",
                            attempt=int(item["attempt"]) + 1,
                            dispatch_id=dispatch_id,
                            error=None,
                            kind="subtask.started",
                            message="开始执行 %s（Dispatch %s）。"
                            % (item["title"], dispatch_id[:8]),
                        )
                        prepared = next(
                            current for current in self.require_task(task_id)["subtasks"]
                            if current["id"] == item["id"]
                        )
                        self._spawn(
                            self._run_subtask,
                            task_id,
                            prepared,
                            item.get("pending_message"),
                            dispatch_id,
                            bool(item.get("pending_message") and item.get("session_id")),
                        )
                        launched += 1
                    except Exception as exc:
                        self._update_subtask_state(
                            task_id, item["id"], status="failed", error=compact_error(exc),
                            kind="subtask.error",
                            message="准备子任务失败：%s" % compact_error(exc),
                        )
                if launched:
                    time.sleep(0.25)
                    continue
                if not running and not ready:
                    failed_keys = {
                        item["plan_key"]
                        for item in subtasks
                        if item["status"] in {"failed", "blocked", "cancelled"}
                    }
                    for item in subtasks:
                        if item["status"] == "pending" and any(
                            dep in failed_keys for dep in item["dependencies"]
                        ):
                            self._update_subtask_state(
                                task_id, item["id"], status="blocked",
                                error="依赖任务尚未成功。"
                            )
                    self._transition_task(
                        task_id,
                        "needs_attention",
                        "task.attention",
                        "执行已暂停，需要处理失败或阻塞的子任务。",
                        error="有子任务失败或被依赖阻塞，请检查后重试。",
                    )
                    return
                time.sleep(0.4)
        except Exception as exc:
            self._transition_task(
                task_id, "failed", "task.error", "调度失败：%s" % compact_error(exc),
                error=compact_error(exc),
            )

    def _prepare_subtask(self, task, subtask):
        if subtask.get("worktree") and Path(subtask["worktree"]).exists():
            return self.store.get_task(task["id"])["subtasks"][subtask["ordinal"] - 1]
        branch = "fastlab/task-%s-sub-%02d" % (task["id"][:8], subtask["ordinal"])
        path = self.data_dir / "worktrees" / task["id"] / ("sub-%02d" % subtask["ordinal"])
        integration_path = Path(task["integration_worktree"])
        base = self._git(["rev-parse", "HEAD"], cwd=integration_path).stdout.strip()
        self._git(
            ["worktree", "add", "-b", branch, str(path), base], cwd=integration_path
        )
        self.store.update_subtask(subtask["id"], branch=branch, worktree=str(path))
        refreshed = self.store.get_task(task["id"])
        return next(item for item in refreshed["subtasks"] if item["id"] == subtask["id"])

    def _run_subtask(self, task_id, subtask, instruction_message, dispatch_id,
                     resume_existing=False):
        task = self.require_task(task_id)
        key = self._subtask_run_key(subtask["id"], dispatch_id)
        if instruction_message and resume_existing:
            prompt = str(instruction_message)
        elif instruction_message:
            prompt = self.worker_revision_prompt(task, subtask, instruction_message)
        else:
            prompt = self.worker_prompt(task, subtask)

        def event(kind, message, payload):
            self.store.add_event(
                task_id,
                kind,
                message,
                subtask["id"],
                {
                    "task_id": task_id,
                    "subtask_id": subtask["id"],
                    "dispatch_id": dispatch_id,
                    "agent_event": payload,
                },
            )

        try:
            snapshot = subtask.get("executor_snapshot") or {}
            executor = subtask.get("executor") or snapshot.get("executor")
            if not executor:
                raise RuntimeError("子任务尚未分配执行器。")
            executor, _ = self._executor_adapter(
                executor, RESUME_CAPABILITY if resume_existing else WRITE_CAPABILITY
            )
            model = subtask.get("model") or snapshot.get("model") or None
            effort = subtask.get("reasoning_effort") or snapshot.get("reasoningEffort") \
                or "high"
            self.store.update_subtask(
                subtask["id"],
                model=model,
                reasoning_effort=effort,
                pending_message=None,
            )
            code, session_id, final = self._run_agent(
                task_id,
                key,
                Path(subtask["worktree"]),
                prompt,
                "workspace_write",
                executor=executor,
                capability=RESUME_CAPABILITY if resume_existing else WRITE_CAPABILITY,
                resume_session=subtask.get("session_id") if resume_existing else None,
                on_event=event,
                model=model,
                effort=effort,
                skills=[],
            )
            if not self._update_subtask_state(
                task_id,
                subtask["id"],
                expected_dispatch_id=dispatch_id,
                session_id=session_id,
                exit_code=code,
            ):
                self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
                return
            task = self.require_task(task_id)
            if task["cancel_requested"]:
                updated = self._update_subtask_state(
                    task_id, subtask["id"], status="cancelled", error="用户取消任务。",
                    expected_dispatch_id=dispatch_id,
                )
                if not updated:
                    self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
                return
            if code:
                detail = compact_error(final, 800)
                raise RuntimeError(
                    "实现 Agent 退出，代码 %s。%s"
                    % (code, (" 原因：%s" % detail) if detail else "")
                )
            self._enforce_working_subdir(task, Path(subtask["worktree"]))
            self._commit_and_merge(task_id, subtask["id"], dispatch_id)
            updated = self._update_subtask_state(
                task_id, subtask["id"], status="succeeded", error=None,
                kind="subtask.completed",
                message="%s 已完成并合入任务分支（Dispatch %s）。"
                % (subtask["title"], dispatch_id[:8]),
                notify=True,
                expected_dispatch_id=dispatch_id,
            )
            if not updated:
                self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
        except Exception as exc:
            current = self.require_task(task_id)
            if current["cancel_requested"]:
                updated = self._update_subtask_state(
                    task_id, subtask["id"], status="cancelled", error="用户取消任务。"
                    , expected_dispatch_id=dispatch_id
                )
            else:
                updated = self._update_subtask_state(
                    task_id, subtask["id"], status="failed", error=compact_error(exc),
                    kind="subtask.error",
                    message="%s 执行失败：%s" % (subtask["title"], compact_error(exc)),
                    notify=True,
                    expected_dispatch_id=dispatch_id,
                )
            if not updated:
                self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
        finally:
            self._ensure_scheduler(task_id)

    def _record_stale_dispatch(self, task_id, subtask_id, dispatch_id):
        self.store.add_event(
            task_id,
            "dispatch.stale",
            "忽略已经失效的 Dispatch %s 返回结果。" % dispatch_id[:8],
            subtask_id=subtask_id,
            payload={
                "task_id": task_id,
                "subtask_id": subtask_id,
                "dispatch_id": dispatch_id,
            },
        )

    @staticmethod
    def worker_prompt(task, subtask):
        acceptance = "\n".join(
            "- [%s] %s" % (item["id"], item["criterion"])
            for item in (task.get("plan") or {}).get("acceptance", [])
        )
        return """你是 FastLab 启动的实现 Agent。只完成分配给你的子任务，不要管理 Git 分支或 worktree，不要修改任务调度数据。你可以读取完整仓库，但只能修改“允许写入范围”内的文件；FastLab 会在提交前检查越界改动。检查现有代码，实施改动并运行与改动相关的测试。结束前确保工作区只包含本子任务需要的改动。

顶层目标：{goal}
允许写入范围：{working_subdir}
子任务 ID：{key}
子任务：{title}
具体要求：
{instructions}

整体验收标准：
{acceptance}
""".format(
            goal=task["goal"],
            working_subdir=(task.get("working_subdir") + "/**") if task.get("working_subdir") else "整个仓库",
            key=subtask["plan_key"],
            title=subtask["title"],
            instructions=subtask["instructions"],
            acceptance=acceptance,
        )

    @classmethod
    def worker_revision_prompt(cls, task, subtask, message):
        return "%s\n\n本轮是对已完成子任务的再次修改。已有实现保留在当前 Worktree 中；请先检查现状，再完成下面的新增要求。不要撤销原有正确成果。\n\n本轮新增要求：\n%s" % (
            cls.worker_prompt(task, subtask), str(message).strip()
        )

    def _enforce_working_subdir(self, task, worktree):
        scope = str(task.get("working_subdir") or "").strip("/")
        if not scope:
            return
        tracked = self._git(
            [
                "diff", "--name-only", "--no-renames", "-z",
                task.get("base_commit") or "HEAD",
            ],
            cwd=worktree,
        ).stdout
        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree
        ).stdout
        changed = sorted({item for item in (tracked + untracked).split("\0") if item})
        prefix = scope + "/"
        outside = [path for path in changed if path != scope and not path.startswith(prefix)]
        if outside:
            shown = ", ".join(outside[:12])
            if len(outside) > 12:
                shown += " 等 %s 个文件" % len(outside)
            raise RuntimeError(
                "检测到工作目录 `%s` 之外的改动，已停止提交与合并：%s" % (scope, shown)
            )

    def _commit_and_merge(self, task_id, subtask_id, dispatch_id):
        # Keep the task lock through commit and merge. A cancelled/retried Dispatch
        # must never commit after a newer Dispatch has taken ownership.
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            subtask = next(
                item for item in task["subtasks"] if item["id"] == subtask_id
            )
            if (
                task["cancel_requested"]
                or subtask.get("dispatch_id") != dispatch_id
                or subtask["status"] != "running"
            ):
                raise AgentRunCancelled("该 Dispatch 已失效，停止提交与合并。")
            worktree = Path(subtask["worktree"])
            changes = self._git(["status", "--porcelain"], cwd=worktree).stdout.strip()
            if changes:
                self._git(["add", "-A"], cwd=worktree)
                self._git(
                    [
                        "-c",
                        "user.name=FastLab",
                        "-c",
                        "user.email=fastlab@local",
                        "commit",
                        "-m",
                        "fastlab: %s" % subtask["title"][:120],
                    ],
                    cwd=worktree,
                )
            with self.merge_lock:
                integration = Path(task["integration_worktree"])
                result = self._git(
                    [
                        "-c",
                        "user.name=FastLab",
                        "-c",
                        "user.email=fastlab@local",
                        "merge",
                        "--no-ff",
                        subtask["branch"],
                        "-m",
                        "fastlab: merge %s" % subtask["plan_key"],
                    ],
                    cwd=integration,
                    check=False,
                )
                if result.returncode:
                    self._git(["merge", "--abort"], cwd=integration, check=False)
                    raise RuntimeError(
                        "合并冲突，子任务分支已保留。\n%s"
                        % compact_error(result.stdout)
                    )

    def _deliver_integration(self, task_id):
        """Safely deliver a verified integration branch to the original worktree.

        The user's worktree is never stashed or overwritten. If its branch advanced
        after the task started, reconcile those commits in the isolated integration
        worktree first and require a new verification pass before delivery.
        """
        task = self.require_task(task_id)
        workspace = self._resolve_workspace(task["workspace"])
        base_branch = str(task.get("base_branch") or "")
        if not base_branch or base_branch == "(detached HEAD)":
            raise RuntimeError("目标仓库没有可交付的当前分支，请先切换到普通分支。")
        integration_branch = str(task.get("integration_branch") or "")
        if not integration_branch:
            raise RuntimeError("任务集成分支不存在。")

        with self.merge_lock:
            dirty = self._git(
                ["status", "--porcelain", "--untracked-files=all"], cwd=workspace
            ).stdout.strip()
            if dirty:
                raise RuntimeError("目标目录存在未提交改动，已停止自动交付。")
            current_branch = self._git(
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=workspace,
                check=False,
            ).stdout.strip()
            if current_branch != base_branch:
                raise RuntimeError(
                    "目标目录当前分支是 `%s`，任务开始时是 `%s`，已停止自动交付。"
                    % (current_branch or "分离 HEAD", base_branch)
                )
            current_commit = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=workspace
            ).stdout.strip()
            integration_commit = self._git(
                ["rev-parse", "--verify", integration_branch], cwd=workspace
            ).stdout.strip()

            def is_ancestor(ancestor, descendant):
                return self._git(
                    ["merge-base", "--is-ancestor", ancestor, descendant],
                    cwd=workspace,
                    check=False,
                ).returncode == 0

            if current_commit == integration_commit:
                delivered_commit = current_commit
            elif is_ancestor(current_commit, integration_commit):
                # The target has not diverged from the integration result. A
                # non-base target may be left by an earlier reconciliation; do
                # not deliver it until the current integration HEAD was verified.
                verified_commit = str(
                    (task.get("plan") or {}).get("verificationCommit") or ""
                )
                if (
                    current_commit != task.get("base_commit")
                    and verified_commit != integration_commit
                ):
                    raise DeliveryReverificationRequired(
                        "目标分支的新提交已合入任务结果，正在重新验收。"
                    )
                result = self._git(
                    ["merge", "--ff-only", integration_branch],
                    cwd=workspace,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(
                        "无法安全快进到任务集成分支：%s" % compact_error(result.stdout)
                    )
                delivered_commit = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=workspace
                ).stdout.strip()
            else:
                base_commit = str(task.get("base_commit") or "")
                if not base_commit or not is_ancestor(base_commit, current_commit):
                    raise RuntimeError(
                        "目标分支历史已被改写，无法确认安全合并。目标目录未修改；"
                        "请切回任务开始时的分支历史，或使用“继续修改”创建新任务。"
                    )

                integration = Path(str(task.get("integration_worktree") or ""))
                if not integration.is_dir():
                    raise RuntimeError("任务集成 Worktree 不存在，无法同步目标分支。")
                integration_dirty = self._git(
                    ["status", "--porcelain", "--untracked-files=all"], cwd=integration
                ).stdout.strip()
                if integration_dirty:
                    raise RuntimeError("任务集成 Worktree 存在未提交改动，无法安全同步。")
                checked_out_branch = self._git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=integration,
                    check=False,
                ).stdout.strip()
                if checked_out_branch != integration_branch:
                    raise RuntimeError("任务集成 Worktree 未位于预期分支，无法安全同步。")
                integration_head = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=integration
                ).stdout.strip()
                if integration_head != integration_commit:
                    raise RuntimeError("任务集成 Worktree 与集成分支不一致，无法安全同步。")

                if is_ancestor(integration_commit, current_commit):
                    result = self._git(
                        ["merge", "--ff-only", current_commit],
                        cwd=integration,
                        check=False,
                    )
                else:
                    result = self._git(
                        [
                            "-c", "user.name=FastLab",
                            "-c", "user.email=fastlab@local",
                            "merge", "--no-ff", current_commit,
                            "-m", "fastlab: reconcile target before delivery",
                        ],
                        cwd=integration,
                        check=False,
                    )
                if result.returncode:
                    self._git(["merge", "--abort"], cwd=integration, check=False)
                    raise RuntimeError(
                        "目标分支的新提交与任务结果发生冲突，目标目录未修改。"
                        "请处理冲突或使用“继续修改”创建新任务。\n%s"
                        % compact_error(result.stdout)
                    )
                reconciled_commit = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=integration
                ).stdout.strip()
                self.store.add_event(
                    task_id,
                    "task.delivery.reconciled",
                    "目标分支的新提交已安全合入任务集成分支；交付前将重新验收。",
                    payload={
                        "targetCommit": current_commit,
                        "integrationCommit": reconciled_commit,
                    },
                )
                raise DeliveryReverificationRequired(
                    "目标分支的新提交已安全合入任务结果，正在重新验收。"
                )

            if delivered_commit != integration_commit:
                raise RuntimeError("交付后的提交与任务集成分支不一致。")
            self.store.update_task(
                task_id,
                delivered_commit=delivered_commit,
                delivered_at=utc_now(),
                error=None,
            )
            return delivered_commit

    def _queue_delivery_reverification(self, task_id, message):
        """Discard stale evidence and verify the reconciled integration HEAD."""
        task = self.require_task(task_id)
        plan = dict(task.get("plan") or {})
        plan.pop("verification", None)
        plan.pop("verificationCommit", None)
        self.store.update_task(
            task_id,
            plan_json=json.dumps(plan, ensure_ascii=False),
            error=None,
            cancel_requested=0,
        )
        self._transition_task(
            task_id,
            "verifying",
            "task.delivery.reverify",
            str(message),
            error=None,
        )
        self._spawn(self._verify_task, task_id)

    def _verification_workspace(self, task):
        integration = task.get("integration_worktree")
        if integration and Path(integration).is_dir():
            return Path(integration)
        if task.get("delivered_commit"):
            return self._resolve_workspace(task["workspace"])
        raise ValueError("任务集成工作区不存在，无法重新验收。")

    def _create_verification_worktree(self, task):
        """Create a disposable detached copy for runtime verification.

        A verifier may need to create caches or start a localhost server. It
        receives a writable disposable worktree, never the integration branch
        that will be delivered.
        """
        workspace = self._resolve_workspace(task["workspace"])
        commit = task.get("delivered_commit")
        if not commit:
            branch = task.get("integration_branch")
            if not branch:
                raise ValueError("任务集成分支不存在，无法验收。")
            commit = self._git(
                ["rev-parse", "--verify", branch], cwd=workspace
            ).stdout.strip()
        root = self.data_dir / "worktrees" / task["id"]
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("verification-%s" % uuid.uuid4().hex[:10])
        self._git(
            ["worktree", "add", "--detach", str(path), commit], cwd=workspace
        )
        return path

    def _remove_verification_worktree(self, task, path):
        workspace = self._resolve_workspace(task["workspace"])
        result = self._git(
            ["worktree", "remove", "--force", str(path)],
            cwd=workspace,
            check=False,
        )
        if result.returncode:
            self.store.add_event(
                task["id"], "verification.cleanup.warning",
                "临时验收 Worktree 清理失败：%s" % compact_error(result.stdout),
            )

    def _verify_task(self, task_id):
        task = self.require_task(task_id)
        if task["status"] != "verifying":
            self._transition_task(
                task_id, "verifying", "task.verifying", "所有子任务已合并，开始最终验收。"
            )
        criteria = "\n".join(
            "- %s: %s" % (item["id"], item["criterion"])
            for item in task["plan"]["acceptance"]
        )
        prompt = """你是 FastLab 的最终验收 Agent。你在可丢弃的临时验证副本中工作，不得修改实际交付分支，也不得把临时改动当作验收成果。可以运行现有测试；如需验证 Web 运行时，可以只在 localhost 启动临时服务并用项目已有的无头测试或 HTTP 检查验证。不要访问公网。

逐项判断验收标准，并给出来自代码、测试或命令输出的简短证据。先寻找无需浏览器或监听端口的等价自动检查，不要反复执行已被环境明确禁止的命令。如果某项确实只能由真实浏览器人工核验，标记 unclear，并在 evidence 中写出用户可在目标目录执行的准确验证步骤；环境限制不是代码失败。只要存在 failed 或 unclear，passed 必须为 false。results 必须覆盖每个验收 ID，严格输出符合 JSON Schema 的对象。

原始目标：
{goal}

主要工作目录：{working_subdir}

验收标准：
{criteria}
""".format(
            goal=task["goal"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            criteria=criteria,
        )

        def event(kind, message, payload):
            self.store.add_event(task_id, kind, message, payload=payload)

        try:
            role = task["role_settings"].get("verifier", {})
            executor = role.get("executor")
            if not executor:
                raise ValueError("任务尚未指定最终验收执行器。")
            executor, verifier_adapter = self._executor_adapter(
                executor, VERIFY_CAPABILITY
            )
            verification_worktree = self._create_verification_worktree(task)
            try:
                verified_commit = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=verification_worktree
                ).stdout.strip()
                # Codex gets localhost-only networking in a disposable copy.
                # Claude remains in its no-prompt read-only tool profile.
                codex_runtime_check = executor == "codex"
                code, session_id, final = self._run_agent(
                    task_id,
                    "task-%s-verifier" % task_id,
                    verification_worktree,
                    prompt,
                    "workspace_write" if codex_runtime_check else "read_only",
                    executor=executor,
                    capability=VERIFY_CAPABILITY,
                    schema=self._schema_for_adapter(verifier_adapter, VERIFIER_SCHEMA),
                    on_event=event,
                    model=role.get("model") or None,
                    effort=role.get("reasoningEffort") or None,
                    local_network=codex_runtime_check,
                )
            finally:
                self._remove_verification_worktree(task, verification_worktree)
            self.store.update_task(task_id, verifier_thread_id=session_id)
            if code:
                detail = compact_error(final, 800)
                raise RuntimeError(
                    "验收 Agent 退出，代码 %s。%s"
                    % (code, (" 原因：%s" % detail) if detail else "")
                )
            verification = self.parse_json_result(final)
            expected = {item["id"] for item in task["plan"]["acceptance"]}
            actual = {str(item.get("id")) for item in verification.get("results", [])}
            passed = bool(verification.get("passed")) and expected == actual and all(
                item.get("status") == "passed" for item in verification.get("results", [])
            )
            with self._task_lock(task_id):
                current_task = self.require_task(task_id)
                plan = dict(current_task.get("plan") or {})
                plan["verification"] = verification
                plan["verificationCommit"] = verified_commit
                self.store.update_task(task_id, plan_json=json.dumps(plan, ensure_ascii=False))
                if not passed:
                    self._transition_task(
                        task_id, "needs_attention", "task.attention",
                        "最终验收未通过，请查看验收证据。",
                        error=verification.get("summary", "验收未通过。"),
                    )
                elif current_task.get("delivered_commit"):
                    self._transition_task(
                        task_id, "completed", "task.completed",
                        "重新验收通过，成果已在目标目录。",
                        error=None,
                        payload={"deliveredCommit": current_task["delivered_commit"]},
                    )
                else:
                    try:
                        delivered_commit = self._deliver_integration(task_id)
                    except DeliveryReverificationRequired as exc:
                        self._queue_delivery_reverification(task_id, exc)
                        return
                    except Exception as exc:
                        self._transition_task(
                            task_id, "needs_attention", "task.delivery.error",
                            "最终验收通过，但成果未能交付到目标目录：%s"
                            % compact_error(exc),
                            error=compact_error(exc),
                        )
                    else:
                        try:
                            deleted_branches = self._cleanup_task_git(task_id)
                        except Exception as exc:
                            self._transition_task(
                                task_id, "needs_attention", "task.cleanup.error",
                                "成果已交付，但临时 Git 数据清理失败：%s"
                                % compact_error(exc),
                                error=compact_error(exc),
                                payload={"deliveredCommit": delivered_commit},
                            )
                        else:
                            self._transition_task(
                                task_id, "completed", "task.completed",
                                "最终验收通过，成果已交付；临时 Worktree 和分支已清理。",
                                error=None,
                                payload={
                                    "deliveredCommit": delivered_commit,
                                    "deletedBranches": deleted_branches,
                                },
                            )
        except Exception as exc:
            if self.require_task(task_id)["cancel_requested"]:
                self._transition_task(
                    task_id, "cancelled", "task.cancelled", "任务已取消。"
                )
            else:
                self._transition_task(
                    task_id, "needs_attention", "task.error",
                    "最终验收失败：%s" % compact_error(exc), error=compact_error(exc)
                )

    def verify_task(self, task_id):
        with self._task_lock(task_id):
            return self._verify_task_locked(task_id)

    def _verify_task_locked(self, task_id):
        task = self.require_task(task_id)
        if task["status"] not in {"completed", "needs_attention", "failed"}:
            raise ValueError("当前状态不能重新验收。")
        if not task["subtasks"] or any(item["status"] != "succeeded" for item in task["subtasks"]):
            raise ValueError("只有全部子任务成功后才能重新验收。")
        self._verification_workspace(task)
        self.store.update_task(task_id, error=None, cancel_requested=0)
        self._transition_task(
            task_id, "verifying", "task.verifying", "用户要求重新执行最终验收。"
        )
        self._spawn(self._verify_task, task_id)
        return self.task_payload(task_id)

    def accept_verification(self, task_id, evidence):
        evidence = str(evidence or "").strip()
        if not evidence:
            raise ValueError("请填写你实际完成的人工验收步骤和结果。")
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] != "needs_attention":
                raise ValueError("只有自动验收存在不确定项时才能人工确认。")
            if not task["subtasks"] or any(
                item["status"] != "succeeded" for item in task["subtasks"]
            ):
                raise ValueError("子任务尚未全部成功，不能人工确认验收。")
            plan = dict(task.get("plan") or {})
            verification = dict(plan.get("verification") or {})
            results = [dict(item) for item in verification.get("results", [])]
            if not results or any(item.get("status") == "failed" for item in results):
                raise ValueError("存在明确失败项，不能用人工确认覆盖；请先修复后重试。")
            unclear = [item for item in results if item.get("status") == "unclear"]
            if not unclear:
                raise ValueError("当前验收没有可人工确认的不确定项。")
            for item in unclear:
                item["agentStatus"] = "unclear"
                item["status"] = "passed"
                item["evidence"] = "%s 人工确认：%s" % (
                    str(item.get("evidence") or "").rstrip("。"), evidence
                )
            verification.update({
                "passed": True,
                "summary": "自动验收未发现明确失败；用户已完成人工运行时验收：%s" % evidence,
                "results": results,
                "manualReview": {"evidence": evidence, "confirmedAt": utc_now()},
            })
            plan["verification"] = verification
            self.store.update_task(
                task_id, plan_json=json.dumps(plan, ensure_ascii=False), error=None
            )
            self.store.add_event(
                task_id, "verification.manual",
                "用户已为自动验收的不确定项补充人工证据。",
                payload={"evidence": evidence},
            )
            self.store.enqueue_notification(
                task_id, "verification.manual", "用户已确认人工验收通过。"
            )
            self._write_docs_unlocked(task_id)
            try:
                return self.deliver_task(task_id)
            except ValueError:
                # The manual evidence remains recorded. A dirty target branch or
                # another delivery guard can be fixed before clicking Apply.
                return self.task_payload(task_id)

    def deliver_task(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            verification = (task.get("plan") or {}).get("verification") or {}
            if not verification.get("passed"):
                raise ValueError("只有最终验收通过的任务可以交付。")
            if not task["subtasks"] or any(
                item["status"] != "succeeded" for item in task["subtasks"]
            ):
                raise ValueError("只有全部子任务成功后才能交付。")
            try:
                delivered_commit = (
                    task.get("delivered_commit") or self._deliver_integration(task_id)
                )
            except DeliveryReverificationRequired as exc:
                self._queue_delivery_reverification(task_id, exc)
                return self.task_payload(task_id)
            except Exception as exc:
                self._transition_task(
                    task_id, "needs_attention", "task.delivery.error",
                    "成果未能交付到目标目录：%s" % compact_error(exc),
                    error=compact_error(exc),
                )
                raise ValueError(compact_error(exc)) from exc
            try:
                deleted_branches = self._cleanup_task_git(task_id)
            except Exception as exc:
                self._transition_task(
                    task_id, "needs_attention", "task.cleanup.error",
                    "成果已交付，但临时 Git 数据清理失败：%s"
                    % compact_error(exc),
                    error=compact_error(exc),
                    payload={"deliveredCommit": delivered_commit},
                )
                raise ValueError(compact_error(exc)) from exc
            self._transition_task(
                task_id, "completed", "task.delivered",
                "成果已交付；临时 Worktree 和分支已清理。",
                error=None,
                payload={
                    "deliveredCommit": delivered_commit,
                    "deletedBranches": deleted_branches,
                },
            )
            return self.task_payload(task_id)

    def cleanup_task_git(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] in {"planning", "running", "verifying"}:
                raise ValueError("任务仍在运行，请先停止任务。")
            deleted_branches = self._cleanup_task_git(task_id)
            refreshed = self.require_task(task_id)
            verification = (refreshed.get("plan") or {}).get("verification") or {}
            if refreshed.get("delivered_commit") and verification.get("passed"):
                self._transition_task(
                    task_id, "completed", "task.cleaned",
                    "临时 Worktree 和任务分支已清理。",
                    error=None,
                    payload={"deletedBranches": deleted_branches},
                )
            else:
                self.store.add_event(
                    task_id,
                    "task.cleaned",
                    "临时 Worktree 和任务分支已清理。",
                    payload={"deletedBranches": deleted_branches},
                )
                self._write_docs_unlocked(task_id)
            return self.task_payload(task_id)

    def cancel_task(self, task_id):
        with self._task_lock(task_id):
            return self._cancel_task_locked(task_id)

    def _cancel_task_locked(self, task_id):
        task = self.require_task(task_id)
        if task["status"] not in {"planning", "running", "verifying"}:
            raise ValueError("当前任务不在运行。")
        self.store.update_task(task_id, cancel_requested=1)
        self.agent_gate.wake_all()
        self.store.add_event(task_id, "task.cancel", "已请求取消，正在停止运行中的 Agent。")
        keys = ["task-%s-planner" % task_id, "task-%s-verifier" % task_id]
        keys.extend(
            self._subtask_run_key(item["id"], item["dispatch_id"])
            for item in task["subtasks"]
            if item.get("dispatch_id")
        )
        for key in keys:
            try:
                with self.active_run_lock:
                    adapter = self.active_run_adapters.get(key)
                if adapter:
                    adapter.interrupt(key)
            except Exception as exc:
                self.store.add_event(task_id, "task.cancel.error", compact_error(exc))
        self._spawn(self._finish_cancel, task)
        return self.task_payload(task_id)

    def _finish_cancel(self, task):
        current = self.require_task(task["id"])
        if current["status"] == "cancelled":
            return
        for item in current["subtasks"]:
            if item["status"] in {"pending", "running", "blocked"}:
                self._update_subtask_state(
                    task["id"], item["id"], status="cancelled", error="用户取消任务。"
                )
        self._transition_task(
            task["id"], "cancelled", "task.cancelled", "任务已取消。"
        )

    @staticmethod
    def _terminate_process(process, platform=None):
        if process.poll() is not None:
            return
        platform = os.name if platform is None else platform
        if platform == "nt":
            break_signal = getattr(signal, "CTRL_BREAK_EVENT", None)
            try:
                if break_signal is None:
                    raise OSError("CTRL_BREAK_EVENT is unavailable")
                process.send_signal(break_signal)
            except (AttributeError, OSError, ValueError):
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError, PermissionError):
                process.kill()
            process.wait(timeout=2)

    def retry_subtask(self, subtask_id, executor=None, model=None, effort=None,
                      message=None):
        message = str(message or "").strip()
        task, subtask = self.require_subtask(subtask_id)
        if subtask["status"] not in {"failed", "blocked", "cancelled"}:
            raise ValueError("只有失败、阻塞或取消的子任务可以重试。")
        with self._task_lock(task["id"]):
            retry_executor = subtask.get("executor")
            retry_model = subtask.get("model") or ""
            if executor is not None or model is not None or effort is not None:
                current_snapshot = subtask.get("executor_snapshot") or {}
                selected_executor = (
                    str(executor or "").strip()
                    or subtask.get("executor")
                    or current_snapshot.get("executor")
                )
                selected_model = (
                    model if model is not None
                    else subtask.get("model") or current_snapshot.get("model")
                )
                selected_effort = (
                    effort if effort is not None
                    else subtask.get("reasoning_effort")
                    or current_snapshot.get("reasoningEffort")
                    or "high"
                )
                retry_executor, retry_snapshot = self._assign_subtask_executor(
                    subtask, selected_executor, selected_model, selected_effort
                )
                retry_model = retry_snapshot.get("model") or ""
            self.store.update_subtask(
                subtask_id,
                status="pending",
                error=None,
                exit_code=None,
                dispatch_id=None,
                session_id=None,
                pending_message=message or None,
            )
            for item in task["subtasks"]:
                if item["status"] == "blocked":
                    self.store.update_subtask(
                        item["id"], status="pending", error=None, dispatch_id=None
                    )
            self.store.update_task(task["id"], cancel_requested=0)
            self._transition_task(
                task["id"], "running", "subtask.retry",
                "已安排重试 %s（%s / %s）。" % (
                    subtask["title"],
                    EXECUTOR_LABELS.get(str(retry_executor or "").lower(), "原执行器"),
                    retry_model or "默认模型",
                ),
                subtask_id=subtask_id,
            )
        self._ensure_scheduler(task["id"])
        return self.task_payload(task["id"])

    def revise_subtask(self, subtask_id, message, executor=None, model=None,
                       effort=None):
        message = str(message or "").strip()
        if not message:
            raise ValueError("新增要求不能为空。")
        task, subtask = self.require_subtask(subtask_id)
        if subtask["status"] != "succeeded":
            raise ValueError("只有已经成功完成的子任务可以修改并重跑。")
        worktree = subtask.get("worktree")
        if task.get("cleaned_at") or not worktree or not Path(worktree).is_dir():
            raise ValueError("原 Agent 现场已清理，请使用‘继续修改’创建新任务。")
        if task["status"] not in {"needs_attention", "failed", "cancelled"}:
            raise ValueError("请等待任务暂停后再修改已完成的子任务。")
        with self._task_lock(task["id"]):
            current = self.require_task(task["id"])
            if current.get("delivered_commit"):
                workspace = self._resolve_workspace(current["workspace"])
                branch = self._git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=workspace,
                    check=False,
                ).stdout.strip()
                head = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=workspace
                ).stdout.strip()
                if branch != current.get("base_branch") or head != current.get(
                    "delivered_commit"
                ):
                    raise ValueError(
                        "目标目录已离开原交付提交，请使用‘继续修改’创建新任务。"
                    )
                self.store.update_task(
                    task["id"], base_commit=head, delivered_commit=None,
                    delivered_at=None,
                )

            snapshot = subtask.get("executor_snapshot") or {}
            selected_executor = (
                str(executor or "").strip()
                or subtask.get("executor")
                or snapshot.get("executor")
            )
            selected_model = (
                model if model is not None
                else subtask.get("model") or snapshot.get("model")
            )
            selected_effort = (
                effort if effort is not None
                else subtask.get("reasoning_effort")
                or snapshot.get("reasoningEffort")
                or "high"
            )
            selected_executor, selected_snapshot = self._assign_subtask_executor(
                subtask, selected_executor, selected_model, selected_effort
            )
            dispatch_id = uuid.uuid4().hex
            plan = dict(current.get("plan") or {})
            plan.pop("verification", None)
            self.store.update_task(
                task["id"], cancel_requested=0,
                plan_json=json.dumps(plan, ensure_ascii=False),
            )
            self.store.update_subtask(
                subtask_id,
                status="running",
                attempt=int(subtask.get("attempt") or 0) + 1,
                error=None,
                exit_code=None,
                dispatch_id=dispatch_id,
                session_id=None,
                pending_message=None,
            )
            self._transition_task(
                task["id"], "running", "subtask.revise",
                "按新增要求重新执行 %s（%s / %s，Dispatch %s）。" % (
                    subtask["title"], EXECUTOR_LABELS[selected_executor],
                    selected_snapshot.get("model") or "默认模型",
                    dispatch_id[:8],
                ),
                subtask_id=subtask_id,
            )
            refreshed = next(
                item for item in self.require_task(task["id"])["subtasks"]
                if item["id"] == subtask_id
            )
            thread = self._spawn(
                self._run_subtask, task["id"], refreshed, message, dispatch_id, False
            )
            if thread is None:
                self.store.update_subtask(
                    subtask_id, status="failed", error="服务正在关闭，未启动重跑。"
                )
                raise ValueError("服务正在关闭，未启动重跑。")
        return self.task_payload(task["id"])

    def _assign_subtask_executor(self, subtask, executor, model=None, effort=None):
        executor, adapter = self._executor_adapter(executor, WRITE_CAPABILITY)
        selected_model = str(model or "").strip()
        available = {
            str(item.get("model") or item.get("slug") or item.get("id"))
            for item in adapter.models() if isinstance(item, dict)
        }
        if selected_model and available and selected_model not in available:
            raise ValueError("模型 %s 不属于 %s。" % (selected_model, EXECUTOR_LABELS[executor]))
        selected_effort = str(effort or "high").lower()
        if selected_effort not in EFFORTS:
            raise ValueError("推理等级无效。")
        snapshot = self._executor_snapshot(executor, selected_model or None, selected_effort)
        self.store.update_subtask(
            subtask["id"],
            executor=executor,
            executor_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            model=snapshot.get("model") or None,
            reasoning_effort=selected_effort,
        )
        return executor, snapshot

    def update_subtask_executor(self, subtask_id, executor, model=None, effort=None):
        task, subtask = self.require_subtask(subtask_id)
        if task["status"] != "awaiting_approval" or subtask["attempt"]:
            raise ValueError("只有等待确认且尚未执行的子任务可以更换执行器。")
        with self._task_lock(task["id"]):
            executor, snapshot = self._assign_subtask_executor(
                subtask, executor, model, effort
            )
            self.store.add_event(
                task["id"], "subtask.executor", "%s 改用 %s。" % (
                    subtask["plan_key"], EXECUTOR_LABELS[executor]
                ), subtask_id=subtask_id,
            )
            self.store.enqueue_notification(
                task["id"], "subtask.executor",
                "%s 已分配给 %s。" % (
                    subtask["plan_key"], EXECUTOR_LABELS[executor]
                ),
            )
            self._write_docs_unlocked(task["id"])
        return self.task_payload(task["id"])

    def update_subtask_plan(self, subtask_id, title, instructions, executor,
                            model=None, effort=None):
        """Edit one generated subtask before the user approves execution."""
        title = str(title or "").strip()
        instructions = str(instructions or "").strip()
        if not title:
            raise ValueError("子任务简介不能为空。")
        if len(title) > 80:
            raise ValueError("子任务简介不能超过 80 个字符。")
        if not instructions:
            raise ValueError("子任务具体要求不能为空。")
        task, subtask = self.require_subtask(subtask_id)
        if task["status"] != "awaiting_approval" or subtask["attempt"]:
            raise ValueError("只有等待确认且尚未执行的子任务可以修改。")
        with self._task_lock(task["id"]):
            selected_executor, snapshot = self._assign_subtask_executor(
                subtask, executor, model, effort
            )
            self.store.update_subtask(
                subtask_id, title=title, instructions=instructions
            )
            current = self.require_task(task["id"])
            plan = dict(current.get("plan") or {})
            planned_subtasks = [dict(item) for item in plan.get("subtasks", [])]
            for item in planned_subtasks:
                if item.get("key") == subtask["plan_key"]:
                    item.update({
                        "title": title,
                        "instructions": instructions,
                        "executor": selected_executor,
                    })
                    break
            plan["subtasks"] = planned_subtasks
            self.store.update_task(
                task["id"], plan_json=json.dumps(plan, ensure_ascii=False)
            )
            self.store.add_event(
                task["id"], "subtask.updated",
                "%s 的内容和执行设置已更新（%s / %s）。" % (
                    subtask["plan_key"], EXECUTOR_LABELS[selected_executor],
                    snapshot.get("model") or "默认模型",
                ),
                subtask_id=subtask_id,
            )
            self._write_docs_unlocked(task["id"])
        return self.task_payload(task["id"])

    def update_task_verifier(self, task_id, executor, model=None, effort=None):
        task = self.require_task(task_id)
        retrying_verification = (
            task["status"] in {"needs_attention", "failed"}
            and bool(task["subtasks"])
            and all(item["status"] == "succeeded" for item in task["subtasks"])
        )
        if task["status"] != "awaiting_approval" and not retrying_verification:
            raise ValueError(
                "只有确认执行前，或全部子任务成功后的重新验收阶段，"
                "可以选择验收执行器。"
            )
        executor, adapter = self._executor_adapter(executor, VERIFY_CAPABILITY)
        selected_model = str(model or "").strip()
        available = {
            str(item.get("model") or item.get("slug") or item.get("id"))
            for item in adapter.models() if isinstance(item, dict)
        }
        if selected_model and available and selected_model not in available:
            raise ValueError("模型 %s 不属于 %s。" % (selected_model, EXECUTOR_LABELS[executor]))
        selected_effort = str(effort or "high").lower()
        if selected_effort not in EFFORTS:
            raise ValueError("验收推理等级无效。")
        snapshot = self._executor_snapshot(executor, selected_model or None, selected_effort)
        verifier = {
            "executor": executor,
            "name": EXECUTOR_LABELS[executor],
            "kind": adapter.kind,
            "model": snapshot.get("model") or "",
            "reasoningEffort": selected_effort,
            "snapshot": snapshot,
        }
        with self._task_lock(task_id):
            current = self.require_task(task_id)
            roles = dict(current.get("role_settings") or {})
            roles["verifier"] = verifier
            self.store.update_task(
                task_id, role_settings_json=json.dumps(roles, ensure_ascii=False)
            )
            self.store.add_event(
                task_id, "task.verifier",
                "最终验收将由 %s 执行。" % EXECUTOR_LABELS[executor],
            )
            self.store.enqueue_notification(
                task_id, "task.verifier",
                "最终验收将由 %s 执行。" % EXECUTOR_LABELS[executor],
            )
            self._write_docs_unlocked(task_id)
        return self.task_payload(task_id)

    def message_subtask(self, subtask_id, message):
        task, subtask = self.require_subtask(subtask_id)
        message = str(message or "").strip()
        if not message:
            raise ValueError("追加指令不能为空。")
        if subtask["status"] in {"running", "pending"}:
            raise ValueError("子任务正在运行，请等待当前回合结束。")
        worktree = subtask.get("worktree")
        if task.get("cleaned_at") or not worktree or not Path(worktree).is_dir():
            raise ValueError("原 Agent 现场已清理，请使用‘继续修改’创建新任务。")
        if not subtask.get("session_id"):
            raise ValueError("该子任务还没有可续跑的 Agent 会话。")
        executor = subtask.get("executor")
        if not executor:
            raise ValueError("该子任务没有记录执行器。")
        self._executor_adapter(executor, RESUME_CAPABILITY)
        with self._task_lock(task["id"]):
            current = self.require_task(task["id"])
            if current.get("delivered_commit"):
                workspace = self._resolve_workspace(current["workspace"])
                branch = self._git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=workspace,
                    check=False,
                ).stdout.strip()
                head = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=workspace
                ).stdout.strip()
                if branch != current.get("base_branch") or head != current.get(
                    "delivered_commit"
                ):
                    raise ValueError(
                        "目标目录已离开原交付提交，请使用‘继续修改’创建新任务。"
                    )
                self.store.update_task(
                    task["id"],
                    base_commit=head,
                    delivered_commit=None,
                    delivered_at=None,
                )
            self.store.update_subtask(
                subtask_id,
                status="pending",
                error=None,
                pending_message=message,
                dispatch_id=None,
            )
            self.store.update_task(task["id"], cancel_requested=0)
            self._transition_task(
                task["id"], "running", "subtask.message", "已向 %s 追加指令。" % subtask["title"],
                subtask_id=subtask_id,
            )
        self._ensure_scheduler(task["id"])
        return self.task_payload(task["id"])

    def require_task(self, task_id):
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("找不到任务。")
        return task

    def require_subtask(self, subtask_id):
        task_id = subtask_id.split(":", 1)[0]
        task = self.require_task(task_id)
        for item in task["subtasks"]:
            if item["id"] == subtask_id:
                return task, item
        raise KeyError("找不到子任务。")

    def task_payload(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            repository = self.store.get_repository(task.get("repository_id"))
            task["repository_alias"] = (
                repository["alias"] if repository else Path(task["workspace"]).name
            )
            docs = {}
            folder = self.data_dir / "tasks" / task_id
            for name in ("task.md", "progress.md", "acceptance.md"):
                path = folder / name
                docs[name] = path.read_text(encoding="utf-8") if path.exists() else ""
            task["documents"] = docs
            return self._public_task(task)

    def list_payload(self):
        return [self._public_task(task) for task in self.store.list_tasks()]

    @staticmethod
    def _public_task(task):
        task["role_settings"] = {
            role: {
                key: value for key, value in setting.items()
                if key in {
                    "executor", "name", "model", "reasoningEffort", "backend",
                    "kind", "source", "readOnly",
                }
            }
            for role, setting in (task.get("role_settings") or {}).items()
        }
        for item in task.get("subtasks", []):
            snapshot = item.get("executor_snapshot", {}) or {}
            item["executor"] = item.get("executor") or snapshot.get("executor")
            item["executor_snapshot"] = {
                key: value for key, value in snapshot.items()
                if key in {"executor", "name", "model", "reasoningEffort"}
            }
            # Never expose columns left behind by a database created by an older build.
            item.pop("agent_profile_id", None)
            item.pop("agent_profile_snapshot_json", None)
            item.pop("planned_skills", None)
            item.pop("injected_skills", None)
        return task

    def write_docs(self, task_id):
        with self._task_lock(task_id):
            self._write_docs_unlocked(task_id)

    def _write_docs_unlocked(self, task_id):
        task = self.store.get_task(task_id)
        if not task:
            return
        folder = self.data_dir / "tasks" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        plan = task.get("plan") or {}
        repository = self.store.get_repository(task.get("repository_id")) or {
            "alias": Path(task["workspace"]).name
        }
        subtask_lines = []
        for item in task["subtasks"]:
            dependencies = ", ".join(item["dependencies"]) or "无"
            snapshot = item.get("executor_snapshot") or {}
            subtask_lines.append(
                "### %s · %s\n\n- 依赖：%s\n- 权重：%s\n"
                "- 执行器：`%s`\n- 执行会话：`%s`\n- 当前 Dispatch：`%s`\n"
                "- 模型：`%s`（%s）\n\n%s"
                % (
                    item["plan_key"], item["title"], dependencies, item["weight"],
                    snapshot.get("name") or EXECUTOR_LABELS.get(item.get("executor")) or "尚未指定",
                    item.get("session_id") or "尚未创建",
                    item.get("dispatch_id") or "尚未创建",
                    item.get("model") or "默认",
                    item.get("reasoning_effort") or "默认", item["instructions"],
                )
            )
        role_rows = []
        for role in ROLE_NAMES:
            setting = task["role_settings"].get(role, {})
            if not setting:
                continue
            role_rows.append(
                "- %s：`%s` / `%s`（%s%s）" % (
                    role, setting.get("name") or EXECUTOR_LABELS.get(setting.get("executor")) or "未指定",
                    setting.get("model") or "默认模型",
                    setting.get("reasoningEffort") or "默认推理等级",
                    "；后端 %s" % setting.get("backend") if setting.get("backend") else "",
                )
            )
        task_doc = """# {title}

## 任务目标

{goal}

## 限制条件

{constraints}

## 仓库上下文

- 来源渠道：`{source}`
- 来源任务：`{parent_task}`
- 仓库别名：`{repository_alias}`
- 仓库：`{workspace}`
- 工作目录：`{working_subdir}`
- 基础分支：`{base_branch}`
- 基础提交：`{base_commit}`
- 任务分支：`{integration_branch}`
- 交付提交：`{delivered_commit}`
- 交付时间：`{delivered_at}`
- 临时数据清理：`{cleaned_at}`

## 规划与验收

{role_settings}

- 规划线程：`{planner_thread}`
- 验收线程：`{verifier_thread}`

## 计划摘要

{summary}

## 任务分配 Skill

- `orchestration`
- 来源：`https://github.com/stablyai/orca/tree/main/skills/orchestration`
- 运行方式：FastLab 结构化计划适配版；实际并行由 FastLab Worktree 调度器执行。

## 子任务

{subtasks}
""".format(
            title=task["title"],
            goal=task["goal"],
            constraints=task["constraints_text"] or "无",
            source=task.get("source_channel") or "web",
            parent_task=task.get("parent_task_id") or "无",
            repository_alias=repository["alias"],
            workspace=task["workspace"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            base_branch=task.get("base_branch") or "尚未确认",
            base_commit=task.get("base_commit") or "尚未确认",
            integration_branch=task.get("integration_branch") or "尚未创建",
            delivered_commit=task.get("delivered_commit") or "尚未交付",
            delivered_at=task.get("delivered_at") or "尚未交付",
            cleaned_at=task.get("cleaned_at") or "尚未清理",
            role_settings="\n".join(role_rows) or "- 验收执行器尚待用户在计划生成后指定。",
            planner_thread=task.get("planner_thread_id") or "尚未创建",
            verifier_thread=task.get("verifier_thread_id") or "尚未创建",
            summary=plan.get("summary", "计划生成中。"),
            subtasks="\n\n".join(subtask_lines) or "计划生成中。",
        )
        rows = [
            "| 子任务 | 状态 | 权重 | 尝试次数 | Dispatch |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        for item in task["subtasks"]:
            rows.append(
                "| %s · %s | %s | %s | %s | %s |"
                % (
                    item["plan_key"], item["title"], item["status"],
                    item["weight"], item["attempt"],
                    (item.get("dispatch_id") or "—")[:12],
                )
            )
        progress_doc = """# 任务进度

- 来源渠道：`{source}`
- 仓库别名：`{repository_alias}`
- 工作目录：`{working_subdir}`
- 当前状态：`{status}`
- 完成度：**{progress}%**
- 最后更新：{updated}
- 当前问题：{error}
- 目标目录交付：{delivery}
- 临时数据：{cleanup}

## 子任务状态

{rows}
""".format(
            status=task["status"],
            source=task.get("source_channel") or "web",
            repository_alias=repository["alias"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            progress=task["progress"],
            updated=task["updated_at"],
            error=task.get("error") or "无",
            delivery=(
                "已交付 `%s`" % task["delivered_commit"]
                if task.get("delivered_commit") else "尚未交付"
            ),
            cleanup="已清理" if task.get("cleaned_at") else "保留中",
            rows="\n".join(rows) if task["subtasks"] else "计划生成中。",
        )
        verification = plan.get("verification") or {}
        result_map = {str(item.get("id")): item for item in verification.get("results", [])}
        acceptance_lines = []
        for item in plan.get("acceptance", []):
            result = result_map.get(item["id"])
            marker = "x" if result and result.get("status") == "passed" else " "
            evidence = "\n  - 证据：%s" % result.get("evidence") if result else ""
            acceptance_lines.append(
                "- [%s] **%s** — %s%s" % (marker, item["id"], item["criterion"], evidence)
            )
        acceptance_doc = """# 验收标准

- 工作目录：`{working_subdir}`
- 验收线程：`{thread}`
- 验收模型：`{model}`（{effort}）
- 目标目录交付：{delivery}
- 临时数据：{cleanup}

{criteria}

## 验收结论

{summary}
""".format(
            criteria="\n".join(acceptance_lines) or "计划生成中。",
            working_subdir=task.get("working_subdir") or "仓库根目录",
            summary=verification.get("summary", "尚未执行最终验收。"),
            thread=task.get("verifier_thread_id") or "尚未创建",
            model=task["role_settings"].get("verifier", {}).get("model") or "默认模型",
            effort=task["role_settings"].get("verifier", {}).get("reasoningEffort") or "默认推理等级",
            delivery=(
                "已交付 `%s`" % task["delivered_commit"]
                if task.get("delivered_commit") else "尚未交付"
            ),
            cleanup="已清理" if task.get("cleaned_at") else "保留中",
        )
        self._atomic_write(folder / "task.md", task_doc)
        self._atomic_write(folder / "progress.md", progress_doc)
        self._atomic_write(folder / "acceptance.md", acceptance_doc)

    @staticmethod
    def _atomic_write(path, content):
        temporary = path.with_suffix(path.suffix + ".%s.tmp" % uuid.uuid4().hex)
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        os.replace(str(temporary), str(path))

    def shutdown(self):
        with self.thread_lock:
            self.shutting_down = True
        self.agent_gate.wake_all()
        if self.feishu:
            self.feishu.stop()
        current = threading.current_thread()
        deadline = time.time() + 6
        while time.time() < deadline:
            with self.thread_lock:
                threads = [
                    thread
                    for thread in self.background_threads
                    if thread is not current and thread.is_alive()
                ]
            if not threads:
                break
            for thread in threads:
                thread.join(timeout=min(0.5, max(0, deadline - time.time())))
        adapters = set(self.executors.values())
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            self.planner.close()
        except Exception:
            pass


class FastLabHandler(BaseHTTPRequestHandler):
    app = None
    server_version = "FastLab/0.1"

    def do_GET(self):
        try:
            path = unquote(urlparse(self.path).path)
            if path == "/api/health":
                return self.json_response(200, self.app.health_payload())
            if path == "/api/repositories":
                return self.json_response(200, {"repositories": self.app.list_repositories()})
            if path == "/api/help/feishu":
                return self.json_response(200, feishu_help_payload())
            if path == "/api/settings/executors":
                return self.json_response(200, self.app.executor_settings())
            if path == "/api/tasks":
                return self.json_response(200, {"tasks": self.app.list_payload()})
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)", path)
            if match:
                return self.json_response(200, self.app.task_payload(match.group(1)))
            if path.startswith("/api/"):
                return self.json_response(404, {"error": "接口不存在。"})
            return self.serve_static(path)
        except KeyError as exc:
            self.json_response(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            self.json_response(500, {"error": compact_error(exc)})

    def do_POST(self):
        try:
            path = unquote(urlparse(self.path).path)
            body = self.read_json()
            if path == "/api/tasks":
                task = self.app.create_task(
                    body.get("title"),
                    body.get("goal"),
                    body.get("constraints"),
                    body.get("maxConcurrency", DEFAULT_GLOBAL_CONCURRENCY),
                    body.get("workspace"),
                    body.get("repositoryId"),
                    working_subdir=body.get("workingSubdir", ""),
                )
                return self.json_response(202, task)
            if path == "/api/repositories":
                result = self.app.add_repository(
                    body.get("alias"), body.get("path"), body.get("isDefault", False),
                    body.get("initialize", False),
                )
                return self.json_response(201, result)
            match = re.fullmatch(r"/api/repositories/([a-f0-9]+)/initialize", path)
            if match:
                return self.json_response(
                    200, self.app.initialize_repository(match.group(1))
                )
            match = re.fullmatch(
                r"/api/tasks/([a-f0-9]+)/(replan|start|cancel|verify|accept-verification|rerun|continue|deliver|cleanup)",
                path,
            )
            if match:
                task_id, action = match.groups()
                if action == "replan":
                    result = self.app.replan_task(task_id, body.get("feedback"))
                elif action == "start":
                    result = self.app.start_task(task_id)
                elif action == "verify":
                    result = self.app.verify_task(task_id)
                elif action == "accept-verification":
                    result = self.app.accept_verification(task_id, body.get("evidence"))
                elif action == "rerun":
                    result = self.app.rerun_task(task_id)
                elif action == "continue":
                    result = self.app.continue_task(task_id, body.get("message"))
                elif action == "deliver":
                    result = self.app.deliver_task(task_id)
                elif action == "cleanup":
                    result = self.app.cleanup_task_git(task_id)
                else:
                    result = self.app.cancel_task(task_id)
                return self.json_response(202, result)
            match = re.fullmatch(r"/api/subtasks/([^/]+)/(retry|message|revise)", path)
            if match:
                subtask_id, action = match.groups()
                if action == "retry":
                    result = self.app.retry_subtask(
                        subtask_id, body.get("executor"), body.get("model"),
                        body.get("reasoningEffort"), body.get("message"),
                    )
                elif action == "revise":
                    result = self.app.revise_subtask(
                        subtask_id, body.get("message"), body.get("executor"),
                        body.get("model"), body.get("reasoningEffort"),
                    )
                else:
                    result = self.app.message_subtask(subtask_id, body.get("message"))
                return self.json_response(202, result)
            self.json_response(404, {"error": "接口不存在。"})
        except RepositoryInitializationRequired as exc:
            self.json_response(409, {
                "error": str(exc),
                "code": "repository_initialization_required",
                "path": exc.path,
                "reason": exc.reason,
            })
        except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
            self.json_response(400, {"error": compact_error(exc)})
        except KeyError as exc:
            self.json_response(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            traceback.print_exc()
            self.json_response(500, {"error": compact_error(exc)})

    def do_PUT(self):
        try:
            path = unquote(urlparse(self.path).path)
            body = self.read_json()
            if path == "/api/settings/executors":
                return self.json_response(200, self.app.update_executor_settings(body))
            match = re.fullmatch(r"/api/subtasks/([^/]+)/executor", path)
            if match:
                return self.json_response(200, self.app.update_subtask_executor(
                    match.group(1), body.get("executor"), body.get("model"),
                    body.get("reasoningEffort"),
                ))
            match = re.fullmatch(r"/api/subtasks/([^/]+)", path)
            if match:
                return self.json_response(200, self.app.update_subtask_plan(
                    match.group(1), body.get("title"), body.get("instructions"),
                    body.get("executor"), body.get("model"),
                    body.get("reasoningEffort"),
                ))
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)/verifier", path)
            if match:
                return self.json_response(200, self.app.update_task_verifier(
                    match.group(1), body.get("executor"), body.get("model"),
                    body.get("reasoningEffort"),
                ))
            match = re.fullmatch(r"/api/repositories/([a-f0-9]+)", path)
            if match:
                result = self.app.update_repository(
                    match.group(1), body.get("alias"), body.get("path"),
                    body.get("isDefault", False),
                )
                return self.json_response(200, result)
            self.json_response(404, {"error": "接口不存在。"})
        except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
            self.json_response(400, {"error": compact_error(exc)})
        except KeyError as exc:
            self.json_response(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            traceback.print_exc()
            self.json_response(500, {"error": compact_error(exc)})

    def do_DELETE(self):
        try:
            path = unquote(urlparse(self.path).path)
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)/events", path)
            if match:
                return self.json_response(200, self.app.clear_task_logs(match.group(1)))
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)", path)
            if match:
                return self.json_response(200, self.app.delete_task(match.group(1)))
            match = re.fullmatch(r"/api/repositories/([a-f0-9]+)", path)
            if match:
                return self.json_response(200, self.app.delete_repository(match.group(1)))
            self.json_response(404, {"error": "接口不存在。"})
        except ValueError as exc:
            self.json_response(400, {"error": compact_error(exc)})
        except KeyError as exc:
            self.json_response(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            traceback.print_exc()
            self.json_response(500, {"error": compact_error(exc)})

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("请求内容过大。")
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def json_response(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def serve_static(self, path):
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
        except ValueError:
            return self.send_error(404)
        if not candidate.is_file():
            candidate = WEB_ROOT / "index.html"
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        path = urlparse(self.path).path
        if self.command == "GET" and (path == "/api/tasks" or path.startswith("/api/tasks/")):
            return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def build_parser():
    parser = argparse.ArgumentParser(description="FastLab local multi-agent task console")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Optional path inside the default Git repository",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", default=str(APP_ROOT / ".fastlab"))
    parser.add_argument(
        "--env-file",
        default=None,
        help="Local secrets/config file; defaults to <data-dir>/fastlab.env",
    )
    parser.add_argument(
        "--codex-bin",
        default=None,
        help="Codex CLI path; defaults to FASTLAB_CODEX_BIN or codex on PATH",
    )
    parser.add_argument(
        "--claude-bin",
        default=None,
        help="Claude CLI path; defaults to FASTLAB_CLAUDE_BIN or claude on PATH",
    )
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    env_path = Path(arguments.env_file) if arguments.env_file else Path(arguments.data_dir) / "fastlab.env"
    try:
        loaded_env = load_local_env(env_path, required=bool(arguments.env_file))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if loaded_env:
        print("Loaded local environment config: %s" % env_path.expanduser().resolve(), flush=True)
    if sys.version_info < (3, 10):
        raise SystemExit("FastLab 需要 Python 3.10 或更高版本。")
    try:
        app = FastLab(
            arguments.workspace,
            arguments.data_dir,
            arguments.codex_bin or os.environ.get("FASTLAB_CODEX_BIN"),
            claude_bin=arguments.claude_bin or os.environ.get("FASTLAB_CLAUDE_BIN"),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit("FastLab 启动失败：%s" % exc) from exc
    if app.store.repaired_event_payloads:
        print(
            "已修复 %s 条旧版本截断的日志记录。" % app.store.repaired_event_payloads,
            flush=True,
        )
    FastLabHandler.app = app
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), FastLabHandler)
    print("FastLab is running at http://127.0.0.1:%s" % server.server_address[1], flush=True)
    print("Workspace: %s" % (app.workspace or "not configured"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
