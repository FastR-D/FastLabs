import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastlab.agents import ClaudeCLIAdapter, CodexCLIAdapter
from fastlab.contracts import ExecutionRunner
from fastlab.runners import ProcessCommandRunner


ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = ROOT / "tests" / "fake_codex.py"
FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"


class _CapturingProcessRunner(ProcessCommandRunner):
    def wrap(self, command, cwd, writable, executor, key="run"):
        self.last_input_command = list(command)
        wrapped = super().wrap(command, cwd, writable, executor, key)
        self.last_environment = dict(wrapped[2])
        return wrapped


class ProcessCommandRunnerTests(unittest.TestCase):
    def make_space(self, folder):
        tenant = Path(folder) / "tenant-a"
        workspace = tenant / "worktrees" / "task-a" / "sub-01"
        workspace.mkdir(parents=True)
        return tenant, workspace

    def make_runner(self, tenant, credentials=None, **options):
        if credentials is None:
            credentials = {
                "openai": "sk-openai-tenant-a",
                "anthropic": "sk-ant-tenant-a",
            }
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "SSL_CERT_FILE": "/safe/certificate.pem",
            "OPENAI_API_KEY": "host-openai-must-not-leak",
            "ANTHROPIC_API_KEY": "host-anthropic-must-not-leak",
            "FASTLAB_CREDENTIAL_MASTER_KEY": "master-must-not-leak",
            "FASTRESEARCH_API_URL": "http://secret-control-plane",
            "AWS_SECRET_ACCESS_KEY": "cloud-must-not-leak",
            "PYTHONPATH": "/host/private/modules",
        }
        return _CapturingProcessRunner(
            tenant,
            ROOT,
            lambda provider: credentials.get(provider, ""),
            base_environment=environment,
            use_outer_sandbox=options.pop("use_outer_sandbox", False),
            **options,
        )

    def test_process_runner_contract(self):
        self.assertTrue(isinstance(ProcessCommandRunner, type))
        with tempfile.TemporaryDirectory(prefix="fastlab-process-contract-") as folder:
            tenant, _ = self.make_space(folder)
            runner = self.make_runner(tenant)
            self.assertNotIn("FASTLAB_CREDENTIAL_MASTER_KEY", runner.base_environment)
            self.assertIsInstance(runner, ExecutionRunner)

    def test_environment_is_allowlisted_and_provider_specific(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-env-") as folder:
            tenant, workspace = self.make_space(folder)
            runner = self.make_runner(tenant)
            _, cwd, codex_env = runner.wrap(
                ["codex", "exec"], workspace, True, "codex", "codex-run"
            )
            _, _, claude_env = runner.wrap(
                ["claude", "-p"], workspace, True, "claude", "claude-run"
            )

            self.assertEqual(Path(cwd), workspace.resolve())
            self.assertEqual(codex_env["OPENAI_API_KEY"], "sk-openai-tenant-a")
            self.assertNotIn("ANTHROPIC_API_KEY", codex_env)
            self.assertEqual(claude_env["ANTHROPIC_API_KEY"], "sk-ant-tenant-a")
            self.assertNotIn("OPENAI_API_KEY", claude_env)
            for forbidden in (
                "FASTLAB_CREDENTIAL_MASTER_KEY", "FASTRESEARCH_API_URL",
                "AWS_SECRET_ACCESS_KEY", "PYTHONPATH",
            ):
                self.assertNotIn(forbidden, codex_env)
                self.assertNotIn(forbidden, claude_env)

            codex_home = Path(codex_env["HOME"])
            claude_home = Path(claude_env["HOME"])
            self.assertNotEqual(codex_home, claude_home)
            self.assertEqual(Path(codex_env["CODEX_HOME"]).parent, codex_home)
            self.assertEqual(Path(claude_env["CLAUDE_CONFIG_DIR"]).parent, claude_home)
            self.assertEqual(claude_env["DISABLE_AUTOUPDATER"], "1")
            self.assertEqual(claude_env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")
            for directory in (
                codex_home, Path(codex_env["TMPDIR"]),
                claude_home, Path(claude_env["TMPDIR"]),
            ):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_rejects_escape_unknown_executor_missing_key_and_bypass_flags(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-policy-") as folder:
            tenant, workspace = self.make_space(folder)
            runner = self.make_runner(tenant)
            with self.assertRaisesRegex(ValueError, "越出租户"):
                runner.wrap(["codex", "exec"], Path(folder), True, "codex")
            tenant_runtime = tenant / "runtime" / "not-a-worktree"
            tenant_runtime.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "仓库或 Worktree"):
                runner.wrap(["codex", "exec"], tenant_runtime, True, "codex")
            with self.assertRaisesRegex(ValueError, "不支持"):
                runner.wrap(["other"], workspace, True, "other")
            with self.assertRaisesRegex(ValueError, "权限绕过"):
                runner.wrap(
                    ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
                    workspace, True, "codex",
                )
            without_key = self.make_runner(tenant, credentials={})
            _, _, cli_environment = without_key.wrap(
                ["codex", "exec"], workspace, True, "codex"
            )
            self.assertNotIn("OPENAI_API_KEY", cli_environment)
            self.assertIn("CODEX_HOME", cli_environment)

    def test_linux_wrapper_exposes_only_system_runtime_workspace_and_safe_assets(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-bwrap-") as folder:
            tenant, workspace = self.make_space(folder)
            runner = self.make_runner(tenant)
            runner.use_outer_sandbox = True
            command, _, _ = runner.wrap(
                ["codex", "exec", "-C", str(workspace)],
                workspace, True, "codex",
            )
            self.assertEqual(command[0], "bwrap")
            self.assertIn("--die-with-parent", command)
            self.assertIn("--unshare-user", command)
            self.assertIn("--bind", command)
            self.assertIn(str(workspace.resolve()), command)
            self.assertIn(str((tenant / "runtime").resolve()), command)
            joined = " ".join(command)
            self.assertNotIn("--ro-bind /etc /etc", joined)
            self.assertNotIn("--bind %s %s" % (tenant, tenant), joined)
            self.assertNotIn(
                "--ro-bind %s %s" % (Path.home(), Path.home()),
                joined,
            )

    @unittest.skipIf(os.name == "nt", "POSIX process groups are tested on Unix")
    def test_interrupt_terminates_the_complete_process_group(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-tree-") as folder:
            tenant, _ = self.make_space(folder)
            runner = self.make_runner(tenant)
            marker = Path(folder) / "child-survived.txt"
            child_code = (
                "import pathlib,time;time.sleep(1.0);"
                "pathlib.Path(%r).write_text('survived')" % str(marker)
            )
            parent_code = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-c',%r]);"
                "print('ready',flush=True);time.sleep(30)" % child_code
            )
            process = subprocess.Popen(
                [sys.executable, "-c", parent_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                runner.started("tree", process)
                self.assertTrue(runner.interrupt("tree"))
                self.assertIsNotNone(process.poll())
                time.sleep(1.2)
                self.assertFalse(marker.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                if process.stdout:
                    process.stdout.close()
                runner.finished("tree")

    def test_codex_adapter_applies_noninteractive_sandbox_and_disables_extensions(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-codex-") as folder:
            tenant, workspace = self.make_space(folder)
            runner = self.make_runner(tenant)
            adapter = CodexCLIAdapter(
                str(FAKE_CODEX), tenant / "runtime" / "adapter-codex",
                command_runner=runner,
            )
            code, _, _ = adapter.run(
                "codex-safe", workspace, "子任务 ID：S1", "workspace_write"
            )
            self.assertEqual(code, 0)
            command = runner.last_input_command
            self.assertIn("--sandbox", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
            self.assertIn("--ask-for-approval", command)
            self.assertEqual(command[command.index("--ask-for-approval") + 1], "never")
            overrides = [command[index + 1] for index, item in enumerate(command) if item == "-c"]
            self.assertIn("features.hooks=false", overrides)
            self.assertIn("features.apps=false", overrides)
            self.assertIn("features.remote_plugin=false", overrides)
            self.assertIn("mcp_servers={}", overrides)
            self.assertTrue(any(
                item.startswith('projects."') and item.endswith('.trust_level="untrusted"')
                for item in overrides
            ))
            self.assertIn('web_search="disabled"', overrides)
            self.assertTrue(any(item.startswith("shell_environment_policy.filters=") for item in overrides))
            self.assertFalse(set(command).intersection({"--yolo", "--dangerously-bypass-approvals-and-sandbox"}))

    def test_claude_adapter_uses_strict_sandbox_tools_and_no_project_sources(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-claude-") as folder:
            tenant, workspace = self.make_space(folder)
            runner = self.make_runner(tenant)
            adapter = ClaudeCLIAdapter(
                str(FAKE_CLAUDE),
                permission_modes=ClaudeCLIAdapter.MODERN_PERMISSION_MODES,
                command_runner=runner,
            )
            code, _, _ = adapter.run(
                "claude-safe", workspace, "子任务 ID：S1", "workspace_write"
            )
            self.assertEqual(code, 0)
            command = runner.last_input_command
            self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(command[command.index("--setting-sources") + 1], "user")
            self.assertIn("--strict-mcp-config", command)
            self.assertEqual(json.loads(command[command.index("--mcp-config") + 1]), {"mcpServers": {}})
            settings = json.loads(command[command.index("--settings") + 1])
            self.assertTrue(settings["sandbox"]["enabled"])
            self.assertTrue(settings["sandbox"]["failIfUnavailable"])
            self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
            self.assertTrue(settings["permissions"]["blockReadsOutsideWorkingDirectories"])
            self.assertTrue(settings["disableAllHooks"])
            self.assertIn("WebFetch", settings["permissions"]["deny"])
            self.assertIn("--disallowedTools", command)
            self.assertNotIn("--dangerously-skip-permissions", command)

    def test_startup_check_fails_closed_for_platform_dependencies_and_clis(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-process-check-") as folder:
            tenant, _ = self.make_space(folder)
            non_linux = self.make_runner(tenant, platform="darwin")
            with self.assertRaisesRegex(ValueError, "仅支持 Linux"):
                non_linux.check()

            linux = self.make_runner(
                tenant, platform="linux", use_outer_sandbox=True
            )
            with patch.object(linux, "_resolve_binary", return_value=None):
                with self.assertRaisesRegex(ValueError, "沙箱依赖"):
                    linux.check()

            def resolve(name):
                return None if name == "claude" else "/usr/bin/" + name

            with patch.object(linux, "_resolve_binary", side_effect=resolve):
                with self.assertRaisesRegex(ValueError, "claude"):
                    linux.check()

            probes = []
            with patch.object(linux, "_resolve_binary", side_effect=lambda name: "/bin/" + name):
                with patch.object(linux, "_run_probe", side_effect=lambda command, _message: probes.append(command)):
                    linux.check()
            self.assertEqual(probes[0][0], "bwrap")
            self.assertEqual(probes[1][1:3], ["sandbox", "linux"])
            self.assertEqual(probes[2][1], "--version")

    def test_opt_cli_installation_is_mounted_as_one_read_only_tree(self):
        self.assertEqual(
            ProcessCommandRunner._cli_install_root(
                Path("/opt/fastlab-cli/bin/codex")
            ),
            Path("/opt/fastlab-cli"),
        )
        self.assertEqual(
            ProcessCommandRunner._cli_install_root(
                Path("/opt/fastlab-cli/lib/node_modules/@anthropic-ai/claude-code/cli.js")
            ),
            Path("/opt/fastlab-cli"),
        )
        self.assertIsNone(
            ProcessCommandRunner._cli_install_root(Path("/usr/local/bin/codex"))
        )
        self.assertIsNone(
            ProcessCommandRunner._cli_install_root(Path("/home/fastlab/bin/codex"))
        )


if __name__ == "__main__":
    unittest.main()
