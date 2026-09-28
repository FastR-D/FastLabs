"""HTTP transport for the FastLab application services."""

import json
import mimetypes
import re
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401
from urllib.parse import unquote, urlparse

from .config import DEFAULT_GLOBAL_CONCURRENCY, WEB_ROOT, compact_error
from .tenancy import FastLabService

class FastLabHandler(BaseHTTPRequestHandler):
    app = None
    server_version = "FastLab/0.1"

    def request_path(self):
        path = unquote(urlparse(self.path).path)
        service = type(self).app
        if isinstance(service, FastLabService) and service.public_path:
            if path == service.public_path:
                return "/"
            if path.startswith(service.public_path + "/"):
                return path[len(service.public_path):]
        return path

    def prepare_tenant(self, path, unsafe=False):
        service = type(self).app
        if not isinstance(service, FastLabService):
            self.app = service
            self.service = None
            self.principal = None
            return True
        if unsafe and self.headers.get("Origin") != service.public_origin:
            self.json_response(403, {"error": "不允许跨站操作。"})
            return False
        principal = service.authenticate(self.headers.get("Cookie", ""))
        if principal is None:
            if path.startswith("/api/"):
                self.json_response(401, {"error": "FastResearch 登录已失效。", "loginUrl": service.panel_url})
            else:
                self.send_response(302)
                self.send_header("Location", service.panel_url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return False
        self.service, self.principal = service, principal
        self.app = service.app_for(principal)
        return True

    def do_GET(self):
        try:
            path = self.request_path()
            if path == "/api/health":
                service = type(self).app
                payload = service.health_payload() if isinstance(service, FastLabService) else service.health_payload()
                return self.json_response(200, payload)
            if not self.prepare_tenant(path):
                return
            if path == "/api/me":
                return self.json_response(200, self.service.me(self.principal) if self.service else {
                    "person": "local", "spaceReady": True, "credentials": {}
                })
            if path == "/api/repositories":
                return self.json_response(200, {
                    "repositories": self.app.list_repositories(),
                    "management": self.app.repository_management(),
                })
            if path == "/api/settings/executors":
                return self.json_response(200, self.app.executor_settings())
            if path == "/api/settings/skill":
                return self.json_response(200, self.app.skill_settings())
            match = re.fullmatch(r"/api/settings/credentials/(openai|anthropic)", path)
            if match and self.service:
                return self.json_response(200, self.service.registry.credential_status(
                    self.principal.account_id
                )[match.group(1)])
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
            path = self.request_path()
            if not self.prepare_tenant(path, unsafe=True):
                return
            body = self.read_json()
            if path == "/api/tasks":
                if "workspace" in body:
                    raise ValueError("不再接受 workspace 路径，请使用 repositoryId 选择托管仓库。")
                task = self.app.create_task(
                    body.get("title"),
                    body.get("goal"),
                    body.get("constraints"),
                    body.get("maxConcurrency", DEFAULT_GLOBAL_CONCURRENCY),
                    body.get("repositoryId"),
                    working_subdir=body.get("workingSubdir", ""),
                )
                return self.json_response(202, task)
            if path == "/api/repositories":
                if "path" in body:
                    raise ValueError("不接受仓库路径，请创建仓库或填写 Git URL。")
                result = self.app.provision_repository(
                    body.get("alias"), body.get("cloneUrl", ""),
                    body.get("isDefault", False),
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
        except (ValueError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
            self.json_response(400, {"error": compact_error(exc)})
        except KeyError as exc:
            self.json_response(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            traceback.print_exc()
            self.json_response(500, {"error": compact_error(exc)})

    def do_PUT(self):
        try:
            path = self.request_path()
            if not self.prepare_tenant(path, unsafe=True):
                return
            body = self.read_json()
            if path == "/api/settings/executors":
                return self.json_response(200, self.app.update_executor_settings(body))
            if path == "/api/settings/skill":
                return self.json_response(
                    200, self.app.update_skill_settings(body.get("content"))
                )
            match = re.fullmatch(r"/api/settings/auth/(openai|anthropic)", path)
            if match and self.service:
                result = self.service.registry.set_credential_mode(
                    self.principal.account_id, match.group(1), body.get("mode")
                )
                return self.json_response(200, result)
            match = re.fullmatch(r"/api/settings/credentials/(openai|anthropic)", path)
            if match and self.service:
                result = self.service.registry.set_credential(
                    self.principal.account_id, match.group(1), body.get("key")
                )
                return self.json_response(200, result)
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
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)", path)
            if match:
                return self.json_response(200, self.app.rename_task(
                    match.group(1), body.get("title")
                ))
            match = re.fullmatch(r"/api/repositories/([a-f0-9]+)", path)
            if match:
                if "path" in body:
                    raise ValueError("不能修改托管仓库路径。")
                result = self.app.update_repository(
                    match.group(1), body.get("alias"),
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
            path = self.request_path()
            if not self.prepare_tenant(path, unsafe=True):
                return
            credential = re.fullmatch(r"/api/settings/credentials/(openai|anthropic)", path)
            if credential and self.service:
                self.service.registry.delete_credential(
                    self.principal.account_id, credential.group(1)
                )
                return self.json_response(200, {"ok": True})
            if path == "/api/settings/skill":
                return self.json_response(200, self.app.reset_skill_settings())
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)/events", path)
            if match:
                return self.json_response(200, self.app.clear_task_logs(match.group(1)))
            match = re.fullmatch(r"/api/tasks/([a-f0-9]+)", path)
            if match:
                return self.json_response(200, self.app.delete_task(match.group(1)))
            match = re.fullmatch(r"/api/repositories/([a-f0-9]+)/managed", path)
            if match:
                return self.json_response(
                    200, self.app.delete_managed_repository(match.group(1))
                )
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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        service = getattr(self, "service", None)
        if candidate.name == "index.html" and service:
            marker = b"<head>"
            inject = (
                "<script>window.FASTLAB_PUBLIC_PATH=%s;</script>" %
                json.dumps(service.public_path)
            ).encode("utf-8")
            content = content.replace(marker, marker + inject, 1)
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
