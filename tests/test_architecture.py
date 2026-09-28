import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import fastlab
import server
from cryptography.fernet import Fernet
from fastlab.bootstrap import local_repository_root, parse_arguments
from fastlab.config import TASK_STATES, validate_environment_key_contract
from fastlab.documents import DocumentProjector
from fastlab.errors import RepositoryInitializationRequired
from fastlab.scheduling import AgentRunGate
from fastlab.storage import Store
from fastlab.runners import ProcessCommandRunner
from fastlab.tenancy import FastLabService
from fastlab.tenant_registry import Principal


class ModuleContractTests(unittest.TestCase):
    def test_root_server_keeps_historical_public_symbols(self):
        self.assertIs(server.FastLab, fastlab.FastLab)
        self.assertIs(server.Store, Store)
        self.assertIs(server.AgentRunGate, AgentRunGate)
        self.assertIs(server.RepositoryInitializationRequired, RepositoryInitializationRequired)

    def test_environment_example_and_allowlist_are_identical(self):
        self.assertTrue(validate_environment_key_contract())

    def test_env_file_is_loaded_before_port_default_and_cli_still_wins(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-bootstrap-") as folder:
            env_file = Path(folder) / "fastlab.env"
            env_file.write_text("FASTLAB_PORT=49123\n", encoding="utf-8")
            env_file.chmod(0o600)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FASTLAB_PORT", None)
                parsed, _path, _loaded = parse_arguments(["--data-dir", folder])
                self.assertEqual(parsed.port, 49123)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FASTLAB_PORT", None)
                parsed, _path, _loaded = parse_arguments(
                    ["--data-dir", folder, "--port", "49124"]
                )
                self.assertEqual(parsed.port, 49124)

    def test_normal_local_start_uses_managed_repositories_and_workspace_is_legacy(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-local-repositories-") as folder:
            root = Path(folder).resolve()
            self.assertEqual(
                local_repository_root(root), root / "repositories"
            )
            self.assertIsNone(local_repository_root(root, root / "existing-repo"))

    def test_state_and_sqlite_schema_contract_remain_stable(self):
        self.assertEqual(TASK_STATES, {
            "planning", "awaiting_approval", "running", "verifying",
            "completed", "needs_attention", "failed", "cancelled",
        })
        with tempfile.TemporaryDirectory(prefix="fastlab-schema-") as folder:
            database = Path(folder) / "fastlab.db"
            Store(database)
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                task_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(tasks)")
                }
            self.assertTrue({"repositories", "settings", "tasks", "subtasks", "events"} <= tables)
            self.assertTrue({
                "id", "parent_task_id", "repository_id", "role_settings_json",
                "working_subdir", "planner_thread_id", "verifier_thread_id",
                "delivered_commit", "delivered_at", "cleaned_at",
            } <= task_columns)

    def test_document_projection_keeps_three_stable_files(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-docs-") as folder:
            root = Path(folder)
            store = Store(root / "fastlab.db")
            task_id = store.create_task("Title", "Goal", "", str(root), 1)
            DocumentProjector(store, root).write(task_id)
            documents = {
                path.name: path.read_text(encoding="utf-8")
                for path in (root / "tasks" / task_id).iterdir()
            }
            self.assertEqual(set(documents), {"task.md", "progress.md", "acceptance.md"})
            self.assertIn("# Title", documents["task.md"])
            self.assertIn("# 任务进度", documents["progress.md"])
            self.assertIn("# 验收标准", documents["acceptance.md"])

    def test_research_service_defaults_to_process_and_one_slot_per_user(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-service-contract-") as folder:
            root = Path(folder)
            environment = {
                "FASTLAB_PUBLIC_URL": "https://research.example/lab/",
                "FASTLAB_PUBLIC_PATH": "/lab",
                "FASTLAB_CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
                "FASTLAB_TENANT_ROOT": str(root / "tenants"),
                "FASTLAB_EXECUTION_BACKEND": "process",
                "FASTLAB_GLOBAL_CONCURRENCY": "4",
                "FASTLAB_MAX_CONCURRENCY_PER_USER": "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                service = FastLabService(root / "data", check_worker_image=False)
                try:
                    app = service.app_for(Principal("account-a", "Alice", "token"))
                    self.assertIsInstance(app.command_runner, ProcessCommandRunner)
                    self.assertEqual(app.agent_gate.limit, 1)
                    self.assertEqual(service.global_gate.limit, 4)
                    self.assertEqual(service.health_payload()["executionBackend"], "process")
                finally:
                    service.shutdown()


class CapacityContractTests(unittest.TestCase):
    def test_fifty_users_share_four_slots_and_one_slot_each(self):
        global_gate = AgentRunGate(4)
        tenant_gates = [AgentRunGate(1) for _ in range(50)]
        barrier = threading.Barrier(101)
        lock = threading.Lock()
        global_active = 0
        global_peak = 0
        tenant_active = [0] * 50
        tenant_peak = [0] * 50

        def run(user_index):
            nonlocal global_active, global_peak
            barrier.wait()
            tenant_gates[user_index].acquire(lambda: False)
            global_gate.acquire(lambda: False)
            try:
                with lock:
                    global_active += 1
                    tenant_active[user_index] += 1
                    global_peak = max(global_peak, global_active)
                    tenant_peak[user_index] = max(
                        tenant_peak[user_index], tenant_active[user_index]
                    )
                time.sleep(0.003)
            finally:
                with lock:
                    tenant_active[user_index] -= 1
                    global_active -= 1
                global_gate.release()
                tenant_gates[user_index].release()

        threads = [
            threading.Thread(target=run, args=(user_index,))
            for user_index in range(50)
            for _attempt in range(2)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertLessEqual(global_peak, 4)
        self.assertEqual(tenant_peak, [1] * 50)


if __name__ == "__main__":
    unittest.main()
