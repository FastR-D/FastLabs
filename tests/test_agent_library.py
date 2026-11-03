import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_adapter import FakeAgentAdapter
from test_fastlab import fixture_app, initialize_repo, cli_planner, FAKE_CLAUDE
from fastlab.agent_library import AgentLibrary
from fastlab.storage import Store
from test_http_contract import _Handler


class AgentLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fastlab-agents-")
        self.root = Path(self.temp.name)
        self.repo = initialize_repo(self.root / "data" / "repositories" / ("a" * 32))
        self.adapter = FakeAgentAdapter()
        self.app = fixture_app(
            self.repo,
            self.root / "data",
            agent_adapter=self.adapter,
            claude_bin=FAKE_CLAUDE,
            planner=cli_planner(self.adapter),
        )
        self.library = self.app.agent_library

    def tearDown(self):
        self.app.shutdown()
        self.temp.cleanup()

    def definition(self, **changes):
        return {
            **dict(
                name="文档工程师",
                description="维护文档",
                instructions="保留用户原有内容。",
                skills="检查相对链接。",
                executor="codex",
                model="fake-fast",
                reasoningEffort="high",
                roles=["worker"],
            ),
            **changes,
        }

    def wait(self, task_id, status):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = self.app.task_payload(task_id)
            if task["status"] == status:
                return task
            if task["status"] == "failed":
                self.fail(task.get("error"))
            time.sleep(0.03)
        self.fail(str(task))

    def task(self, agents=None):
        task = self.app.create_task("Agent test", "生成结果文件", "", 2, agents=agents)
        return self.wait(task["id"], "awaiting_approval")

    def test_version_archive_and_tenant_boundary(self):
        agent = self.library.save(self.definition())
        updated = self.library.save({**agent, "instructions": "新指令"}, agent["id"])
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(ValueError):
            self.library.save(agent, agent["id"])
        with self.app.store.connect() as db:
            old = db.execute(
                "SELECT definition_json FROM agent_versions WHERE agent_id=? AND version=1",
                (agent["id"],),
            ).fetchone()
        self.assertEqual(json.loads(old[0])["instructions"], agent["instructions"])
        other = AgentLibrary(Store(self.root / "other.db"), self.app._validate_role_preference)
        with self.assertRaises(KeyError):
            other.snapshot(agent["id"], "worker")
        self.library.archive(agent["id"])
        self.assertNotIn(agent["id"], [item["id"] for item in self.library.list()])
        with self.assertRaises(KeyError):
            self.library.snapshot(agent["id"], "worker")

    def test_snapshot_survives_edit_archive_and_flows_into_runtime(self):
        agent = self.library.save(self.definition())
        task = self.task({"worker": agent["id"]})
        self.assertFalse(any("subtask" in call["key"] for call in self.adapter.calls))
        self.library.save({**agent, "instructions": "禁止进入旧任务的新版"}, agent["id"])
        self.library.archive(agent["id"])
        with patch.dict(os.environ, {"FASTLAB_FAKE_VERIFY_UNCLEAR": "1"}):
            self.app.start_task(task["id"])
            completed = self.wait(task["id"], "needs_attention")
        workers = [r for r in completed["runs"] if r["role"] == "worker"]
        self.assertEqual(len(workers), 3)
        self.assertEqual(len({r["session_id"] for r in workers}), 3)
        self.assertEqual(len({r["instance_id"] for r in workers}), 3)
        for run in workers:
            self.assertEqual(run["snapshot"]["agent"]["version"], 1)
            self.assertEqual(run["status"], "succeeded")
        for call in self.adapter.calls:
            if "-dispatch-" in call["key"]:
                self.assertIn("保留用户原有内容", call["prompt"])
                self.assertNotIn("禁止进入旧任务的新版", call["prompt"])
        child = completed["subtasks"][0]
        self.app.message_subtask(child["id"], "补充一份说明")
        resumed = self.wait(task["id"], "completed")
        history = [r for r in resumed["runs"] if r["instance_id"] == child["id"]]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["session_id"], history[1]["session_id"])
        self.assertNotEqual(history[0]["id"], history[1]["id"])
        resumed_call = next(c for c in self.adapter.calls if c["resume"] == child["session_id"])
        self.assertEqual(resumed_call["prompt"], "补充一份说明")

    def test_plan_reference_temporary_definition_and_invalid_policy(self):
        agent = self.library.save(self.definition())
        plan = FakeAgentAdapter._plan()
        plan["subtasks"][0]["agentId"] = agent["id"]
        plan["subtasks"][1]["temporaryAgent"] = self.definition(name="临时执行者")
        result = self.library.resolve_plan(plan, self.library.list(), {})
        self.assertEqual(result["subtasks"][0]["agentSnapshot"]["id"], agent["id"])
        self.assertNotIn("id", result["subtasks"][1]["agentSnapshot"])
        plan["subtasks"][0]["temporaryAgent"] = self.definition()
        with self.assertRaises(ValueError):
            self.library.resolve_plan(plan, self.library.list(), {})
        with self.assertRaises(ValueError):
            self.library.save(self.definition(permissions="unrestricted"))
        plan["subtasks"][0].pop("temporaryAgent")
        plan["subtasks"][0]["agentId"] = "another-tenant"
        with self.assertRaises(ValueError):
            self.library.resolve_plan(plan, self.library.list(), {})

    def test_assign_temporary_before_confirmation_and_save_to_library(self):
        task = self.task()
        child = task["subtasks"][0]
        edited = self.app.assign_agent(
            task["id"], "worker", subtask_id=child["id"], temporary=self.definition()
        )
        snapshot = edited["subtasks"][0]["executor_snapshot"]["agent"]
        self.assertEqual(snapshot["name"], "文档工程师")
        stored = self.library.save(snapshot)
        self.assertEqual(stored["version"], 1)
        verifier = self.library.save(self.definition(name="验收专家", roles=["verifier"]))
        edited = self.app.assign_agent(task["id"], "verifier", verifier["id"])
        self.assertEqual(edited["role_settings"]["verifier"]["agent"]["id"], verifier["id"])
        edited = self.app.update_task_verifier(task["id"], "codex", "fake-deep")
        self.assertEqual(edited["role_settings"]["verifier"]["agent"]["id"], verifier["id"])
        self.app.store.update_task(task["id"], status="running")
        with self.assertRaises(ValueError):
            self.app.assign_agent(task["id"], "worker", stored["id"], child["id"])

    def test_run_exception_timeout_and_restart_preserve_history(self):
        from fastlab.errors import AgentRunTimeout

        task = self.task()
        with self.assertRaises(AgentRunTimeout):
            with self.library.record(task["id"], "test", "worker", "codex", {}, "instance"):
                raise AgentRunTimeout("timeout")
        self.assertEqual(self.library.runs(task["id"])[-1]["status"], "timed_out")
        with self.app.store.connect() as db:
            db.execute("UPDATE agent_runs SET status='running' WHERE run_key='test'")
        AgentLibrary(self.app.store, self.app._validate_role_preference)
        self.assertEqual(self.library.runs(task["id"])[-1]["status"], "interrupted")
        self.assertTrue(Path(str(self.app.store.path) + ".before-agent-library.db").exists())
        self.assertEqual(len(self.library.list()), 3)
        self.library.seed(self.app.agent_preferences())
        self.assertEqual(len(self.library.list()), 3)

    def test_http_lifecycle_and_task_assignment(self):
        def request(method, path, body=None):
            handler = _Handler(path, self.app, None, body)
            getattr(handler, "do_" + method)()
            return handler.response

        code, agent = request("POST", "/api/agents", self.definition())
        self.assertEqual(code, 201)
        self.assertEqual(request("GET", "/api/agents/" + agent["id"])[0], 200)
        self.assertEqual(
            request("PUT", "/api/agents/" + agent["id"], {**agent, "name": "新版"})[1]["version"], 2
        )
        self.assertEqual(request("PUT", "/api/agents/" + agent["id"], agent)[0], 400)
        task = self.task()
        code, result = request(
            "POST",
            "/api/tasks/" + task["id"] + "/agents",
            {"role": "worker", "agentId": agent["id"], "subtaskId": task["subtasks"][0]["id"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(result["subtasks"][0]["executor_snapshot"]["agent"]["version"], 2)
        self.assertEqual(request("DELETE", "/api/agents/" + agent["id"])[0], 200)
        self.assertEqual(request("GET", "/api/agents/" + "0" * 32)[0], 404)

    def test_planner_uses_creation_catalog_and_generated_temporary_definition(self):
        agent = self.library.save(self.definition())
        plan = FakeAgentAdapter._plan()
        plan["subtasks"][0]["agentId"] = agent["id"]
        plan["subtasks"][1]["temporaryAgent"] = self.definition(name="规划生成的临时定义")
        plan["subtasks"][1]["temporaryAgent"].pop("roles")
        with patch.object(self.app, "_spawn"):
            task = self.app.create_task("快照测试", "生成三个结果", "", 1)
        self.library.save({**agent, "name": "后来修改的名称"}, agent["id"])
        with patch.object(self.adapter, "_plan", return_value=plan):
            self.app._plan_task(task["id"], "")
        planned = self.wait(task["id"], "awaiting_approval")
        first, second = planned["subtasks"][:2]
        self.assertEqual(first["executor_snapshot"]["agent"]["version"], 1)
        self.assertEqual(first["executor_snapshot"]["agent"]["name"], "文档工程师")
        self.assertEqual(second["executor_snapshot"]["agent"]["name"], "规划生成的临时定义")
        self.assertNotIn("id", second["executor_snapshot"]["agent"])
        self.assertEqual(len(self.library.list()), 4)
        self.assertEqual(len(planned["runs"]), 1)

    def test_migration_keeps_old_task_and_rejects_unknown_schema(self):
        store = Store(self.root / "legacy.db")
        task_id = store.create_task("旧任务", "原目标", "", str(self.repo), 1)
        store.update_task(task_id, status="awaiting_approval")
        before = store.get_task(task_id)
        library = AgentLibrary(store, self.app._validate_role_preference)
        library.seed(self.app.agent_preferences())
        self.assertEqual(store.get_task(task_id), before)
        self.assertEqual(library.runs(task_id), [])
        store.set_setting("agent.library.schema", 999)
        with self.assertRaises(ValueError):
            AgentLibrary(store, self.app._validate_role_preference)

    def test_codex_output_schema_is_strict_without_breaking_old_plans(self):
        from fastlab.agents.schema import strict_output_schema, validate_json_schema
        from test_fastlab import PLANNER_SCHEMA

        schema = json.loads(PLANNER_SCHEMA.read_text())
        validate_json_schema(FakeAgentAdapter._plan(), schema)
        strict = strict_output_schema(schema)
        item = strict["properties"]["subtasks"]["items"]
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertIn("null", item["properties"]["agentId"]["type"])
        self.assertNotIn("agentId", schema["properties"]["subtasks"]["items"]["required"])

    def test_provider_switch_starts_new_session_and_keeps_definition(self):
        agent = self.library.save(self.definition())
        task = self.task({"worker": agent["id"]})
        child = task["subtasks"][0]
        self.app.update_subtask_executor(child["id"], "claude", "sonnet")
        changed = self.app.task_payload(task["id"])["subtasks"][0]
        self.assertEqual(changed["executor"], "claude")
        self.assertIsNone(changed["session_id"])
        self.assertEqual(changed["executor_snapshot"]["agent"]["id"], agent["id"])
