"""SQLite persistence for FastLab's stable on-disk schema."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULT_GLOBAL_CONCURRENCY, MAX_EVENT_PAYLOAD, compact_error, utc_now

class Store:
    def __init__(self, path, redactor=None):
        self.path = Path(path)
        self.redactor = redactor or (lambda value: str(value))
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
                """
            )
            self._ensure_column(db, "tasks", "repository_id", "TEXT")
            self._ensure_column(db, "tasks", "parent_task_id", "TEXT")
            self._ensure_column(db, "tasks", "working_subdir", "TEXT NOT NULL DEFAULT ''")
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
            defaults = {
                "models": {
                    "planner": {"model": "", "reasoningEffort": "high"},
                    "worker": {"model": "", "reasoningEffort": "high"},
                    "verifier": {"model": "", "reasoningEffort": "high"},
                },
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
                    repository_id=None, role_settings=None,
                    working_subdir="", parent_task_id=None, base_branch=None,
                    base_commit=None):
        task_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks "
                "(id,parent_task_id,title,goal,constraints_text,workspace,status,max_concurrency,"
                "repository_id,role_settings_json,working_subdir,base_branch,"
                "base_commit,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'planning',?,?,?,?,?,?,?,?)",
                (
                    task_id, parent_task_id, title, goal, constraints_text, workspace,
                    max_concurrency,
                    repository_id, json.dumps(role_settings or {}, ensure_ascii=False),
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
        pairs = [
            (key, self.redactor(value) if key == "error" and value is not None else value)
            for key, value in values.items() if key in allowed
        ]
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
        pairs = [
            (key, self.redactor(value) if key == "error" and value is not None else value)
            for key, value in values.items() if key in allowed
        ]
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
        pairs = [
            (key, self.redactor(value) if key == "error" and value is not None else value)
            for key, value in values.items() if key in allowed
        ]
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
        encoded = self.redactor(encoded) if encoded is not None else None
        with self.connect() as db:
            db.execute(
                "INSERT INTO events (task_id,subtask_id,kind,message,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, subtask_id, kind, compact_error(self.redactor(message), 8000), encoded, utc_now()),
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
            active = self._active_repository_tasks(db, repository_id)
            if active:
                raise ValueError("该仓库仍有未结束任务，不能删除。")
            cursor = db.execute("DELETE FROM repositories WHERE id=?", (repository_id,))
            if not cursor.rowcount:
                raise KeyError("找不到仓库。")

    @staticmethod
    def _active_repository_tasks(db, repository_id):
        return db.execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE repository_id=? "
            "AND status NOT IN ('completed','cancelled','failed')",
            (repository_id,),
        ).fetchone()["total"]

    def repository_has_active_tasks(self, repository_id):
        with self.connect() as db:
            return bool(self._active_repository_tasks(db, repository_id))

    def clear_task_events(self, task_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise KeyError("找不到任务。")
            db.execute("DELETE FROM events WHERE task_id=?", (task_id,))
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (utc_now(), task_id))

    def delete_task(self, task_id):
        with self.connect() as db:
            cursor = db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            if not cursor.rowcount:
                raise KeyError("找不到任务。")

    def transition_task(self, task_id, status, kind, message, error=None, payload=None,
                        subtask_id=None):
        now = utc_now()
        encoded_payload = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?",
                (status, self.redactor(error) if error else None, now, task_id),
            )
            db.execute(
                "INSERT INTO events (task_id,subtask_id,kind,message,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, subtask_id, kind, compact_error(self.redactor(message), 8000),
                 self.redactor(encoded_payload) if encoded_payload else None, now),
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
