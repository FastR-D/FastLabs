"""Optional one-shot Docker execution backend.

This is the former ``multitenancy.DockerCommandRunner`` implementation.  The
root module re-exports it so existing imports keep working.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path

from .common import executor_provider, require_within, validate_command


class DockerCommandRunner:
    """Turn an Agent CLI command into a hardened one-shot Docker command."""

    def __init__(self, image: str, tenant_root: Path, app_root: Path, credential_provider,
                 cpus="2", memory="4g", pids=256):
        self.image = str(image or "").strip()
        if not self.image:
            raise ValueError("FASTLAB_WORKER_IMAGE 不能为空。")
        self.tenant_root = Path(tenant_root).resolve()
        self.app_root = Path(app_root).resolve()
        self.credential_provider = credential_provider
        self.cpus, self.memory, self.pids = str(cpus), str(memory), int(pids)
        self.active = {}
        self.lock = threading.Lock()

    def _map(self, value: str, cwd: Path) -> str:
        value = str(value)
        if not Path(value).is_absolute():
            return value
        mappings = (
            (cwd, Path("/workspace")),
            (self.app_root, Path("/fastlab")),
            (self.tenant_root / "runtime", Path("/runtime")),
        )
        for source, target in mappings:
            try:
                relative = Path(value).resolve().relative_to(source.resolve())
                return str(target / relative)
            except (ValueError, OSError):
                continue
        return value

    def runtime_directory(self, executor: str) -> Path:
        directory = self.tenant_root / "runtime" / str(executor)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        return directory

    def wrap(self, command, cwd, writable, executor, key="run"):
        cwd = require_within(cwd, self.tenant_root, "Agent 工作目录越出租户空间。")
        command = validate_command(command)
        provider, credential_name = executor_provider(executor)
        secret = self.credential_provider(provider)
        mapped = [self._map(item, cwd) for item in command]
        executor_home = self.tenant_root / "runtime" / str(executor)
        executor_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home = "/runtime/%s" % executor
        uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 65532
        gid = os.getgid() if hasattr(os, "getgid") and os.getgid() else 65532
        name = "fastlab-%s-%s" % (
            self.tenant_root.name[:10], hashlib.sha256(str(key).encode()).hexdigest()[:16]
        )
        with self.lock:
            self.active[str(key)] = name
        docker = [
            "docker", "run", "--rm", "--name", name, "--init", "--read-only",
            "--log-driver=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit", str(self.pids), "--cpus", self.cpus, "--memory", self.memory,
            "--user", "%s:%s" % (uid, gid), "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            "--workdir", "/workspace",
            "--mount", "type=bind,src=%s,dst=/workspace,%s" % (
                cwd, "rw" if writable else "readonly"
            ),
            "--mount", "type=bind,src=%s,dst=/runtime,rw" % (self.tenant_root / "runtime"),
            "--mount", "type=bind,src=%s,dst=/fastlab/schemas,readonly" % (
                self.app_root / "schemas"
            ),
            "--mount", "type=bind,src=%s,dst=/fastlab/skills,readonly" % (
                self.app_root / "skills"
            ),
            "--env", "HOME=" + home,
            "--env", "CODEX_HOME=" + home + "/.codex",
        ]
        if secret:
            docker.extend(["--env", credential_name])
        try:
            common = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=5, check=True,
            ).stdout.strip()
            common_path = Path(common).resolve()
            try:
                common_path.relative_to(cwd)
            except ValueError:
                require_within(
                    common_path,
                    self.tenant_root / "repositories",
                    "Git 元数据越出租户仓库空间。",
                )
                docker.extend([
                    "--mount", "type=bind,src=%s,dst=%s,rw" % (common_path, common_path)
                ])
        except subprocess.CalledProcessError:
            pass
        docker += [self.image] + mapped
        environment = {"PATH": os.environ.get("PATH", "")}
        if secret:
            environment[credential_name] = secret
        return docker, str(cwd), environment

    def started(self, key, process):
        """Docker tracks a container name during ``wrap``; Popen needs no registration."""
        return None

    def interrupt(self, key):
        with self.lock:
            name = self.active.get(str(key))
        if not name:
            return False
        subprocess.run(
            ["docker", "kill", name], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
        return True

    def finished(self, key):
        with self.lock:
            name = self.active.pop(str(key), None)
        if name:
            subprocess.run(
                ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10, check=False,
            )

    def check(self):
        result = subprocess.run(
            ["docker", "image", "inspect", self.image], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=10, check=False,
        )
        if result.returncode:
            raise ValueError("找不到 FastLab Worker 镜像：%s" % self.image)

    def shutdown(self):
        with self.lock:
            keys = list(self.active)
        for key in keys:
            try:
                self.finished(key)
            except (OSError, subprocess.SubprocessError):
                pass

    close = shutdown
