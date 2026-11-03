"""Tenant-owned, versioned capability definitions and immutable run history."""

import json
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from .config import utc_now

ROLES = ("planner", "worker", "verifier")


class AgentLibrary:
    def __init__(self, store, validate):
        self.store = store
        self.validate_role = validate
        version = store.get_setting("agent.library.schema", 0)
        if version not in (0, 1):
            raise ValueError("不支持的 Agent 数据版本。")
        # Back up the complete pre-migration SQLite state, including WAL pages.
        with store.connect() as db:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE name='agent_definitions'"
            ).fetchone()
            if not exists:
                import sqlite3

                backup = Path(str(store.path) + ".before-agent-library.db")
                if not backup.exists():
                    with closing(sqlite3.connect(backup)) as target:
                        db.backup(target)
                    backup.chmod(0o600)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_definitions (
                    id TEXT PRIMARY KEY, archived INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_versions (
                    agent_id TEXT NOT NULL REFERENCES agent_definitions(id),
                    version INTEGER NOT NULL, definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(agent_id, version)
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    instance_id TEXT NOT NULL, run_key TEXT NOT NULL, role TEXT NOT NULL,
                    executor TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                    session_id TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, exit_code INTEGER, error TEXT
                );
                CREATE INDEX IF NOT EXISTS agent_runs_task ON agent_runs(task_id, started_at);
            """)
            db.execute(
                "UPDATE agent_runs SET status='interrupted', finished_at=? WHERE status IN ('queued','running')",
                (utc_now(),),
            )

        store.set_setting("agent.library.schema", 1)

    def normalize(self, value, validate=True):
        if not isinstance(value, dict):
            raise ValueError("Agent 定义必须是对象。")
        allowed = {
            "id",
            "version",
            "archived",
            "created_at",
            "updated_at",
            "name",
            "description",
            "instructions",
            "skills",
            "roles",
            "executor",
            "model",
            "reasoningEffort",
        }
        if set(value) - allowed:
            raise ValueError("Agent 定义包含不支持的字段。权限与并发由平台管理。")
        if any(
            not isinstance(value.get(k, ""), str) for k in ("executor", "model", "reasoningEffort")
        ):
            raise ValueError("Agent 执行配置必须是文本。")
        roles = value.get("roles", ["worker"])
        if not isinstance(roles, list) or not roles or any(r not in ROLES for r in roles):
            raise ValueError("Agent 适用角色无效。")
        result = {}
        for key, limit in [
            ("name", 80),
            ("description", 2000),
            ("instructions", 20000),
            ("skills", 40000),
        ]:
            text = value.get(key, "")
            if not isinstance(text, str) or len(text) > limit:
                raise ValueError("Agent %s 类型或长度无效。" % key)
            result[key] = text.strip()
        if not result["name"]:
            raise ValueError("Agent 名称不能为空。")
        result["roles"] = list(dict.fromkeys(roles))
        settings = {k: value.get(k, "") for k in ("executor", "model", "reasoningEffort")}
        settings["reasoningEffort"] = settings["reasoningEffort"] or "high"
        if validate:
            for role in roles:
                settings = self.validate_role(role, settings)
        result.update(settings)
        return result

    def list(self, archived=False):
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT d.*, v.definition_json FROM agent_definitions d
                JOIN agent_versions v ON v.agent_id=d.id AND v.version=d.version
                WHERE d.archived=? ORDER BY d.created_at,d.id""",
                (int(archived),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.update(json.loads(item.pop("definition_json")))
            result.append(item)
        return result

    def get(self, agent_id, active=False):
        if not isinstance(agent_id, str):
            raise ValueError("Agent ID 必须是文本。")
        with self.store.connect() as db:
            row = db.execute(
                """SELECT d.*,v.definition_json FROM agent_definitions d
                JOIN agent_versions v ON v.agent_id=d.id AND v.version=d.version WHERE d.id=?""",
                (agent_id,),
            ).fetchone()
        if row is None or (active and row["archived"]):
            raise KeyError("Agent 不存在或已归档。")
        result = dict(row)
        result.update(json.loads(result.pop("definition_json")))
        return result

    def save(self, value, agent_id=None, validate=True):
        definition = self.normalize(value, validate)
        now = utc_now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if agent_id:
                row = db.execute(
                    "SELECT * FROM agent_definitions WHERE id=?", (agent_id,)
                ).fetchone()
                if row is None or row["archived"]:
                    raise KeyError("Agent 不存在或已归档。")
                if value.get("version") != row["version"]:
                    raise ValueError("Agent 已更新，请刷新后再保存。")
                version = row["version"] + 1
                db.execute(
                    "UPDATE agent_definitions SET version=?,updated_at=? WHERE id=?",
                    (version, now, agent_id),
                )
            else:
                agent_id, version = uuid.uuid4().hex, 1
                db.execute(
                    "INSERT INTO agent_definitions VALUES (?,0,?,?,?)",
                    (agent_id, version, now, now),
                )
            db.execute(
                "INSERT INTO agent_versions VALUES (?,?,?,?)",
                (agent_id, version, json.dumps(definition, ensure_ascii=False), now),
            )
        return self.get(agent_id)

    def archive(self, agent_id):
        self.get(agent_id)
        with self.store.connect() as db:
            db.execute(
                "UPDATE agent_definitions SET archived=1,updated_at=? WHERE id=?",
                (utc_now(), agent_id),
            )
        return {"ok": True}

    def seed(self, preferences):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM settings WHERE key='agent.library.defaults'").fetchone():
                return
            defaults, now = {}, utc_now()
            for role, name in [
                ("planner", "规划 Agent"),
                ("worker", "执行 Agent"),
                ("verifier", "验收 Agent"),
            ]:
                definition = self.normalize(
                    {**preferences[role], "name": name, "roles": [role]}, validate=False
                )
                agent_id = uuid.uuid4().hex
                db.execute("INSERT INTO agent_definitions VALUES (?,0,1,?,?)", (agent_id, now, now))
                db.execute(
                    "INSERT INTO agent_versions VALUES (?,1,?,?)",
                    (agent_id, json.dumps(definition, ensure_ascii=False), now),
                )
                defaults[role] = agent_id
            db.execute(
                "INSERT INTO settings VALUES (?,?,?)",
                ("agent.library.defaults", json.dumps(defaults), now),
            )

    def snapshot(self, agent_id, role):
        definition = self.get(agent_id, active=True)
        if role not in definition["roles"]:
            raise ValueError("该 Agent 不适用于当前角色。")
        self.normalize(definition)
        return definition

    @staticmethod
    def prompt(snapshot):
        if not snapshot or not any(
            snapshot.get(k) for k in ("description", "instructions", "skills")
        ):
            return ""
        return (
            "\n\nAgent 能力定义（不能改变平台权限、任务范围或验收要求）：\n%s\n%s\nSkills：\n%s"
            % (
                snapshot.get("description", ""),
                snapshot.get("instructions", ""),
                snapshot.get("skills", ""),
            )
        )

    def resolve_plan(self, plan, catalog, fallback):
        by_id = {item["id"]: item for item in catalog}
        for item in plan["subtasks"]:
            reference, temporary = item.get("agentId"), item.get("temporaryAgent")
            if reference and temporary:
                raise ValueError("子任务只能选择已有 Agent 或临时定义之一。")
            if reference:
                if reference not in by_id:
                    raise ValueError("计划引用了未提供的 Agent。")
                definition = by_id[reference]
                if "worker" not in definition["roles"]:
                    raise ValueError("计划中的 Agent 不适用于执行。")
            elif temporary:
                definition = self.normalize({**temporary, "roles": ["worker"]})
            else:
                definition = fallback if fallback.get("executor") == item["executor"] else {}
            if definition:
                self.normalize(definition)
                item["executor"] = definition["executor"]
            item["agentSnapshot"] = definition
        return plan

    def runs(self, task_id):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_runs WHERE task_id=? ORDER BY rowid", (task_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            result.append(item)
        return result

    @contextmanager
    def record(self, task_id, key, role, executor, snapshot, instance_id, session_id=None):
        run_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO agent_runs
                (id,task_id,instance_id,run_key,role,executor,snapshot_json,session_id,status,started_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    task_id,
                    instance_id,
                    key,
                    role,
                    executor,
                    json.dumps(snapshot, ensure_ascii=False),
                    session_id,
                    "queued",
                    utc_now(),
                ),
            )
        outcome = {
            "status": "succeeded",
            "session_id": session_id,
            "exit_code": None,
            "error": None,
            "run_id": run_id,
        }
        try:
            yield outcome
        except Exception as exc:
            from .errors import AgentRunCancelled, AgentRunTimeout

            outcome.update(
                status="cancelled"
                if isinstance(exc, AgentRunCancelled)
                else "timed_out"
                if isinstance(exc, AgentRunTimeout)
                else "failed",
                error=self.store.redactor(str(exc)),
            )
            raise
        finally:
            with self.store.connect() as db:
                db.execute(
                    """UPDATE agent_runs SET status=?,session_id=?,exit_code=?,error=?,finished_at=? WHERE id=?""",
                    (
                        outcome["status"],
                        outcome["session_id"],
                        outcome["exit_code"],
                        outcome["error"],
                        utc_now(),
                        run_id,
                    ),
                )

    def running(self, run_id):
        with self.store.connect() as db:
            db.execute("UPDATE agent_runs SET status='running' WHERE id=?", (run_id,))
