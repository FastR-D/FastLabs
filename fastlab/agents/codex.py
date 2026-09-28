"""Codex CLI adapter and planner export."""

import json
import re
import subprocess
import sys
import threading
from pathlib import Path

from .core import (
    AgentAdapter,
    PLAN_CAPABILITY,
    RESUME_CAPABILITY,
    SKILLS_CAPABILITY,
    VERIFY_CAPABILITY,
    WRITE_CAPABILITY,
    process_group_options,
    terminate_process,
)
from .planner import CodexPlanner


class CodexCLIAdapter(AgentAdapter):
    """Codex CLI worker and planner adapter."""

    kind = "codex-cli"
    capabilities = frozenset(
        {
            PLAN_CAPABILITY,
            WRITE_CAPABILITY,
            RESUME_CAPABILITY,
            VERIFY_CAPABILITY,
            SKILLS_CAPABILITY,
        }
    )

    SAFE_CONFIG_OVERRIDES = (
        'approval_policy="never"',
        "mcp_servers={}",
        'web_search="disabled"',
        "allow_login_shell=false",
        "features.apps=false",
        "features.hooks=false",
        "features.memories=false",
        "features.multi_agent=false",
        "features.remote_plugin=false",
        'shell_environment_policy.inherit="core"',
        "shell_environment_policy.ignore_default_excludes=false",
        (
            'shell_environment_policy.filters={PATH="include",HOME="include",'
            'TMPDIR="include",TMP="include",TEMP="include",LANG="include",'
            'LC_ALL="include",LC_CTYPE="include",TERM="include"}'
        ),
    )

    def __init__(self, binary, runtime_dir, command_runner=None, models=None):
        self.binary = str(binary)
        self.command_prefix = (
            [sys.executable, self.binary]
            if Path(self.binary).suffix == ".py"
            else [self.binary]
        )
        runner_runtime = getattr(command_runner, "runtime_directory", None)
        self.runtime_dir = Path(
            runner_runtime("codex") if callable(runner_runtime) else runtime_dir
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.command_runner = command_runner
        self.configured_models = [
            str(item).strip() for item in (models or []) if str(item).strip()
        ]
        self._active = {}
        self._active_lock = threading.Lock()

    def run(
        self,
        key,
        cwd,
        prompt,
        sandbox,
        schema=None,
        resume_session=None,
        on_event=None,
        model=None,
        effort=None,
        skills=None,
        local_network=False,
    ):
        output_path = self.runtime_dir / (
            re.sub(r"[^a-zA-Z0-9_.-]", "-", key) + "-last.txt"
        )
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        cli_sandbox = str(sandbox).replace("_", "-")
        security_arguments = [
            "--sandbox",
            cli_sandbox,
            "--ask-for-approval",
            "never",
        ]
        for override in self.SAFE_CONFIG_OVERRIDES:
            security_arguments.extend(["-c", override])
        security_arguments.extend(
            [
                "-c",
                'projects.%s.trust_level="untrusted"'
                % json.dumps(str(Path(cwd).resolve())),
            ]
        )
        if resume_session:
            command = self.command_prefix + ["exec"] + security_arguments + [
                "resume",
                "--json",
                "-o",
                str(output_path),
                resume_session,
                "-",
            ]
        else:
            command = self.command_prefix + ["exec"] + security_arguments + [
                "--json",
                "--color",
                "never",
                "-C",
                str(cwd),
                "-o",
                str(output_path),
            ]
            if local_network:
                if cli_sandbox != "workspace-write":
                    raise ValueError(
                        "本地网络验收只能在临时可写 Worktree 中运行。"
                    )
                # Keep public network access denied. The verifier may bind and
                # call a local test server, but cannot reach arbitrary hosts.
                command.extend(
                    [
                        "-c",
                        "sandbox_workspace_write.network_access=true",
                        "-c",
                        "features.network_proxy.enabled=true",
                        "-c",
                        (
                            'features.network_proxy.domains={ localhost = "allow", '
                            '"127.0.0.1" = "allow", "::1" = "allow" }'
                        ),
                    ]
                )
            if model:
                command.extend(["--model", model])
            if schema:
                schema_path = schema if isinstance(schema, (str, Path)) else None
                if schema_path:
                    command.extend(["--output-schema", str(schema_path)])
            command.append("-")
        skill_note = (
            "\n\n已显式注入 Skills：\n"
            + "\n".join(
                "- %s (%s)" % (item["name"], item["path"])
                for item in (skills or [])
            )
            if skills
            else ""
        )
        process_cwd, process_env = str(cwd), None
        if self.command_runner:
            command, process_cwd, process_env = self.command_runner.wrap(
                command,
                cwd,
                writable=str(sandbox).replace("-", "_") != "read_only",
                executor="codex",
                key=key,
            )
        process = subprocess.Popen(
            command,
            cwd=process_cwd,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_options(),
        )
        started = getattr(self.command_runner, "started", None)
        if callable(started):
            try:
                started(key, process)
            except Exception:
                terminate_process(process)
                raise
        if process_env:
            process_env.clear()
        with self._active_lock:
            self._active[key] = process
        session_id = resume_session
        last_message = ""
        try:
            try:
                process.stdin.write(prompt + skill_note)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    session_id = payload.get("thread_id") or session_id
                    kind = payload.get("type", "codex")
                    message = self._event_message(payload)
                except json.JSONDecodeError:
                    payload = None
                    kind, message = "codex.output", line
                if message:
                    last_message = message
                if on_event and message:
                    on_event(kind, message, payload)
            code = process.wait()
            final = (
                output_path.read_text(encoding="utf-8")
                if output_path.exists()
                else ""
            )
            if code and not final.strip():
                final = last_message
            return code, session_id, final
        finally:
            with self._active_lock:
                self._active.pop(key, None)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.stdout and not process.stdout.closed:
                process.stdout.close()
            if self.command_runner:
                self.command_runner.finished(key)

    @staticmethod
    def _event_message(payload):
        item = payload.get("item") or {}
        if isinstance(item, dict):
            for key in ("text", "output", "aggregated_output", "command"):
                if item.get(key):
                    return str(item[key])
        message = payload.get("message")
        if message:
            return str(message)
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error.strip():
            return error.strip()
        return None

    def interrupt(self, key):
        if self.command_runner:
            self.command_runner.interrupt(key)
        with self._active_lock:
            process = self._active.get(key)
        if process is None:
            return False
        terminate_process(process)
        return True

    def health(self):
        return {
            "adapter": self.kind,
            "available": True,
            "executable": self.binary,
            "capabilities": sorted(self.capabilities),
        }

    def models(self):
        return [{"model": item} for item in self.configured_models]

    def close(self):
        with self._active_lock:
            processes = list(self._active.values())
        for process in processes:
            try:
                terminate_process(process)
            except Exception:
                pass


__all__ = ["CodexCLIAdapter", "CodexPlanner"]
