"""Direct, tenant-scoped Codex and Claude CLI execution backend."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .common import executor_provider, require_within, validate_command


_PASSTHROUGH_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def _terminate_process_tree(process) -> None:
    """Terminate the independent process group represented by *process*."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
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
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
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
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class ProcessCommandRunner:
    """Launch Agent CLIs directly with a minimal environment and Linux jail.

    The outer bubblewrap filesystem boundary protects other tenant directories;
    Codex and Claude still run their own, stricter command sandboxes inside it.
    This backend is intended for trusted internal users and is not a replacement
    for a container or VM trust boundary.
    """

    strict_cli = True

    def __init__(
        self,
        tenant_root: Path,
        app_root: Path,
        credential_provider,
        *,
        codex_bin="codex",
        claude_bin="claude",
        base_environment=None,
        use_outer_sandbox=None,
        platform=None,
    ):
        self.tenant_root = Path(tenant_root).resolve()
        self.app_root = Path(app_root).resolve()
        self.credential_provider = credential_provider
        self.codex_bin = str(codex_bin or "codex")
        self.claude_bin = str(claude_bin or "claude")
        source_environment = os.environ if base_environment is None else base_environment
        self.base_environment = {
            key: str(source_environment[key])
            for key in _PASSTHROUGH_ENVIRONMENT
            if source_environment.get(key)
        }
        self.platform = str(platform or sys.platform)
        self.use_outer_sandbox = (
            self.platform.startswith("linux")
            if use_outer_sandbox is None else bool(use_outer_sandbox)
        )
        self.runtime_root = self.tenant_root / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime_root, 0o700)
        self.active = {}
        self.lock = threading.Lock()

    def _executor_directories(self, executor: str) -> dict[str, Path]:
        root = self.runtime_root / str(executor)
        directories = {
            "root": root,
            "home": root / "home",
            "tmp": root / "tmp",
            "config": root / "config",
            "cache": root / "cache",
            "data": root / "data",
        }
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        return directories

    def runtime_directory(self, executor: str) -> Path:
        """Return the private runtime directory used by one executor."""
        return self._executor_directories(str(executor))["root"]

    def _environment(self, executor: str, secret: str, credential_name: str) -> dict[str, str]:
        directories = self._executor_directories(executor)
        environment = {
            key: str(self.base_environment[key])
            for key in _PASSTHROUGH_ENVIRONMENT
            if self.base_environment.get(key)
        }
        environment.update({
            "HOME": str(directories["home"]),
            "TMPDIR": str(directories["tmp"]),
            "TMP": str(directories["tmp"]),
            "TEMP": str(directories["tmp"]),
            "XDG_CONFIG_HOME": str(directories["config"]),
            "XDG_CACHE_HOME": str(directories["cache"]),
            "XDG_DATA_HOME": str(directories["data"]),
        })
        if secret:
            environment[credential_name] = str(secret)
        if executor == "codex":
            environment["CODEX_HOME"] = str(directories["home"] / ".codex")
        else:
            environment.update({
                "CLAUDE_CONFIG_DIR": str(directories["home"] / ".claude"),
                "CLAUDE_CODE_TMPDIR": str(directories["tmp"]),
                "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            })
        return environment

    @staticmethod
    def _directory_creation_arguments(path: Path) -> list[str]:
        arguments = []
        parents = list(Path(path).resolve().parents)
        for parent in reversed(parents[:-1]):
            arguments.extend(["--dir", str(parent)])
        return arguments

    @staticmethod
    def _cli_install_root(binary: Path) -> Path | None:
        """Return a narrow, supported non-system CLI installation root."""
        # Resolve npm-style launcher symlinks before deciding which tree must
        # be visible inside bubblewrap.  Mounting only /usr/local/bin while the
        # launcher points into /opt would otherwise pass startup validation but
        # fail as soon as the jailed process follows the symlink.
        resolved = Path(binary).resolve()
        for system_root in (Path("/usr"), Path("/bin"), Path("/sbin")):
            try:
                resolved.relative_to(system_root)
                return None
            except ValueError:
                continue
        try:
            relative = resolved.relative_to("/opt")
        except ValueError:
            return None
        if len(relative.parts) < 2:
            return None
        return Path("/opt") / relative.parts[0]

    def _cli_install_mounts(self) -> list[tuple[str, str]]:
        if not self.platform.startswith("linux"):
            return []
        roots = set()
        for configured in (self.codex_bin, self.claude_bin):
            binary = self._resolve_binary(configured)
            if not binary:
                continue
            root = self._cli_install_root(Path(binary))
            if root and root.exists():
                roots.add(root.resolve())
        return [(str(root), str(root)) for root in sorted(roots)]

    def _existing_system_mounts(self) -> list[tuple[str, str]]:
        directories = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
        files = (
            "/etc/ca-certificates",
            "/etc/ssl",
            "/etc/alternatives",
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
            "/etc/passwd",
            "/etc/group",
            "/etc/ld.so.cache",
            "/etc/localtime",
        )
        mounts = [(item, item) for item in directories + files if Path(item).exists()]
        mounts.extend(self._cli_install_mounts())
        return mounts

    def _git_common_directory(self, cwd: Path) -> Path | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=5, check=True,
                env={"PATH": self.base_environment.get("PATH", os.defpath)},
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        common = Path(result.stdout.strip()).resolve()
        try:
            common.relative_to(cwd)
            return None
        except ValueError:
            return require_within(
                common,
                self.tenant_root / "repositories",
                "Git 元数据越出租户仓库空间。",
            )

    def _bubblewrap(
        self,
        command: list[str],
        cwd: Path,
        writable: bool,
        executor: str,
    ) -> list[str]:
        runtime = self.runtime_directory(executor).resolve()
        mounts: list[tuple[str, Path, Path]] = []
        for source, destination in self._existing_system_mounts():
            mounts.append(("--ro-bind", Path(source), Path(destination)))
        mounts.extend([
            ("--bind" if writable else "--ro-bind", cwd, cwd),
            ("--bind", runtime, runtime),
        ])
        for asset in (self.app_root / "schemas", self.app_root / "skills"):
            if asset.exists():
                mounts.append(("--ro-bind", asset.resolve(), asset.resolve()))
        common = self._git_common_directory(cwd)
        if common is not None:
            mounts.append(("--bind" if writable else "--ro-bind", common, common))

        wrapped = [
            "bwrap", "--die-with-parent", "--new-session",
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        created = set()
        for mode, source, destination in mounts:
            for item in self._directory_creation_arguments(destination):
                # Each pair is ("--dir", path). Avoid duplicate bwrap mounts.
                if item == "--dir":
                    continue
                if item not in created:
                    wrapped.extend(["--dir", item])
                    created.add(item)
            wrapped.extend([mode, str(source), str(destination)])
        wrapped.extend(["--chdir", str(cwd), "--"])
        wrapped.extend(command)
        return wrapped

    def wrap(self, command, cwd, writable, executor, key="run"):
        cwd = require_within(cwd, self.tenant_root, "Agent 工作目录越出租户空间。")
        allowed_roots = (
            self.tenant_root / "repositories",
            self.tenant_root / "worktrees",
        )
        if not any(_is_within(cwd, root) for root in allowed_roots):
            raise ValueError("Agent 工作目录必须是当前租户的仓库或 Worktree。")
        command = validate_command(command)
        provider, credential_name = executor_provider(executor)
        secret = self.credential_provider(provider)
        environment = self._environment(str(executor), secret, credential_name)
        if self.use_outer_sandbox:
            command = self._bubblewrap(command, cwd, bool(writable), str(executor))
        return command, str(cwd), environment

    def started(self, key, process):
        with self.lock:
            self.active[str(key)] = process

    def interrupt(self, key):
        with self.lock:
            process = self.active.get(str(key))
        if process is None:
            return False
        _terminate_process_tree(process)
        return True

    def finished(self, key):
        with self.lock:
            self.active.pop(str(key), None)

    def _resolve_binary(self, value: str) -> str | None:
        candidate = Path(value).expanduser()
        if candidate.is_absolute() or candidate.parent != Path("."):
            return str(candidate.absolute()) if candidate.is_file() else None
        return shutil.which(value, path=self.base_environment.get("PATH", os.defpath))

    def _run_probe(self, command, message):
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            env={
                "PATH": self.base_environment.get("PATH", os.defpath),
                "HOME": str(self.runtime_root),
                "TMPDIR": str(self.runtime_root),
            },
        )
        if result.returncode:
            detail = " ".join(str(result.stderr or "").split())[:500]
            raise ValueError("%s%s" % (message, ("：" + detail) if detail else ""))

    def check(self):
        """Fail closed unless both CLI sandboxes are usable on Linux."""
        if not self.platform.startswith("linux"):
            raise ValueError("Process 后端的多用户模式仅支持 Linux；其他系统请使用 Docker。")
        if not self.use_outer_sandbox:
            raise ValueError("Process 后端必须启用外层 bubblewrap 文件系统隔离。")
        missing = [name for name in ("bwrap", "socat") if not self._resolve_binary(name)]
        if missing:
            raise ValueError("Process 后端缺少沙箱依赖：%s。" % ", ".join(missing))
        codex = self._resolve_binary(self.codex_bin)
        claude = self._resolve_binary(self.claude_bin)
        if not codex or not claude:
            missing_cli = [
                name for name, value in (("codex", codex), ("claude", claude)) if not value
            ]
            raise ValueError("找不到 Agent CLI：%s。" % ", ".join(missing_cli))
        for label, binary in (("Codex", codex), ("Claude", claude)):
            path = Path(binary).resolve()
            supported = self._cli_install_root(path) is not None
            if not supported:
                for root in (Path("/usr"), Path("/bin"), Path("/sbin")):
                    try:
                        path.relative_to(root)
                        supported = True
                        break
                    except ValueError:
                        continue
            if not supported:
                raise ValueError(
                    "%s CLI 必须安装在 /usr 或独立的 /opt/<name> 目录中：%s" % (label, path)
                )
        probe_workspace = self.runtime_root / ".sandbox-check-workspace"
        probe_sentinel = self.tenant_root / ".sandbox-check-sentinel"
        probe_workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe_sentinel.write_text("must remain invisible", encoding="utf-8")
        try:
            boundary_probe = self._bubblewrap(
                ["/bin/sh", "-c", 'test ! -r "$1"', "fastlab-sandbox-check", str(probe_sentinel)],
                probe_workspace.resolve(),
                False,
                "codex",
            )
            self._run_probe(boundary_probe, "bubblewrap 文件系统隔离自检失败")
        finally:
            probe_sentinel.unlink(missing_ok=True)
            shutil.rmtree(probe_workspace, ignore_errors=True)
        self._run_probe(
            [codex, "sandbox", "linux", "--", "/bin/true"],
            "Codex Linux 沙箱自检失败",
        )
        self._run_probe([claude, "--version"], "Claude CLI 健康检查失败")

    def shutdown(self):
        with self.lock:
            processes = list(self.active.values())
        for process in processes:
            try:
                _terminate_process_tree(process)
            except (OSError, subprocess.SubprocessError):
                pass
        with self.lock:
            self.active.clear()

    close = shutdown


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True
