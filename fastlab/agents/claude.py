"""Claude Code print-mode adapter and planner export."""

import json
import os
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
from .planner import ClaudePlanner


class ClaudeCLIAdapter(AgentAdapter):
    """Claude Code print-mode adapter using its documented JSONL stream.

    FastLab deliberately never uses ``--dangerously-skip-permissions``. Modern
    Claude versions use ``dontAsk`` for read-only turns and ``auto`` for write
    turns, so a background run never waits for an approval dialog. Older Claude
    versions fall back to their safest supported modes.
    """

    kind = "claude-cli"
    capabilities = frozenset(
        {
            PLAN_CAPABILITY,
            WRITE_CAPABILITY,
            RESUME_CAPABILITY,
            VERIFY_CAPABILITY,
            SKILLS_CAPABILITY,
        }
    )

    READ_ONLY_ALLOWED_TOOLS = (
        "Read",
        "Glob",
        "Grep",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git log:*)",
        "Bash(git show:*)",
        "Bash(git rev-parse:*)",
        "Bash(git ls-files:*)",
        "Bash(python -B -m unittest:*)",
        "Bash(python3 -B -m unittest:*)",
        "Bash(python -B -m pytest:*)",
        "Bash(python3 -B -m pytest:*)",
        "Bash(python -B tests/*)",
        "Bash(python3 -B tests/*)",
        "Bash(pytest:*)",
        "Bash(npm test:*)",
        "Bash(npm run test:*)",
        "Bash(cargo test:*)",
        "Bash(go test:*)",
    )

    DEFAULT_ALLOWED_TOOLS = (
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "NotebookEdit",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git log:*)",
        "Bash(python -m unittest:*)",
        "Bash(python -m pytest:*)",
        "Bash(python3 -m unittest:*)",
        "Bash(python3 -m pytest:*)",
        "Bash(pytest:*)",
        "Bash(npm test:*)",
        "Bash(npm run test:*)",
        "Bash(cargo test:*)",
        "Bash(go test:*)",
    )

    MODERN_PERMISSION_MODES = frozenset(
        {"acceptEdits", "auto", "default", "dontAsk", "plan"}
    )
    LEGACY_PERMISSION_MODES = frozenset({"acceptEdits", "plan"})

    DISALLOWED_TOOLS = (
        "Agent",
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "Skill",
        "Task",
        "WebFetch",
        "WebSearch",
    )

    def __init__(
        self,
        binary,
        allowed_tools=None,
        max_turns=80,
        models=None,
        sandbox_bash=None,
        permission_modes=None,
        command_runner=None,
    ):
        self.binary = str(binary)
        self.command_prefix = (
            [sys.executable, self.binary]
            if Path(self.binary).suffix.lower() == ".py"
            else [self.binary]
        )
        self.allowed_tools = tuple(allowed_tools or self.DEFAULT_ALLOWED_TOOLS)
        self.sandbox_bash = (
            os.name != "nt" if sandbox_bash is None else bool(sandbox_bash)
        )
        self.max_turns = max(1, min(200, int(max_turns or 80)))
        self.configured_models = [
            str(item).strip() for item in (models or []) if str(item).strip()
        ]
        self.command_runner = command_runner
        if permission_modes is None:
            permission_modes = self._detect_permission_modes()
        self.permission_modes = frozenset(
            mode for mode in permission_modes if mode != "bypassPermissions"
        )
        self.read_permission_mode = (
            "dontAsk" if "dontAsk" in self.permission_modes else "plan"
        )
        if (
            getattr(command_runner, "strict_cli", False)
            and "dontAsk" in self.permission_modes
        ):
            self.write_permission_mode = "dontAsk"
        elif "auto" in self.permission_modes:
            self.write_permission_mode = "auto"
        elif "dontAsk" in self.permission_modes:
            self.write_permission_mode = "dontAsk"
        else:
            self.write_permission_mode = "acceptEdits"
        self._active = {}
        self._active_lock = threading.Lock()

    @staticmethod
    def _permission_modes_from_help(output):
        for line in str(output or "").splitlines():
            if "--permission-mode" not in line or "choices:" not in line:
                continue
            choices = line.split("choices:", 1)[1]
            modes = set(re.findall(r'"([A-Za-z]+)"', choices))
            # A bypass mode is never considered selectable by FastLab.
            modes.discard("bypassPermissions")
            if modes:
                return frozenset(modes)
        return frozenset()

    def _detect_permission_modes(self):
        # Test doubles and user-provided Python wrappers should not be executed
        # during FastLab startup merely to inspect their help output.
        if Path(self.binary).suffix.lower() == ".py":
            return self.MODERN_PERMISSION_MODES
        try:
            completed = subprocess.run(
                self.command_prefix + ["--help"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            detected = self._permission_modes_from_help(completed.stdout)
            if detected:
                return detected
        except (OSError, subprocess.SubprocessError):
            pass
        return self.LEGACY_PERMISSION_MODES

    @staticmethod
    def _schema(schema):
        if not schema:
            return None
        if isinstance(schema, (str, Path)):
            return json.loads(Path(schema).read_text(encoding="utf-8"))
        return schema

    @staticmethod
    def _message(payload):
        event_type = str(payload.get("type") or "")
        if event_type == "result":
            result = str(payload.get("result") or "").strip()
            if result:
                return result
            structured = payload.get("structured_output")
            if isinstance(structured, dict) and structured.get("summary"):
                return str(structured["summary"])
            if payload.get("api_error_status"):
                return "Claude API 错误（HTTP %s）。" % payload["api_error_status"]
            return "Claude 本轮完成。" if not payload.get("is_error") else None
        if event_type == "system":
            return "Claude 会话已开始。" if payload.get("subtype") == "init" else None
        if event_type == "user":
            # Claude stream-json uses user messages for tool results. They are
            # transport details, not user-authored log messages.
            return None
        message = payload.get("message") or {}
        if isinstance(message, str):
            return message.strip() or None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            texts = [
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and item.get("text")
            ]
            if texts:
                return "\n".join(texts)
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error.strip():
            return error.strip()
        return None

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
        read_only = str(sandbox).replace("-", "_") == "read_only"
        mode = self.read_permission_mode if read_only else self.write_permission_mode
        command = self.command_prefix + [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            mode,
            "--max-turns",
            str(self.max_turns),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-chrome",
            "--setting-sources",
            "user",
            "--disable-slash-commands",
        ]
        if self.sandbox_bash:
            command.extend(
                [
                    "--settings",
                    json.dumps(
                        {
                            "disableAllHooks": True,
                            "sandbox": {
                                "enabled": True,
                                "allowUnsandboxedCommands": False,
                                "failIfUnavailable": True,
                            },
                            "permissions": {
                                "blockReadsOutsideWorkingDirectories": True,
                                "deny": list(self.DISALLOWED_TOOLS),
                            },
                        },
                        separators=(",", ":"),
                    ),
                ]
            )
        if resume_session:
            command.extend(["--resume", str(resume_session)])
        if model:
            command.extend(["--model", str(model)])
        if effort:
            command.extend(
                ["--effort", "max" if str(effort) == "ultra" else str(effort)]
            )
        if read_only:
            # dontAsk denies anything outside this explicit read/test surface
            # instead of pausing a background task for user approval.
            command.extend(["--tools", "Read,Glob,Grep,Bash"])
            command.extend(
                ["--allowedTools", ",".join(self.READ_ONLY_ALLOWED_TOOLS)]
            )
        elif self.allowed_tools:
            command.extend(
                ["--tools", "Read,Glob,Grep,Edit,Write,NotebookEdit,Bash"]
            )
            command.extend(["--allowedTools", ",".join(self.allowed_tools)])
        command.extend(["--disallowedTools", ",".join(self.DISALLOWED_TOOLS)])
        if self.command_runner:
            skill_directories = sorted(
                {
                    str(Path(item["path"]).resolve().parent)
                    for item in (skills or [])
                    if item.get("path")
                }
            )
            for directory in skill_directories:
                command.extend(["--add-dir", directory])
        additions = []
        if skills:
            additions.append(
                "已显式提供以下 Skills。开始前逐一读取对应 SKILL.md，并遵守其中适用的说明：\n"
                + "\n".join(
                    "- %s: %s" % (item["name"], item["path"])
                    for item in skills
                )
            )
        output_schema = self._schema(schema)
        if output_schema:
            command.extend(
                ["--json-schema", json.dumps(output_schema, ensure_ascii=False)]
            )
            additions.append(
                "最终回复必须严格遵守 FastLab 通过 --json-schema 提供的结构。"
            )
        full_prompt = str(prompt)
        if additions:
            full_prompt += "\n\n" + "\n\n".join(additions)
        process_cwd, process_env = str(cwd), None
        if self.command_runner:
            command, process_cwd, process_env = self.command_runner.wrap(
                command,
                cwd,
                writable=not read_only,
                executor="claude",
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
        final = ""
        last_message = ""
        last_emitted_message = ""
        result_error = False
        try:
            try:
                process.stdin.write(full_prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = {"type": "claude.output", "message": line}
                session_id = payload.get("session_id") or session_id
                if payload.get("type") == "result":
                    structured = payload.get("structured_output")
                    successful_final = (
                        json.dumps(structured, ensure_ascii=False)
                        if structured is not None
                        else str(payload.get("result") or "")
                    )
                    result_error = bool(payload.get("is_error"))
                    if result_error:
                        final = str(payload.get("result") or "").strip()
                        if not final and payload.get("api_error_status"):
                            final = "Claude API 错误（HTTP %s）。" % payload[
                                "api_error_status"
                            ]
                        if not final:
                            final = successful_final
                    else:
                        final = successful_final
                    denials = payload.get("permission_denials") or []
                    if denials and not result_error:
                        blocked = []
                        for denial in denials[:5]:
                            tool_input = denial.get("tool_input") or {}
                            command_text = tool_input.get("command")
                            blocked.append(
                                str(
                                    command_text
                                    or denial.get("tool_name")
                                    or "未知命令"
                                )
                            )
                        final = (
                            "Claude 命令被权限策略拦截：%s。"
                            "请把必要命令精确加入 FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS 后重启 FastLab。"
                            % "；".join(blocked)
                        )
                        result_error = True
                event_message = self._message(payload)
                if event_message:
                    last_message = event_message
                if (
                    on_event
                    and event_message
                    and event_message != last_emitted_message
                ):
                    on_event(
                        "claude.%s" % payload.get("type", "output"),
                        event_message,
                        payload,
                    )
                    last_emitted_message = event_message
            code = process.wait()
            if result_error and code == 0:
                code = 1
            if code and not final:
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
            # Container/direct runners validate the executable in their own
            # runtime. Checking a relative host path here produced a false
            # negative for the normal server configuration (``claude``).
            "available": bool(self.command_runner) or Path(self.binary).is_file(),
            "executable": self.binary,
            "capabilities": sorted(self.capabilities),
            "permissionMode": self.write_permission_mode,
            "permissionModes": {
                "readOnly": self.read_permission_mode,
                "workspaceWrite": self.write_permission_mode,
                "supported": sorted(self.permission_modes),
            },
            "unattended": (
                self.read_permission_mode == "dontAsk"
                and self.write_permission_mode in {"auto", "dontAsk"}
            ),
            "bashSandbox": self.sandbox_bash,
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


__all__ = ["ClaudeCLIAdapter", "ClaudePlanner"]
