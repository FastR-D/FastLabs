import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from multitenancy import (
    DockerCommandRunner, Principal, ResearchAuthenticator, TenantRegistry,
)
from server import AgentRunGate, FastLab, FastLabHandler, FastLabService, ThreadingHTTPServer
from fake_adapter import FakeAgentAdapter


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class MultiTenantTests(unittest.TestCase):
    def test_research_authenticator_uses_cookie_and_stable_account(self):
        auth = ResearchAuthenticator("http://research.test", ttl=30)
        payload = b'{"accountId":"account-a","person":"Alice"}'
        with patch("urllib.request.urlopen", return_value=_Response(payload)) as request:
            first = auth.authenticate("other=x; fr_session=secret-token")
            second = auth.authenticate("fr_session=secret-token")
        self.assertEqual(first.account_id, "account-a")
        self.assertEqual(second.person, "Alice")
        self.assertEqual(request.call_count, 1)
        headers = request.call_args.args[0].headers
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["Cookie"], "fr_session=secret-token")

    def test_registry_encrypts_credentials_and_separates_tenant_roots(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-tenants-") as folder:
            root = Path(folder)
            key = Fernet.generate_key().decode()
            registry = TenantRegistry(root / "fastlab-v2.db", root / "tenants", key)
            alice = registry.ensure_tenant(Principal("account-a", "Alice", "a"))
            bob = registry.ensure_tenant(Principal("account-b", "Bob", "b"))
            self.assertNotEqual(alice["root"], bob["root"])
            for name in ("repositories", "tasks", "worktrees", "runtime", "skills"):
                self.assertTrue((alice["root"] / name).is_dir())
            saved = registry.set_credential("account-a", "openai", "sk-example-secret-value")
            self.assertTrue(saved["configured"])
            self.assertEqual(saved["mode"], "api_key")
            self.assertNotIn("example-secret", saved["preview"])
            self.assertEqual(registry.credential("account-a", "openai"), "sk-example-secret-value")
            self.assertEqual(registry.credential("account-b", "openai"), "")
            self.assertEqual(registry.credential_mode("account-b", "openai"), "cli")
            self.assertEqual(registry.effective_credential("account-b", "openai"), "")
            registry.set_credential_mode("account-a", "openai", "cli")
            self.assertEqual(registry.effective_credential("account-a", "openai"), "")
            registry.set_credential_mode("account-a", "openai", "api_key")
            self.assertEqual(
                registry.effective_credential("account-a", "openai"),
                "sk-example-secret-value",
            )
            self.assertEqual(registry.redact("account-a", "token=sk-example-secret-value"), "token=[REDACTED]")
            database = (root / "fastlab-v2.db").read_bytes()
            self.assertNotIn(b"sk-example-secret-value", database)
            wrong = TenantRegistry(root / "fastlab-v2.db", root / "tenants", Fernet.generate_key().decode())
            with self.assertRaisesRegex(ValueError, "无法解密"):
                wrong.credential("account-a", "openai")

    def test_repository_paths_cannot_escape_tenant_root(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-space-") as folder:
            root = Path(folder)
            tenant = root / "tenant"
            repositories = tenant / "repositories"
            repositories.mkdir(parents=True)
            app = FastLab(
                data_dir=tenant, repository_root=repositories,
                agent_adapter=FakeAgentAdapter(), planner=None,
            )
            try:
                with self.assertRaisesRegex(ValueError, "相对路径"):
                    app.add_repository("outside", str(root))
                with self.assertRaisesRegex(ValueError, "相对路径"):
                    app.add_repository("parent", "../other")
                outside = root / "outside"
                outside.mkdir()
                link = repositories / "link"
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "越出"):
                    app.add_repository("link", "link")
            finally:
                app.shutdown()

    def test_managed_repositories_create_isolated_paths_and_reject_unsafe_urls(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-managed-repos-") as folder:
            root = Path(folder)
            apps = []
            repositories = []
            try:
                for name in ("tenant-a", "tenant-b"):
                    tenant = root / name
                    repository_root = tenant / "repositories"
                    app = FastLab(
                        data_dir=tenant,
                        repository_root=repository_root,
                        agent_adapter=FakeAgentAdapter(),
                        planner=None,
                    )
                    apps.append(app)
                    repository = app.provision_repository("project")
                    repositories.append((app, repository_root, repository))
                    self.assertTrue(repository["available"])
                    self.assertTrue(repository["has_commit"])
                    self.assertTrue(repository["managed"])
                    self.assertTrue(repository["deletable"])
                    self.assertEqual(repository["source"], "created")
                    self.assertEqual(repository["path"], repository["id"])
                    stored = app.store.get_repository(repository["id"])
                    self.assertEqual(Path(stored["path"]).parent, repository_root.resolve())

                self.assertNotEqual(
                    apps[0].store.get_repository(repositories[0][2]["id"])["path"],
                    apps[1].store.get_repository(repositories[1][2]["id"])["path"],
                )
                active_app, active_root, active_repository = repositories[0]
                with self.assertRaisesRegex(ValueError, "只能永久删除"):
                    active_app.delete_repository(active_repository["id"])
                active_path = Path(
                    active_app.store.get_repository(active_repository["id"])["path"]
                )
                task_id = active_app.store.create_task(
                    "active", "active", "", str(active_path), 1,
                    repository_id=active_repository["id"],
                )
                with self.assertRaisesRegex(ValueError, "未结束任务"):
                    active_app.delete_managed_repository(active_repository["id"])
                self.assertTrue(active_path.is_dir())
                active_app.store.update_task(task_id, status="failed")

                deleted_app, _deleted_root, deleted_repository = repositories[1]
                deleted_path = Path(
                    deleted_app.store.get_repository(deleted_repository["id"])["path"]
                )
                deleted = deleted_app.delete_managed_repository(
                    deleted_repository["id"]
                )
                self.assertTrue(deleted["deletedFiles"])
                self.assertFalse(deleted_path.exists())
                self.assertIsNone(
                    deleted_app.store.get_repository(deleted_repository["id"])
                )

                restore_repository = active_app.provision_repository("restore-on-failure")
                restore_path = Path(
                    active_app.store.get_repository(restore_repository["id"])["path"]
                )
                with patch.object(
                    active_app.store, "delete_repository",
                    side_effect=RuntimeError("database failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "database failure"):
                        active_app.delete_managed_repository(restore_repository["id"])
                self.assertTrue(restore_path.is_dir())
                self.assertIsNotNone(
                    active_app.store.get_repository(restore_repository["id"])
                )

                outside = root / "outside-repository"
                outside.mkdir()
                outside_record = active_app.store.add_repository(
                    "outside-record", str(outside)
                )
                with self.assertRaisesRegex(ValueError, "托管目录"):
                    active_app.delete_managed_repository(outside_record["id"])
                self.assertTrue(outside.is_dir())
                for unsafe in (
                    "file:///tmp/repo", "ssh://git@example.com/repo",
                    "https://token@example.com/repo.git",
                    "https://example.com/repo.git?token=secret",
                ):
                    with self.subTest(url=unsafe), self.assertRaisesRegex(ValueError, "HTTPS"):
                        apps[0].provision_repository("unsafe-%s" % len(unsafe), unsafe)

                before = {item.name for item in repositories[0][1].iterdir()}
                with patch.object(apps[0], "_git", side_effect=RuntimeError("clone failed")):
                    with self.assertRaisesRegex(ValueError, "克隆失败"):
                        apps[0].provision_repository(
                            "clone-failure", "https://example.com/repo.git"
                        )
                self.assertEqual(
                    {item.name for item in repositories[0][1].iterdir()}, before
                )
                self.assertIsNone(apps[0].store.find_repository("clone-failure"))
            finally:
                for app in apps:
                    app.shutdown()

    def test_docker_command_mounts_only_current_workspace_and_safe_assets(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-docker-") as folder:
            tenant = Path(folder) / "tenant"
            workspace = tenant / "worktrees" / "task" / "sub"
            workspace.mkdir(parents=True)
            (tenant / "runtime").mkdir()
            runner = DockerCommandRunner(
                "fastlab-worker:test", tenant, ROOT, lambda _provider: "sk-user-only"
            )
            command, cwd, environment = runner.wrap(
                ["codex", "exec", "-C", str(workspace), "-o", str(tenant / "runtime" / "out")],
                workspace, writable=True, executor="codex",
            )
            joined = " ".join(command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn("--security-opt=no-new-privileges", command)
            self.assertIn("dst=/workspace,rw", joined)
            self.assertIn("dst=/fastlab/schemas,readonly", joined)
            self.assertIn("dst=/fastlab/skills,readonly", joined)
            self.assertNotIn("docker.sock", joined)
            self.assertNotIn(str(ROOT / ".fastlab"), joined)
            self.assertNotIn("src=%s,dst=" % Path.home(), joined)
            self.assertNotIn("sk-user-only", joined)
            self.assertEqual(environment["OPENAI_API_KEY"], "sk-user-only")
            self.assertEqual(Path(cwd), workspace.resolve())

    def test_global_and_tenant_gates_both_bound_runs(self):
        global_gate = AgentRunGate(2)
        tenant_gate = AgentRunGate(1)
        active = 0
        peak = 0
        lock = threading.Lock()

        def run():
            nonlocal active, peak
            tenant_gate.acquire(lambda: False)
            global_gate.acquire(lambda: False)
            try:
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.04)
            finally:
                with lock:
                    active -= 1
                global_gate.release()
                tenant_gate.release()

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak, 1)

    def test_service_creates_physically_separate_apps_for_accounts(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-service-") as folder:
            root = Path(folder)
            environment = {
                "FASTLAB_PUBLIC_URL": "https://research.example/lab/",
                "FASTLAB_PUBLIC_PATH": "/lab",
                "FASTLAB_CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
                "FASTLAB_TENANT_ROOT": str(root / "tenants"),
                "FASTLAB_WORKER_IMAGE": "fastlab-worker:test",
            }
            with patch.dict(os.environ, environment, clear=False):
                service = FastLabService(root / "data", check_worker_image=False)
                try:
                    alice = service.app_for(Principal("account-a", "Alice", "a"))
                    bob = service.app_for(Principal("account-b", "Bob", "b"))
                    self.assertIsNot(alice, bob)
                    self.assertNotEqual(alice.data_dir, bob.data_dir)
                    self.assertNotEqual(alice.store.path, bob.store.path)
                    self.assertIs(alice.global_agent_gate, bob.global_agent_gate)
                    self.assertEqual(service.me(Principal("account-a", "Alice", "a"))["person"], "Alice")
                finally:
                    service.shutdown()

    def test_http_auth_credentials_origin_and_cross_tenant_lookup(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-http-tenants-") as folder:
            root = Path(folder)
            environment = {
                "FASTLAB_PUBLIC_URL": "https://research.example/lab/",
                "FASTLAB_PUBLIC_PATH": "/lab",
                "FASTLAB_CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
                "FASTLAB_TENANT_ROOT": str(root / "tenants"),
                "FASTLAB_WORKER_IMAGE": "fastlab-worker:test",
            }
            with patch.dict(os.environ, environment, clear=False):
                service = FastLabService(root / "data", check_worker_image=False)
            identities = {
                "fr_session=alice": Principal("account-a", "Alice", "alice"),
                "fr_session=bob": Principal("account-b", "Bob", "bob"),
            }
            service.authenticator.authenticate = lambda value: identities.get(value)
            previous_app = FastLabHandler.app
            FastLabHandler.app = service
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), FastLabHandler)
            except PermissionError:
                service.shutdown()
                FastLabHandler.app = previous_app
                self.skipTest("sandbox forbids local ports")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            origin = "http://127.0.0.1:%s" % server.server_address[1]

            def request(path, cookie="", method="GET", body=None, request_origin=None):
                headers = {"Cookie": cookie}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                if request_origin:
                    headers["Origin"] = request_origin
                return urlopen(Request(
                    origin + path, data=body, headers=headers, method=method
                ))

            try:
                with request("/api/health") as response:
                    self.assertTrue(json.loads(response.read())["ok"])
                with self.assertRaises(HTTPError) as unauthorized:
                    request("/api/me")
                self.assertEqual(unauthorized.exception.code, 401)
                with request("/lab/api/me", "fr_session=alice") as response:
                    self.assertEqual(json.loads(response.read())["person"], "Alice")
                payload = json.dumps({"key": "sk-alice-private-value"}).encode()
                with self.assertRaises(HTTPError) as csrf:
                    request(
                        "/api/settings/credentials/openai", "fr_session=alice",
                        "PUT", payload, "https://attacker.example",
                    )
                self.assertEqual(csrf.exception.code, 403)
                with request(
                    "/api/settings/credentials/openai", "fr_session=alice",
                    "PUT", payload, "https://research.example",
                ) as response:
                    self.assertTrue(json.loads(response.read())["configured"])
                with request(
                    "/api/settings/credentials/openai", "fr_session=bob"
                ) as response:
                    self.assertFalse(json.loads(response.read())["configured"])
                alice = service.app_for(identities["fr_session=alice"])
                task_id = alice.store.create_task("Private", "Goal", "", "/tmp/unused", 1)
                with self.assertRaises(HTTPError) as hidden:
                    request("/api/tasks/%s" % task_id, "fr_session=bob")
                self.assertEqual(hidden.exception.code, 404)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                service.shutdown()
                FastLabHandler.app = previous_app


if __name__ == "__main__":
    unittest.main()
