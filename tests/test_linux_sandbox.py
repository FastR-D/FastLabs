"""Release-gate checks for the Linux direct-process sandbox.

Set ``FASTLAB_REQUIRE_LINUX_SANDBOX_TESTS=1`` in deployment CI to turn every
missing prerequisite into a hard failure.  Developer machines may otherwise
skip these host-specific checks.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastlab.runners import ProcessCommandRunner


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = os.environ.get("FASTLAB_REQUIRE_LINUX_SANDBOX_TESTS") == "1"


class LinuxSandboxReleaseGateTests(unittest.TestCase):
    def require_linux_tools(self, *tools):
        reason = None
        if not sys_platform_linux():
            reason = "Linux sandbox tests require Linux"
        else:
            missing = [tool for tool in tools if not shutil.which(tool)]
            if missing:
                reason = "missing Linux sandbox tools: %s" % ", ".join(missing)
        if reason and REQUIRED:
            self.fail(reason)
        if reason:
            self.skipTest(reason)

    def test_outer_bubblewrap_cannot_read_another_tenant(self):
        self.require_linux_tools("bwrap", "socat")
        with tempfile.TemporaryDirectory(prefix="fastlab-linux-isolation-") as folder:
            root = Path(folder)
            tenant = root / "tenant-a"
            workspace = tenant / "worktrees" / "task-a" / "sub-01"
            workspace.mkdir(parents=True)
            other = root / "tenant-b"
            other.mkdir(mode=0o700)
            sentinel = other / "private-sentinel"
            service_config = root / "fastlab.env"
            master_secret = root / "credential-master-key"
            service_home = root / "service-home" / "private-config"
            service_home.parent.mkdir(mode=0o700)
            for path, content in (
                (sentinel, "another tenant secret"),
                (service_config, "FASTLAB_CREDENTIAL_MASTER_KEY=must-not-leak"),
                (master_secret, "master-key-material"),
                (service_home, "service-home-secret"),
            ):
                path.write_text(content, encoding="utf-8")
            result_path = workspace / "sandbox-ok"
            runner = ProcessCommandRunner(
                tenant,
                ROOT,
                lambda _provider: "test-only-key",
                base_environment={"PATH": os.environ.get("PATH", os.defpath)},
                platform="linux",
                use_outer_sandbox=True,
            )
            shell = shutil.which("sh") or "/bin/sh"
            command, cwd, environment = runner.wrap(
                [
                    shell,
                    "-c",
                    "test ! -r \"$1\" && test ! -r \"$2\" && "
                    "test ! -r \"$3\" && test ! -r \"$4\" && "
                    "printf isolated > \"$5\"",
                    "fastlab-sandbox-probe",
                    str(sentinel),
                    str(service_config),
                    str(master_secret),
                    str(service_home),
                    str(result_path),
                ],
                workspace,
                True,
                "codex",
                "sentinel-probe",
            )
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(result_path.read_text(encoding="utf-8"), "isolated")

    def test_installed_clis_and_native_sandboxes_pass_startup_check(self):
        self.require_linux_tools("bwrap", "socat", "codex", "claude")
        with tempfile.TemporaryDirectory(prefix="fastlab-linux-check-") as folder:
            tenant = Path(folder) / "tenant"
            (tenant / "runtime").mkdir(parents=True)
            runner = ProcessCommandRunner(
                tenant,
                ROOT,
                lambda _provider: "test-only-key",
                codex_bin=os.environ.get("FASTLAB_CODEX_BIN", "codex"),
                claude_bin=os.environ.get("FASTLAB_CLAUDE_BIN", "claude"),
                base_environment={"PATH": os.environ.get("PATH", os.defpath)},
                platform="linux",
            )
            runner.check()


def sys_platform_linux():
    return os.name == "posix" and os.uname().sysname.lower() == "linux"


if __name__ == "__main__":
    unittest.main()
