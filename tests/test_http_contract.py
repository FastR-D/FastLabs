import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastlab.http_server import FastLabHandler


class _Backend:
    def __init__(self, repository_root):
        self.repository_root = Path(repository_root)
        self.calls = []
        self.failure = None

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.failure:
                raise self.failure
            if name in {"list_repositories", "list_payload"}:
                return []
            return {"called": name}
        return call

    def health_payload(self):
        return {"ok": True, "mode": "contract"}

    def repository_management(self):
        return {"mode": "managed", "acceptsPath": False, "acceptsCloneUrl": True}


class _Registry:
    def __init__(self):
        self.calls = []

    def credential_status(self, account_id):
        self.calls.append(("credential_status", account_id))
        return {
            "openai": {"configured": False, "preview": ""},
            "anthropic": {"configured": False, "preview": ""},
        }

    def set_credential(self, account_id, provider, key):
        self.calls.append(("set_credential", account_id, provider, key))
        return {"configured": True, "preview": "configured"}

    def delete_credential(self, account_id, provider):
        self.calls.append(("delete_credential", account_id, provider))

    def set_credential_mode(self, account_id, provider, mode):
        self.calls.append(("set_credential_mode", account_id, provider, mode))
        return {"configured": False, "preview": "", "mode": mode}


class _Handler(FastLabHandler):
    app = None

    def __init__(self, path, backend, service, body=None):
        self.path = path
        self.command = "TEST"
        self.backend = backend
        self.service_fixture = service
        self.body = body or {}
        self.response = None

    def prepare_tenant(self, _path, unsafe=False):
        self.app = self.backend
        self.service = self.service_fixture
        self.principal = SimpleNamespace(account_id="account-a")
        return True

    def read_json(self):
        return dict(self.body)

    def json_response(self, status, payload):
        # Round-trip to freeze JSON serializability and key/value types.
        self.response = (status, json.loads(json.dumps(payload, ensure_ascii=False)))


class HttpRouteContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fastlab-http-contract-")
        root = Path(self.temp.name) / "repositories"
        root.mkdir()
        self.backend = _Backend(root)
        self.registry = _Registry()
        self.service = SimpleNamespace(
            registry=self.registry,
            me=lambda principal: {"person": principal.account_id},
        )
        _Handler.app = self.backend

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, path, body=None):
        handler = _Handler(path, self.backend, self.service, body)
        getattr(handler, "do_" + method)()
        self.assertIsNotNone(handler.response, path)
        return handler.response

    def test_get_route_golden_contract(self):
        routes = {
            "/api/health": (200, {"ok": True, "mode": "contract"}),
            "/api/me": (200, {"person": "account-a"}),
            "/api/repositories": (200, {
                "repositories": [],
                "management": {
                    "mode": "managed", "acceptsPath": False,
                    "acceptsCloneUrl": True,
                },
            }),
            "/api/settings/credentials/openai": (
                200, {"configured": False, "preview": ""},
            ),
            "/api/settings/skill": (200, {"called": "skill_settings"}),
            "/api/tasks": (200, {"tasks": []}),
            "/api/tasks/abcdef": (200, {"called": "task_payload"}),
            "/api/not-found": (404, {"error": "接口不存在。"}),
        }
        for path, expected in routes.items():
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path), expected)
        status, settings = self.request("GET", "/api/settings/executors")
        self.assertEqual(status, 200)
        self.assertEqual(settings["called"], "executor_settings")

    def test_all_post_routes_keep_status_and_dispatch_contract(self):
        body = {
            "title": "Title", "goal": "Goal", "constraints": "",
            "maxConcurrency": 2, "repositoryId": "repo-1",
            "workingSubdir": "web", "feedback": "revise", "message": "more",
            "evidence": "checked", "executor": "codex", "model": "model",
            "reasoningEffort": "high", "alias": "repo",
            "cloneUrl": "https://example.com/repo.git",
            "isDefault": True, "initialize": False,
        }
        routes = {
            "/api/tasks": (202, "create_task"),
            "/api/repositories": (201, "provision_repository"),
            "/api/repositories/abcdef/initialize": (200, "initialize_repository"),
            "/api/tasks/abcdef/replan": (202, "replan_task"),
            "/api/tasks/abcdef/start": (202, "start_task"),
            "/api/tasks/abcdef/cancel": (202, "cancel_task"),
            "/api/tasks/abcdef/verify": (202, "verify_task"),
            "/api/tasks/abcdef/accept-verification": (202, "accept_verification"),
            "/api/tasks/abcdef/rerun": (202, "rerun_task"),
            "/api/tasks/abcdef/continue": (202, "continue_task"),
            "/api/tasks/abcdef/deliver": (202, "deliver_task"),
            "/api/tasks/abcdef/cleanup": (202, "cleanup_task_git"),
            "/api/subtasks/sub-1/retry": (202, "retry_subtask"),
            "/api/subtasks/sub-1/message": (202, "message_subtask"),
            "/api/subtasks/sub-1/revise": (202, "revise_subtask"),
        }
        for path, (status, called) in routes.items():
            with self.subTest(path=path):
                response_status, payload = self.request("POST", path, body)
                self.assertEqual(response_status, status)
                self.assertEqual(payload["called"], called)
        self.assertEqual(self.request("POST", "/api/unknown", body), (404, {"error": "接口不存在。"}))

    def test_put_delete_and_admin_policy_contract(self):
        body = {
            "key": "secret", "title": "T", "instructions": "I",
            "executor": "codex", "model": "m", "reasoningEffort": "high",
            "alias": "repo", "isDefault": True,
        }
        put_routes = {
            "/api/subtasks/sub-1": "update_subtask_plan",
            "/api/subtasks/sub-1/executor": "update_subtask_executor",
            "/api/tasks/abcdef": "rename_task",
            "/api/tasks/abcdef/verifier": "update_task_verifier",
            "/api/repositories/abcdef": "update_repository",
        }
        for path, called in put_routes.items():
            with self.subTest(path=path):
                status, payload = self.request("PUT", path, body)
                self.assertEqual((status, payload["called"]), (200, called))
        status, payload = self.request("PUT", "/api/settings/executors", body)
        self.assertEqual((status, payload["called"]), (200, "update_executor_settings"))
        status, payload = self.request("PUT", "/api/settings/credentials/openai", body)
        self.assertEqual((status, payload["configured"]), (200, True))
        status, payload = self.request(
            "PUT", "/api/settings/auth/openai", {"mode": "cli"}
        )
        self.assertEqual((status, payload["mode"]), (200, "cli"))

        delete_routes = {
            "/api/settings/skill": "reset_skill_settings",
            "/api/tasks/abcdef/events": "clear_task_logs",
            "/api/tasks/abcdef": "delete_task",
            "/api/repositories/abcdef/managed": "delete_managed_repository",
        }
        for path, called in delete_routes.items():
            with self.subTest(path=path):
                status, payload = self.request("DELETE", path)
                self.assertEqual((status, payload["called"]), (200, called))
        self.assertEqual(
            self.request("DELETE", "/api/settings/credentials/openai"),
            (200, {"ok": True}),
        )

    def test_managed_repository_api_rejects_user_paths(self):
        status, payload = self.request(
            "POST", "/api/repositories", {"alias": "x", "path": "x"}
        )
        self.assertEqual(status, 400)
        self.assertIn("不接受仓库路径", payload["error"])

        status, payload = self.request(
            "PUT", "/api/repositories/abcdef", {"alias": "x", "path": "x"}
        )
        self.assertEqual(status, 400)
        self.assertIn("不能修改托管仓库路径", payload["error"])

    def test_legacy_paths_and_record_only_delete_are_rejected(self):
        for value in ("/tmp/project", "", None):
            with self.subTest(workspace=value):
                self.backend.calls.clear()
                status, payload = self.request(
                    "POST", "/api/tasks", {"goal": "Goal", "workspace": value}
                )
                self.assertEqual(status, 400)
                self.assertIn("repositoryId", payload["error"])
                self.assertEqual(self.backend.calls, [])
        self.backend.calls.clear()
        status, _ = self.request("DELETE", "/api/repositories/abcdef")
        self.assertEqual(status, 404)
        self.assertEqual(self.backend.calls, [])



if __name__ == "__main__":
    unittest.main()
