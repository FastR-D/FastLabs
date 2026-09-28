"""Authenticated tenant application lifecycle."""

import os
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

from .application import FastLab
from .config import APP_ROOT, CapacityConfig
from .integrations.fastresearch import ResearchAuthenticator
from .runners import DockerCommandRunner, ProcessCommandRunner
from .scheduling import AgentRunGate
from .tenant_registry import TenantRegistry, safe_public_path

class FastLabService:
    """Resolve one authenticated FastLab engine per FastResearch account."""

    def __init__(self, data_dir, *, app_factory=FastLab, check_worker_image=True):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.public_path = safe_public_path(os.environ.get("FASTLAB_PUBLIC_PATH", "/lab"))
        self.public_url = str(os.environ.get("FASTLAB_PUBLIC_URL") or "").strip().rstrip("/") + "/"
        if not self.public_url.startswith(("http://", "https://")):
            raise ValueError("FASTLAB_PUBLIC_URL 必须是完整的 http(s) 地址。")
        configured_url_path = urlparse(self.public_url).path.rstrip("/")
        if configured_url_path != self.public_path:
            raise ValueError("FASTLAB_PUBLIC_URL 的路径必须与 FASTLAB_PUBLIC_PATH 一致。")
        self.public_origin = "%s://%s" % (
            urlparse(self.public_url).scheme, urlparse(self.public_url).netloc
        )
        self.panel_url = str(os.environ.get("FASTRESEARCH_PANEL_URL") or self.public_origin + "/")
        self.authenticator = ResearchAuthenticator(
            os.environ.get("FASTRESEARCH_API_URL", "http://127.0.0.1:8787"),
            os.environ.get("FASTRESEARCH_COOKIE_NAME", "fr_session"),
        )
        tenant_root = Path(os.environ.get("FASTLAB_TENANT_ROOT") or self.data_dir / "tenants")
        self.registry = TenantRegistry(
            self.data_dir / "fastlab-v2.db", tenant_root,
            os.environ.get("FASTLAB_CREDENTIAL_MASTER_KEY", ""),
        )
        self.worker_image = str(os.environ.get("FASTLAB_WORKER_IMAGE") or "").strip()
        self.execution_backend = str(
            os.environ.get("FASTLAB_EXECUTION_BACKEND") or "process"
        ).strip().lower()
        if self.execution_backend not in {"process", "docker"}:
            raise ValueError("FASTLAB_EXECUTION_BACKEND 只能是 process 或 docker。")
        self.codex_bin = str(os.environ.get("FASTLAB_CODEX_BIN") or "codex")
        self.claude_bin = str(os.environ.get("FASTLAB_CLAUDE_BIN") or "claude")
        capacity = CapacityConfig.from_environment()
        self.global_gate = AgentRunGate(capacity.global_concurrency)
        self.tenant_limit = capacity.per_user_concurrency
        self.app_factory = app_factory
        self.apps = {}
        self.lock = threading.Lock()
        if check_worker_image:
            self.authenticator.check()
            if self.execution_backend == "docker" and not shutil.which("docker"):
                raise ValueError("找不到 Docker；docker 执行后端不可用。")
            runner = self._runner_for(
                self.data_dir / "runner-check", lambda _provider: "probe"
            )
            try:
                runner.check()
            finally:
                runner.close()

    def _runner_for(self, root, credential):
        if self.execution_backend == "process":
            return ProcessCommandRunner(
                root,
                APP_ROOT,
                credential,
                codex_bin=self.codex_bin,
                claude_bin=self.claude_bin,
            )
        return DockerCommandRunner(
            self.worker_image,
            root,
            APP_ROOT,
            credential,
            cpus=os.environ.get("FASTLAB_CONTAINER_CPUS", "2"),
            memory=os.environ.get("FASTLAB_CONTAINER_MEMORY", "4g"),
            pids=os.environ.get("FASTLAB_CONTAINER_PIDS", "256"),
        )

    def authenticate(self, cookie_header):
        return self.authenticator.authenticate(cookie_header)

    def app_for(self, principal):
        tenant = self.registry.ensure_tenant(principal)
        key = tenant["tenantKey"]
        with self.lock:
            current = self.apps.get(key)
            if current:
                return current
            root = tenant["root"]
            credential = lambda provider: self.registry.effective_credential(
                principal.account_id, provider
            )
            runner = self._runner_for(root, credential)
            app = self.app_factory(
                data_dir=root,
                repository_root=root / "repositories",
                command_runner=runner,
                shared_agent_gate=self.global_gate,
                tenant_limit=self.tenant_limit,
                credential_available=lambda provider: (
                    self.registry.credential_mode(principal.account_id, provider) == "cli"
                    or bool(self.registry.credential(principal.account_id, provider))
                ),
                redactor=lambda value: self.registry.redact(principal.account_id, value),
            )
            self.apps[key] = app
            return app

    def me(self, principal):
        tenant = self.registry.ensure_tenant(principal)
        app = self.app_for(principal)
        return {
            "person": principal.person,
            "spaceReady": True,
            "repositoryCount": len(app.store.list_repositories()),
            "credentials": self.registry.credential_status(principal.account_id),
        }

    def health_payload(self):
        return {
            "ok": True,
            "mode": "research",
            "executionBackend": self.execution_backend,
            "sandbox": (
                "bubblewrap+cli" if self.execution_backend == "process" else "docker+cli"
            ),
            "docker": bool(shutil.which("docker")),
            "workerImage": self.worker_image,
            "activeTenants": len(self.apps),
            "agentRuns": self.global_gate.snapshot(),
        }

    def shutdown(self):
        with self.lock:
            apps = list(self.apps.values())
        for app in apps:
            app.shutdown()
