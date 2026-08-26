"""Planner and worker adapters used by FastLab."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path


PLAN_CAPABILITY = "plan"
WRITE_CAPABILITY = "write"
RESUME_CAPABILITY = "resume"
VERIFY_CAPABILITY = "verify"
SKILLS_CAPABILITY = "skills"


class PlannerError(RuntimeError):
    """A safe, user-facing planner failure."""


def parse_json_object(value):
    """Parse a planner response as a JSON object, accepting a JSON code fence."""
    text = str(value or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise PlannerError("Planner 没有返回 JSON。")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(
            "Planner 返回的 JSON 无法解析（第 %s 行第 %s 列）：%s。"
            % (exc.lineno, exc.colno, exc.msg)
        ) from exc
    if not isinstance(result, dict):
        raise PlannerError("Planner 必须返回一个 JSON 对象。")
    return result


def validate_json_schema(value, schema, path="$", root_schema=None):
    """Validate the draft-07 subset used by FastLab's bundled schemas.

    Keeping this validator in the standard-library adapter avoids making
    ``jsonschema`` a runtime dependency while still enforcing the same schema
    for every planner backend.
    """
    if not isinstance(schema, dict):
        return value
    root_schema = root_schema or schema
    if "$ref" in schema:
        reference = str(schema["$ref"])
        if not reference.startswith("#/"):
            raise PlannerError("Planner Schema 使用了不支持的引用：%s" % reference)
        target = root_schema
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return validate_json_schema(value, target, path, root_schema)

    expected = schema.get("type")
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(matches.get(item, False) for item in allowed):
            raise PlannerError("Planner JSON 的 %s 类型不符合 Schema。" % path)
    if "enum" in schema and value not in schema["enum"]:
        raise PlannerError("Planner JSON 的 %s 只能是：%s。" % (
            path, ", ".join(map(str, schema["enum"]))
        ))

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise PlannerError("Planner JSON 的 %s 缺少字段：%s。" % (
                path, ", ".join(missing)
            ))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise PlannerError("Planner JSON 的 %s 包含未知字段：%s。" % (
                    path, ", ".join(extra)
                ))
        for key, item in value.items():
            if key in properties:
                validate_json_schema(item, properties[key], "%s.%s" % (path, key), root_schema)
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise PlannerError("Planner JSON 的 %s 项目过少。" % path)
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise PlannerError("Planner JSON 的 %s 项目过多。" % path)
        for index, item in enumerate(value):
            validate_json_schema(
                item, schema.get("items") or {}, "%s[%s]" % (path, index), root_schema
            )
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise PlannerError("Planner JSON 的 %s 不能为空。" % path)
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise PlannerError("Planner JSON 的 %s 过长。" % path)
        if schema.get("pattern") and not re.search(schema["pattern"], value):
            raise PlannerError("Planner JSON 的 %s 格式不符合 Schema。" % path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise PlannerError("Planner JSON 的 %s 小于允许值。" % path)
        if "maximum" in schema and value > schema["maximum"]:
            raise PlannerError("Planner JSON 的 %s 大于允许值。" % path)
    return value


class Planner:
    """One-shot plan producer. A planner never edits the repository."""

    backend = "unknown"
    kind = "planner"

    def plan(self, prompt, context):
        raise NotImplementedError

    def interrupt(self, key):
        return False

    def health(self):
        return {"backend": self.backend, "kind": self.kind, "available": True}

    def settings(self):
        return self.health()

    def close(self):
        return None


class _SchemaPlanner(Planner):
    def __init__(self, model, schema_path, skill_path):
        self.model = str(model or "").strip()
        self.schema_path = Path(schema_path).resolve()
        self.skill_path = Path(skill_path).resolve()
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.skill = self.skill_path.read_text(encoding="utf-8")

    def _result(self, value):
        result = parse_json_object(value)
        validate_json_schema(result, self.schema)
        return result

    def _planner_prompt(self, prompt, context):
        public_context = {
            key: _jsonable(value)
            for key, value in (context or {}).items()
            if key not in {"on_event", "key", "schema", "skill"}
        }
        return """You are FastLab's Planner. Produce one execution plan only.
Never edit files, run Git write operations, or start another Agent. Workers must
not create Workers. Assign each subtask to exactly one available executor.
Return only JSON matching the supplied planner schema; do not use Markdown.

FastLab orchestration rules:
{skill}

Request:
{prompt}

Context:
{context}
""".format(
            skill=self.skill,
            prompt=str(prompt),
            context=json.dumps(public_context, ensure_ascii=False, indent=2),
        )


class _CliPlanner(_SchemaPlanner):
    executor = "unknown"

    def __init__(self, adapter, model, schema_path, skill_path, effort=None):
        super().__init__(model, schema_path, skill_path)
        self.adapter = adapter
        self.effort = str(effort or "").strip()

    def plan(self, prompt, context):
        if self.adapter is None:
            raise PlannerError("%s Planner 不可用：未找到本地 CLI。" % self.executor.title())
        health = self.adapter.health()
        if not health.get("available", True):
            raise PlannerError("%s Planner 不可用：%s" % (
                self.executor.title(), health.get("error") or "请检查本地 CLI"
            ))
        context = context or {}
        key = str(context.get("key") or "fastlab-planner")
        code, session_id, final = self.adapter.run(
            key,
            Path(context.get("cwd") or Path.cwd()),
            self._planner_prompt(prompt, context),
            "read_only",
            schema=self.schema_path,
            on_event=context.get("on_event"),
            model=self.model or None,
            effort=self.effort or None,
            skills=[],
        )
        if code:
            detail = " ".join(str(final or "").split())[:800]
            raise PlannerError(
                "%s Planner 退出，代码 %s。%s"
                % (
                    self.executor.title(),
                    code,
                    (" 原因：%s" % detail) if detail else "",
                )
            )
        if context.get("on_event"):
            context["on_event"]("planner.completed", "%s Planner 已生成计划。" % self.executor.title(), {
                "backend": self.backend, "model": self.model, "sessionId": session_id
            })
        return self._result(final)

    def interrupt(self, key):
        return bool(self.adapter and self.adapter.interrupt(key))

    def health(self):
        adapter_health = self.adapter.health() if self.adapter else {}
        return {
            "backend": self.backend,
            "kind": self.kind,
            "model": self.model,
            "available": bool(self.adapter and adapter_health.get("available", True)),
            "executable": adapter_health.get("executable"),
            "error": None if self.adapter else "未找到本地 CLI。",
            "source": "environment",
        }


class CodexPlanner(_CliPlanner):
    backend = "codex"
    kind = "codex-cli"
    executor = "codex"


class ClaudePlanner(_CliPlanner):
    backend = "claude"
    kind = "claude-cli"
    executor = "claude"


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    if hasattr(value, "value"):
        return _jsonable(value.value)
    return str(value)


def _process_group_options():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def _terminate_process(process):
    if process.poll() is not None:
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
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


class AgentAdapter:
    kind = "unknown"
    capabilities = frozenset()

    def run(self, key, cwd, prompt, sandbox, schema=None, resume_session=None,
            on_event=None, model=None, effort=None, skills=None,
            local_network=False):
        raise NotImplementedError

    def interrupt(self, key):
        return False

    def supports(self, capability):
        return str(capability) in self.capabilities

    def health(self):
        return {
            "adapter": self.kind,
            "available": True,
            "capabilities": sorted(self.capabilities),
        }

    def models(self):
        return []

    def close(self):
        return None


class CodexCLIAdapter(AgentAdapter):
    """Codex CLI worker and planner adapter."""

    kind = "codex-cli"
    capabilities = frozenset({
        PLAN_CAPABILITY, WRITE_CAPABILITY, RESUME_CAPABILITY,
        VERIFY_CAPABILITY, SKILLS_CAPABILITY,
    })

    def __init__(self, binary, runtime_dir):
        self.binary = str(binary)
        self.command_prefix = [sys.executable, self.binary] if Path(self.binary).suffix == ".py" else [self.binary]
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._active = {}
        self._active_lock = threading.Lock()

    def run(self, key, cwd, prompt, sandbox, schema=None, resume_session=None,
            on_event=None, model=None, effort=None, skills=None,
            local_network=False):
        output_path = self.runtime_dir / (re.sub(r"[^a-zA-Z0-9_.-]", "-", key) + "-last.txt")
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        if resume_session:
            command = self.command_prefix + ["exec", "resume", "--json", "-o", str(output_path), resume_session, "-"]
        else:
            cli_sandbox = str(sandbox).replace("_", "-")
            command = self.command_prefix + [
                "exec", "--json", "--color", "never", "-C", str(cwd),
                "--sandbox", cli_sandbox, "-o", str(output_path),
            ]
            if local_network:
                if cli_sandbox != "workspace-write":
                    raise ValueError("本地网络验收只能在临时可写 Worktree 中运行。")
                # Keep public network access denied. The verifier may bind and
                # call a local test server, but cannot reach arbitrary hosts.
                command.extend([
                    "-c", "sandbox_workspace_write.network_access=true",
                    "-c", "features.network_proxy.enabled=true",
                    "-c", (
                        'features.network_proxy.domains={ localhost = "allow", '
                        '"127.0.0.1" = "allow", "::1" = "allow" }'
                    ),
                ])
            if model:
                command.extend(["--model", model])
            if schema:
                schema_path = schema if isinstance(schema, (str, Path)) else None
                if schema_path:
                    command.extend(["--output-schema", str(schema_path)])
            command.append("-")
        skill_note = "\n\n已显式注入 Skills：\n" + "\n".join(
            "- %s (%s)" % (item["name"], item["path"]) for item in (skills or [])
        ) if skills else ""
        process = subprocess.Popen(
            command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, **_process_group_options()
        )
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
            final = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
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
        with self._active_lock:
            process = self._active.get(key)
        if process is None:
            return False
        _terminate_process(process)
        return True

    def health(self):
        return {
            "adapter": self.kind,
            "available": True,
            "executable": self.binary,
            "capabilities": sorted(self.capabilities),
        }

    def close(self):
        with self._active_lock:
            processes = list(self._active.values())
        for process in processes:
            try:
                _terminate_process(process)
            except Exception:
                pass


class ClaudeCLIAdapter(AgentAdapter):
    """Claude Code print-mode adapter using its documented JSONL stream.

    FastLab deliberately never uses ``--dangerously-skip-permissions``. Modern
    Claude versions use ``dontAsk`` for read-only turns and ``auto`` for write
    turns, so a background run never waits for an approval dialog. Older Claude
    versions fall back to their safest supported modes.
    """

    kind = "claude-cli"
    capabilities = frozenset({
        PLAN_CAPABILITY, WRITE_CAPABILITY, RESUME_CAPABILITY,
        VERIFY_CAPABILITY, SKILLS_CAPABILITY,
    })

    READ_ONLY_ALLOWED_TOOLS = (
        "Read", "Glob", "Grep",
        "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
        "Bash(git show:*)", "Bash(git rev-parse:*)", "Bash(git ls-files:*)",
        "Bash(python -B -m unittest:*)", "Bash(python3 -B -m unittest:*)",
        "Bash(python -B -m pytest:*)", "Bash(python3 -B -m pytest:*)",
        "Bash(python -B tests/*)", "Bash(python3 -B tests/*)",
        "Bash(pytest:*)", "Bash(npm test:*)", "Bash(npm run test:*)",
        "Bash(cargo test:*)", "Bash(go test:*)",
    )

    DEFAULT_ALLOWED_TOOLS = (
        "Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit",
        "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
        "Bash(python -m unittest:*)", "Bash(python -m pytest:*)",
        "Bash(python3 -m unittest:*)", "Bash(python3 -m pytest:*)",
        "Bash(pytest:*)", "Bash(npm test:*)", "Bash(npm run test:*)",
        "Bash(cargo test:*)", "Bash(go test:*)",
    )

    MODERN_PERMISSION_MODES = frozenset({
        "acceptEdits", "auto", "default", "dontAsk", "plan",
    })
    LEGACY_PERMISSION_MODES = frozenset({"acceptEdits", "plan"})

    def __init__(self, binary, allowed_tools=None, max_turns=80, models=None,
                 sandbox_bash=None, permission_modes=None):
        self.binary = str(binary)
        self.command_prefix = (
            [sys.executable, self.binary]
            if Path(self.binary).suffix.lower() == ".py" else [self.binary]
        )
        self.allowed_tools = tuple(allowed_tools or self.DEFAULT_ALLOWED_TOOLS)
        self.sandbox_bash = os.name != "nt" if sandbox_bash is None else bool(sandbox_bash)
        self.max_turns = max(1, min(200, int(max_turns or 80)))
        self.configured_models = [str(item).strip() for item in (models or []) if str(item).strip()]
        if permission_modes is None:
            permission_modes = self._detect_permission_modes()
        self.permission_modes = frozenset(
            mode for mode in permission_modes if mode != "bypassPermissions"
        )
        self.read_permission_mode = (
            "dontAsk" if "dontAsk" in self.permission_modes else "plan"
        )
        if "auto" in self.permission_modes:
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
            texts = [str(item.get("text")) for item in content
                     if isinstance(item, dict) and item.get("text")]
            if texts:
                return "\n".join(texts)
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error.strip():
            return error.strip()
        return None

    def run(self, key, cwd, prompt, sandbox, schema=None, resume_session=None,
            on_event=None, model=None, effort=None, skills=None,
            local_network=False):
        read_only = str(sandbox).replace("-", "_") == "read_only"
        mode = self.read_permission_mode if read_only else self.write_permission_mode
        command = self.command_prefix + [
            "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", mode,
            "--max-turns", str(self.max_turns),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--no-chrome",
            "--setting-sources", "user", "--disable-slash-commands",
        ]
        if self.sandbox_bash and not read_only:
            command.extend([
                "--settings",
                json.dumps({
                    "sandbox": {
                        "enabled": True,
                        "allowUnsandboxedCommands": False,
                    }
                }, separators=(",", ":")),
            ])
        if resume_session:
            command.extend(["--resume", str(resume_session)])
        if model:
            command.extend(["--model", str(model)])
        if effort:
            command.extend(["--effort", "max" if str(effort) == "ultra" else str(effort)])
        if read_only:
            # dontAsk denies anything outside this explicit read/test surface
            # instead of pausing a background task for user approval.
            command.extend(["--tools", "Read,Glob,Grep,Bash"])
            command.extend([
                "--allowedTools", ",".join(self.READ_ONLY_ALLOWED_TOOLS),
            ])
        elif self.allowed_tools:
            command.extend(["--allowedTools", ",".join(self.allowed_tools)])
        additions = []
        if skills:
            additions.append(
                "已显式提供以下 Skills。开始前逐一读取对应 SKILL.md，并遵守其中适用的说明：\n"
                + "\n".join("- %s: %s" % (item["name"], item["path"]) for item in skills)
            )
        output_schema = self._schema(schema)
        if output_schema:
            command.extend(["--json-schema", json.dumps(output_schema, ensure_ascii=False)])
            additions.append("最终回复必须严格遵守 FastLab 通过 --json-schema 提供的结构。")
        full_prompt = str(prompt)
        if additions:
            full_prompt += "\n\n" + "\n\n".join(additions)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_process_group_options(),
        )
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
                        if structured is not None else str(payload.get("result") or "")
                    )
                    result_error = bool(payload.get("is_error"))
                    if result_error:
                        final = str(payload.get("result") or "").strip()
                        if not final and payload.get("api_error_status"):
                            final = "Claude API 错误（HTTP %s）。" % payload["api_error_status"]
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
                            blocked.append(str(command_text or denial.get("tool_name") or "未知命令"))
                        final = (
                            "Claude 命令被权限策略拦截：%s。"
                            "请把必要命令精确加入 FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS 后重启 FastLab。"
                            % "；".join(blocked)
                        )
                        result_error = True
                event_message = self._message(payload)
                if event_message:
                    last_message = event_message
                if on_event and event_message and event_message != last_emitted_message:
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

    def interrupt(self, key):
        with self._active_lock:
            process = self._active.get(key)
        if process is None:
            return False
        _terminate_process(process)
        return True

    def health(self):
        return {
            "adapter": self.kind,
            "available": Path(self.binary).is_file(),
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
                _terminate_process(process)
            except Exception:
                pass
