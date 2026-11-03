#!/usr/bin/env python3
"""FastLab: an authenticated, tenant-isolated multi-Agent task gateway."""

import json
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from .agent_library import AgentLibrary
from .agents import (
    AgentAdapter,
    ClaudeCLIAdapter,
    ClaudePlanner,
    CodexCLIAdapter,
    CodexPlanner,
    PLAN_CAPABILITY,
    Planner,
    RESUME_CAPABILITY,
    VERIFY_CAPABILITY,
    WRITE_CAPABILITY,
)
from .config import (
    APP_ROOT, DEFAULT_GLOBAL_CONCURRENCY, EFFORTS, EXECUTOR_LABELS,
    MAX_GLOBAL_CONCURRENCY, PLANNER_SCHEMA, PLANNER_SKILL, compact_error,
    parse_agent_timeout, parse_claude_extra_allowed_tools, resolve_executable,
    slug, utc_now, validate_execution_environment,
)
from .errors import (
    AgentRunCancelled, DeliveryReverificationRequired,
)
from .documents import DocumentProjector
from .git_service import GitService
from .planning import PlanningService
from .runtime import AgentRuntime
from .scheduling import AgentRunGate
from .verification import VerificationService

from .storage import Store


class FastLab:
    def __init__(self, *, data_dir=None, codex_bin=None, agent_adapter=None,
                 claude_bin=None, planner=None, repository_root=None,
                 command_runner=None, shared_agent_gate=None, tenant_limit=None,
                 credential_available=None, redactor=None):
        validate_execution_environment()
        self.workspace = None
        self.default_repository_id = None
        self.data_dir = Path(data_dir or APP_ROOT / ".fastlab").expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repository_root = (
            Path(repository_root or self.data_dir / "repositories").expanduser().resolve()
        )
        self.repository_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.command_runner = command_runner
        self.credential_available = credential_available or (lambda _provider: True)
        self.allowed_models = {
            "codex": [item.strip() for item in os.environ.get("FASTLAB_CODEX_MODELS", "").split(",") if item.strip()],
            "claude": [item.strip() for item in os.environ.get("FASTLAB_CLAUDE_MODELS", "sonnet,opus").split(",") if item.strip()],
        }
        self.store = Store(self.data_dir / "fastlab.db", redactor=redactor)
        self.documents = DocumentProjector(self.store, self.data_dir)
        self.merge_lock = threading.Lock()
        self.repository_lock = threading.Lock()
        self.settings_lock = threading.RLock()
        self.scheduler_lock = threading.Lock()
        self.schedulers = {}
        self.thread_lock = threading.Lock()
        self.background_threads = set()
        self.task_locks_lock = threading.Lock()
        self.task_locks = {}
        self.shutting_down = False
        self.agent_timeout = parse_agent_timeout(os.environ.get("FASTLAB_AGENT_TIMEOUT"))
        self.git_bin = shutil.which("git")
        self.git_service = GitService(self.git_bin)
        self._validate_environment()
        self._initialize_repositories()
        self.git_service.set_default_workspace(self.workspace)
        codex_adapter = None
        if agent_adapter is not None:
            if not isinstance(agent_adapter, AgentAdapter):
                required = {"run", "interrupt", "health", "models"}
                if not all(hasattr(agent_adapter, name) for name in required):
                    raise TypeError("agent_adapter 不符合 AgentAdapter 接口。")
            codex_adapter = agent_adapter
        else:
            requested_codex = codex_bin or os.environ.get("FASTLAB_CODEX_BIN") or "codex"
            resolved = requested_codex if command_runner else resolve_executable(requested_codex)
            if resolved:
                codex_adapter = CodexCLIAdapter(
                    resolved, self.data_dir / "runtime", command_runner=command_runner,
                    models=self.allowed_models["codex"],
                )
        self.codex_bin = getattr(codex_adapter, "binary", None)
        requested_claude = claude_bin or os.environ.get("FASTLAB_CLAUDE_BIN") or "claude"
        self.claude_bin = requested_claude if command_runner else resolve_executable(requested_claude)
        self.executors = {}
        if codex_adapter:
            self.executors["codex"] = codex_adapter
        if self.claude_bin:
            claude_allowed_tools = list(ClaudeCLIAdapter.DEFAULT_ALLOWED_TOOLS)
            claude_allowed_tools.extend(parse_claude_extra_allowed_tools(
                os.environ.get("FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS")
            ))
            self.executors["claude"] = ClaudeCLIAdapter(
                self.claude_bin,
                allowed_tools=claude_allowed_tools,
                models=self.allowed_models["claude"],
                permission_modes=ClaudeCLIAdapter.MODERN_PERMISSION_MODES if command_runner else None,
                command_runner=command_runner,
            )
        self.planner_override = planner is not None
        self.planner = planner or self._planner_from_environment()
        if not isinstance(self.planner, Planner) and not all(
            hasattr(self.planner, name) for name in ("plan", "interrupt", "health", "settings")
        ):
            raise TypeError("planner 不符合 Planner 接口。")
        self.active_run_adapters = {}
        self.active_run_lock = threading.Lock()
        global_limit = self._normalize_global_concurrency(os.environ.get(
            "FASTLAB_GLOBAL_CONCURRENCY",
            self.store.get_setting("agent.globalConcurrency", DEFAULT_GLOBAL_CONCURRENCY),
        ))
        self.global_agent_gate = shared_agent_gate or AgentRunGate(global_limit)
        per_user = tenant_limit or os.environ.get("FASTLAB_MAX_CONCURRENCY_PER_USER") or global_limit
        self.agent_gate = AgentRunGate(self._normalize_global_concurrency(per_user))
        self.runtime = AgentRuntime(self)
        self.verification = VerificationService(self)
        self.user_skill_path = self.data_dir / "skills" / "orchestration" / "SKILL.md"
        self._ensure_user_skill()
        self.agent_library = AgentLibrary(self.store, self._validate_role_preference)
        self.agent_library.seed(self.agent_preferences())
        self._rebuild_documents()

    def _planner_from_environment(self):
        backend = str(os.environ.get("FASTLAB_PLANNER_BACKEND") or "codex").strip().lower()
        model = ""
        if backend == "codex":
            return CodexPlanner(
                self.executors.get("codex"), model, PLANNER_SCHEMA, PLANNER_SKILL,
                effort="high",
            )
        if backend == "claude":
            return ClaudePlanner(
                self.executors.get("claude"), model, PLANNER_SCHEMA, PLANNER_SKILL,
                effort="high",
            )
        raise ValueError(
            "FASTLAB_PLANNER_BACKEND 只能是 codex 或 claude，当前为：%s" % backend
        )

    def _ensure_user_skill(self):
        self.user_skill_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.user_skill_path.exists():
            self.user_skill_path.write_text(
                PLANNER_SKILL.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if os.name != "nt":
            os.chmod(self.user_skill_path.parent, 0o700)
            os.chmod(self.user_skill_path, 0o600)

    def skill_settings(self, include_content=True):
        with self.settings_lock:
            self._ensure_user_skill()
            content = self.user_skill_path.read_text(encoding="utf-8")
        default = PLANNER_SKILL.read_text(encoding="utf-8")
        result = {
            "name": "orchestration",
            "customized": content != default,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "source": "user" if content != default else "builtin",
        }
        if include_content:
            result["content"] = content
        return result

    def update_skill_settings(self, content):
        value = str(content or "")
        if not value.strip():
            raise ValueError("分发 Skill 不能为空。")
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("分发 Skill 不能超过 64 KiB。")
        if "\x00" in value:
            raise ValueError("分发 Skill 包含无效字符。")
        with self.settings_lock:
            self._ensure_user_skill()
            temporary = self.user_skill_path.with_name(
                ".SKILL.%s.tmp" % uuid.uuid4().hex
            )
            try:
                temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, self.user_skill_path)
            finally:
                temporary.unlink(missing_ok=True)
        return self.skill_settings()

    def reset_skill_settings(self):
        return self.update_skill_settings(PLANNER_SKILL.read_text(encoding="utf-8"))

    def agent_preferences(self):
        default_backend = getattr(self.planner, "backend", "codex") or "codex"
        raw = self.store.get_setting("agent.preferences", {}) or {}
        result = {}
        for role in ("planner", "worker", "verifier"):
            fallback = default_backend if role == "planner" else "codex"
            item = raw.get(role) if isinstance(raw.get(role), dict) else {}
            executor = str(item.get("executor") or fallback).strip().lower()
            if executor not in EXECUTOR_LABELS:
                executor = fallback
            fallback_model = (
                getattr(self.planner, "model", "")
                if role == "planner" and self.planner_override else ""
            )
            fallback_effort = (
                getattr(self.planner, "effort", "")
                if role == "planner" and self.planner_override else "high"
            )
            result[role] = {
                "executor": executor,
                "model": str(item.get("model", fallback_model) or "").strip(),
                "reasoningEffort": str(
                    item.get("reasoningEffort", fallback_effort) or "high"
                ).strip(),
            }
        return result

    def _validate_role_preference(self, role, value):
        item = value if isinstance(value, dict) else {}
        executor = str(item.get("executor") or "").strip().lower()
        capability = {
            "planner": PLAN_CAPABILITY, "worker": WRITE_CAPABILITY,
            "verifier": VERIFY_CAPABILITY,
        }[role]
        executor, adapter = self._executor_adapter(executor, capability)
        model = str(item.get("model") or "").strip()
        allowed = {
            str(entry.get("model") or entry.get("slug") or entry.get("id"))
            for entry in adapter.models() if isinstance(entry, dict)
        }
        if model and self.command_runner and (not allowed or model not in allowed):
            raise ValueError("模型 %s 不在 %s 的服务器允许列表中。" % (
                model, EXECUTOR_LABELS[executor]
            ))
        effort = str(item.get("reasoningEffort") or "high").strip().lower()
        if effort not in EFFORTS:
            raise ValueError("推理等级无效。")
        return {"executor": executor, "model": model, "reasoningEffort": effort}

    def _planner_for_role(self, setting, skill_content=None):
        if self.planner_override:
            return self.planner
        executor = str((setting or {}).get("executor") or "codex").lower()
        adapter = self.executors.get(executor)
        planner_type = ClaudePlanner if executor == "claude" else CodexPlanner
        return planner_type(
            adapter,
            (setting or {}).get("model") or "",
            PLANNER_SCHEMA,
            self.user_skill_path,
            effort=(setting or {}).get("reasoningEffort") or "high",
            skill_text=skill_content,
        )

    def _validate_environment(self):
        if self.git_bin is None:
            raise ValueError("找不到 Git，请先安装 Git 并重新打开终端。")

    def _initialize_repositories(self):
        self._migrate_task_repositories()
        self._sync_default_repository()

    def _available_alias(self, value):
        base_alias = slug(value, 36)
        alias = base_alias
        index = 2
        while self.store.find_repository(alias):
            alias = "%s-%s" % (base_alias, index)
            index += 1
        return alias

    def _migrate_task_repositories(self):
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT id,workspace FROM tasks WHERE repository_id IS NULL"
            ).fetchall()]
        migrated = {}
        for row in rows:
            workspace = row["workspace"]
            match = migrated.get(workspace) or self.store.find_repository(workspace)
            if not match:
                try:
                    resolved = self._resolve_workspace(workspace)
                    match = self.store.add_repository(
                        self._available_alias(resolved.name), str(resolved)
                    )
                except (ValueError, RuntimeError):
                    match = None
            if match:
                migrated[workspace] = match
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE tasks SET repository_id=? WHERE id=?", (match["id"], row["id"])
                    )

    def _sync_default_repository(self):
        repositories = self.store.list_repositories()
        default = next((item for item in repositories if item["is_default"]), None)
        if default is None and repositories:
            first = repositories[0]
            default = self.store.update_repository(
                first["id"], first["alias"], first["path"], is_default=True
            )
        self.default_repository_id = default["id"] if default else None
        self.workspace = Path(default["path"]) if default else None

    def _resolve_workspace(self, value):
        source = self.workspace if value is None else value
        if source is None:
            raise ValueError("尚未登记 Git 仓库，请先在设置页添加仓库。")
        workspace = Path(source).expanduser().resolve()
        try:
            workspace.relative_to(self.repository_root)
        except ValueError as exc:
            raise ValueError("仓库目录越出当前用户空间。") from exc
        if not workspace.is_dir():
            raise ValueError("工作目录不存在：%s" % workspace)
        inside = self._git(
            ["rev-parse", "--is-inside-work-tree"], cwd=workspace, check=False
        )
        if inside.returncode != 0:
            raise ValueError("工作目录不是 Git 仓库：%s" % workspace)
        root = self._git(["rev-parse", "--show-toplevel"], cwd=workspace).stdout.strip()
        if not root:
            raise ValueError("无法确定 Git 仓库根目录：%s" % workspace)
        resolved = Path(root).expanduser().resolve()
        try:
            resolved.relative_to(self.repository_root)
        except ValueError as exc:
            raise ValueError("Git 根目录越出当前用户空间。") from exc
        return resolved

    def _git(self, arguments, cwd=None, check=True, timeout=None):
        return self.git_service.run(arguments, cwd=cwd, check=check, timeout=timeout)

    @staticmethod
    def _normalize_global_concurrency(value):
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("全局并发必须是整数。") from exc
        if not 1 <= number <= MAX_GLOBAL_CONCURRENCY:
            raise ValueError("全局并发必须在 1–%s 之间。" % MAX_GLOBAL_CONCURRENCY)
        return number

    def _executor_adapter(self, executor, capability=None):
        name = str(executor or "").strip().lower()
        if name not in EXECUTOR_LABELS:
            raise ValueError("执行器只能选择 Codex 或 Claude。")
        adapter = self.executors.get(name)
        if adapter is None:
            raise ValueError("%s 尚未安装。" % EXECUTOR_LABELS[name])
        if capability and not adapter.supports(capability):
            raise ValueError("%s 不支持当前操作。" % EXECUTOR_LABELS[name])
        provider = "openai" if name == "codex" else "anthropic"
        if not self.credential_available(provider):
            raise ValueError("请先在设置页配置 %s 的 CLI 登录或 API Key。" % provider.title())
        health = adapter.health()
        if not health.get("available", True):
            raise ValueError("%s 当前不可用：%s" % (
                EXECUTOR_LABELS[name], health.get("error") or "请检查登录或安装"
            ))
        return name, adapter

    def _executor_snapshot(self, executor, model=None, effort=None):
        name, adapter = self._executor_adapter(executor)
        selected_model = str(model or "").strip()
        if self.command_runner and selected_model and selected_model not in self.allowed_models[name]:
            raise ValueError("所选模型不在服务器允许列表中。")
        return {
            "executor": name,
            "name": EXECUTOR_LABELS[name],
            "model": selected_model,
            "reasoningEffort": str(effort or "high"),
            "kind": adapter.kind,
        }

    def list_executors(self):
        result = []
        for name in ("codex", "claude"):
            adapter = self.executors.get(name)
            if adapter is None:
                result.append({
                    "id": name, "name": EXECUTOR_LABELS[name], "available": False,
                    "kind": "codex-cli" if name == "codex" else "claude-cli",
                    "models": [], "error": "未安装。",
                })
                continue
            try:
                health = adapter.health()
                provider = "openai" if name == "codex" else "anthropic"
                credential_ready = bool(self.credential_available(provider))
                result.append({
                    "id": name, "name": EXECUTOR_LABELS[name], "kind": adapter.kind,
                    "available": bool(health.get("available", True)) and credential_ready,
                    "models": adapter.models(),
                    "error": health.get("error") if credential_ready else "尚未配置 CLI 登录或 API Key。",
                })
            except Exception as exc:
                result.append({
                    "id": name, "name": EXECUTOR_LABELS[name], "kind": adapter.kind,
                    "available": False, "models": [], "error": compact_error(exc, 300),
                })
        return result

    def executor_settings(self):
        runs = self.global_agent_gate.snapshot()
        tenant_runs = self.agent_gate.snapshot()
        return {
            "planner": self.role_settings()["planner"],
            "preferences": self.agent_preferences(),
            "executors": self.list_executors(),
            "globalConcurrency": runs["limit"],
            "maxGlobalConcurrency": MAX_GLOBAL_CONCURRENCY,
            "runs": runs,
            "tenantRuns": tenant_runs,
            "orchestrationSkill": self.skill_settings(include_content=False),
        }

    def update_executor_settings(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        current = self.agent_preferences()
        updated = {}
        for role in ("planner", "worker", "verifier"):
            updated[role] = self._validate_role_preference(
                role, payload.get(role, current[role])
            )
        self.store.set_setting("agent.preferences", updated)
        for role, agent_id in self.store.get_setting('agent.library.defaults', {}).items():
            definition = self.agent_library.get(agent_id)
            if not definition['archived']:
                self.agent_library.save({**definition, **updated[role]}, agent_id)
        return self.executor_settings()

    def role_settings(self, include_skill=False):
        preferences = self.agent_preferences()
        planner_instance = self._planner_for_role(preferences["planner"])
        planner = dict(planner_instance.settings())
        backend = planner.get("backend") or preferences["planner"]["executor"]
        names = {"codex": "Codex Planner", "claude": "Claude Planner"}
        result = {
            "planner": {
                **planner,
                "name": names.get(backend, "Planner"),
                "executor": backend,
                "model": preferences["planner"]["model"],
                "reasoningEffort": preferences["planner"]["reasoningEffort"],
                "readOnly": True,
            },
            "worker": {
                **preferences["worker"],
                "name": EXECUTOR_LABELS[preferences["worker"]["executor"]],
            },
            "verifier": {
                **preferences["verifier"],
                "name": EXECUTOR_LABELS[preferences["verifier"]["executor"]],
            },
        }
        if include_skill:
            skill = self.skill_settings()
            result["skill"] = {
                "name": skill["name"], "sha256": skill["sha256"],
                "content": skill["content"], "source": skill["source"],
                "customized": skill["customized"],
            }
        return result

    def list_repositories(self):
        return [self._repository_status(item) for item in self.store.list_repositories()]

    def repository_management(self):
        return {
            "mode": "managed",
            "acceptsPath": False,
            "acceptsCloneUrl": True,
        }

    def _repository_status(self, repository):
        result = dict(repository)
        try:
            resolved = self._resolve_workspace(repository["path"])
            if str(resolved) != repository["path"]:
                raise ValueError("路径不再指向已登记的 Git 根目录。")
            head = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=resolved, check=False
            )
            has_commit = head.returncode == 0 and bool(head.stdout.strip())
            result.update({
                "available": True,
                "has_commit": has_commit,
                "initializable": not has_commit,
                "error": None if has_commit else "仓库还没有初始提交。",
            })
        except (OSError, ValueError, RuntimeError) as exc:
            path = Path(repository["path"]).expanduser()
            result.update({
                "available": False,
                "has_commit": False,
                "initializable": path.is_dir(),
                "error": compact_error(exc, 240),
            })
        try:
            result["path"] = str(Path(repository["path"]).resolve().relative_to(self.repository_root))
        except ValueError:
            result["path"] = ""
        origin = None
        if result.get("available"):
            origin = self._git(
                ["remote", "get-url", "origin"], cwd=repository["path"], check=False
            )
        result["source"] = (
            "clone" if origin and origin.returncode == 0 and origin.stdout.strip()
            else "created"
        )
        result["managed"] = True
        try:
            self._managed_repository_path(repository)
            result["deletable"] = True
        except (OSError, ValueError):
            result["deletable"] = False
            result["initializable"] = False
        return result

    def _managed_repository_path(self, repository):
        stored = Path(repository["path"])
        if stored.is_symlink():
            raise ValueError("托管仓库目录不能是符号链接。")
        candidate = stored.resolve()
        root = self.repository_root.resolve()
        if candidate.parent != root or candidate.name != repository["id"]:
            raise ValueError("仓库目录不是 FastLab 生成的托管目录。")
        if not candidate.is_dir():
            raise ValueError("托管仓库目录不存在。")
        return candidate

    @staticmethod
    def _validate_repository_alias(alias):
        alias = str(alias or "").strip()
        if not re.match(r"^[a-zA-Z0-9._-]{1,40}$", alias):
            raise ValueError("仓库名称只能包含字母、数字、点、短横线和下划线。")
        return alias

    @staticmethod
    def _validate_clone_url(value):
        clone_url = str(value or "").strip()
        if not clone_url:
            return ""
        if len(clone_url) > 2048 or any(char in clone_url for char in "\r\n\x00"):
            raise ValueError("Git URL 格式无效。")
        try:
            parsed = urlsplit(clone_url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Git URL 格式无效。") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.strip("/")
        ):
            raise ValueError("Git URL 只允许不含凭证、查询参数或片段的 HTTPS 地址。")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Git URL 端口无效。")
        return clone_url

    def provision_repository(self, alias, clone_url="", is_default=False):
        alias = self._validate_repository_alias(alias)
        clone_url = self._validate_clone_url(clone_url)
        with self.repository_lock:
            if self.store.find_repository(alias):
                raise ValueError("仓库名称已经存在。")
            repository_id = uuid.uuid4().hex
            candidate = (self.repository_root / repository_id).resolve()
            try:
                candidate.relative_to(self.repository_root)
            except ValueError as exc:
                raise ValueError("仓库目录越出当前用户空间。") from exc
            try:
                if clone_url:
                    try:
                        self._git(
                            [
                                "-c", "protocol.allow=never",
                                "-c", "protocol.https.allow=always",
                                "clone", "--origin", "origin", "--", clone_url,
                                str(candidate),
                            ],
                            cwd=self.repository_root,
                            timeout=300,
                        )
                    except RuntimeError as exc:
                        raise ValueError(
                            "Git 仓库克隆失败：%s" % compact_error(exc, 500)
                        ) from exc
                    os.chmod(candidate, 0o700)
                    resolved = self._resolve_workspace(candidate)
                    head = self._git(
                        ["rev-parse", "--verify", "HEAD"], cwd=resolved, check=False
                    )
                    if head.returncode != 0 or not head.stdout.strip():
                        self._create_initial_snapshot(resolved, initialize_git=False)
                else:
                    candidate.mkdir(mode=0o700)
                    self._create_initial_snapshot(candidate, initialize_git=True)
                    resolved = self._resolve_workspace(candidate)
                make_default = bool(is_default) or not self.store.list_repositories()
                result = self.store.add_repository(
                    alias, str(resolved), make_default, repository_id=repository_id
                )
            except Exception:
                if candidate.parent == self.repository_root and candidate.name == repository_id:
                    shutil.rmtree(candidate, ignore_errors=True)
                raise
            if result["is_default"]:
                self.default_repository_id = result["id"]
                self.workspace = Path(result["path"])
            return {**self._repository_status(result), "provisioned": True}

    def _create_initial_snapshot(self, workspace, initialize_git):
        workspace = Path(workspace).resolve()
        try:
            if initialize_git:
                self._git(["init"], cwd=workspace)
            git_dir = self._git(
                ["rev-parse", "--git-dir"], cwd=workspace
            ).stdout.strip()
            git_dir = Path(git_dir)
            if not git_dir.is_absolute():
                git_dir = workspace / git_dir
            exclude = git_dir.resolve() / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if ".fastlab/" not in {line.strip() for line in existing.splitlines()}:
                separator = "" if not existing or existing.endswith("\n") else "\n"
                with exclude.open("a", encoding="utf-8") as stream:
                    stream.write(separator + ".fastlab/\n")
            self._git(["add", "--all", "--", "."], cwd=workspace)
            self._git(
                [
                    "-c", "user.name=FastLab",
                    "-c", "user.email=fastlab@local",
                    "commit", "--allow-empty", "-m", "Initial snapshot by FastLab",
                ],
                cwd=workspace,
            )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                "Git 初始化未完成，请检查目录权限或文件后重试：%s"
                % compact_error(exc, 500)
            ) from exc

    def initialize_repository(self, repository_id):
        with self.repository_lock:
            repository = self.store.get_repository(repository_id)
            if not repository:
                raise KeyError("找不到仓库。")
            resolved = self._managed_repository_path(repository)
            top = self._git(["rev-parse", "--show-toplevel"], cwd=resolved, check=False)
            own_repository = top.returncode == 0 and Path(top.stdout.strip()).resolve() == resolved
            head = self._git(["rev-parse", "--verify", "HEAD"], cwd=resolved, check=False)
            initialized = not own_repository or head.returncode != 0
            if initialized:
                self._create_initial_snapshot(resolved, initialize_git=not own_repository)
            return {**self._repository_status(repository), "initialized": initialized}

    def update_repository(self, repository_id, alias, is_default=False):
        alias = self._validate_repository_alias(alias)
        current = self.store.get_repository(repository_id)
        if not current:
            raise KeyError("找不到仓库。")
        resolved = self._resolve_workspace(current["path"])
        result = self.store.update_repository(
            repository_id, alias, str(resolved), bool(is_default) or current["is_default"]
        )
        if result["is_default"]:
            self.default_repository_id = result["id"]
            self.workspace = Path(result["path"])
        return self._repository_status(result)

    def delete_managed_repository(self, repository_id):
        with self.repository_lock:
            repository = self.store.get_repository(repository_id)
            if not repository:
                raise KeyError("找不到仓库。")
            candidate = self._managed_repository_path(repository)
            if self.store.repository_has_active_tasks(repository_id):
                raise ValueError("该仓库仍有未结束任务，不能删除。")
            staging = self.repository_root / (
                ".deleting-%s-%s" % (repository_id, uuid.uuid4().hex)
            )
            candidate.rename(staging)
            try:
                self.store.delete_repository(repository_id)
            except Exception:
                if staging.exists() and not candidate.exists():
                    staging.rename(candidate)
                raise
            try:
                shutil.rmtree(staging)
            except Exception as exc:
                if staging.exists() and not candidate.exists():
                    staging.rename(candidate)
                try:
                    self.store.add_repository(
                        repository["alias"], repository["path"],
                        repository["is_default"], repository_id=repository_id,
                    )
                    self._sync_default_repository()
                except Exception as restore_exc:
                    raise RuntimeError(
                        "仓库删除失败且登记恢复失败，请联系管理员：%s"
                        % compact_error(restore_exc, 300)
                    ) from exc
                raise RuntimeError("仓库文件删除失败，已恢复仓库登记。") from exc
            self._sync_default_repository()
            return {
                "ok": True,
                "deletedFiles": True,
                "repositoryId": self.default_repository_id,
                "workspace": str(self.workspace) if self.workspace else None,
            }

    def rerun_task(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] not in {"completed", "needs_attention", "failed", "cancelled"}:
                raise ValueError("只有已结束的任务可以重新运行。")
            return self.create_task(
                task["title"],
                task["goal"],
                task["constraints_text"],
                task["max_concurrency"],
                repository_id=task.get("repository_id"),
                working_subdir=task.get("working_subdir") or "",
            )

    def continue_task(self, task_id, message):
        message = str(message or "").strip()
        if not message:
            raise ValueError("继续修改的追加要求不能为空。")
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] != "completed" or not task.get("delivered_commit"):
                raise ValueError(
                    "只有已经成功交付的任务可以继续修改；未交付任务请使用“追加”或“重试”。"
                )
            workspace = self._resolve_workspace(task["workspace"])
            current_branch = self._git(
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=workspace,
                check=False,
            ).stdout.strip()
            if not current_branch or current_branch != task.get("base_branch"):
                raise ValueError(
                    "目标目录必须位于原任务交付分支 `%s`，当前为 `%s`。"
                    % (task.get("base_branch") or "未知", current_branch or "分离 HEAD")
                )
            current_head = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=workspace
            ).stdout.strip()
            contains_delivery = self._git(
                ["merge-base", "--is-ancestor", task["delivered_commit"], current_head],
                cwd=workspace,
                check=False,
            )
            if contains_delivery.returncode:
                raise ValueError("目标分支已不再包含原任务的交付结果，不能创建继续任务。")
            continued_goal = (
                "原任务目标：\n%s\n\n本次继续修改：\n%s"
                % (task["goal"], message)
            )
            return self.create_task(
                "继续：%s" % task["title"],
                continued_goal,
                task["constraints_text"],
                task["max_concurrency"],
                repository_id=task.get("repository_id"),
                working_subdir=task.get("working_subdir") or "",
                parent_task_id=task_id,
                base_branch=current_branch,
                base_commit=current_head,
            )

    def clear_task_logs(self, task_id):
        with self._task_lock(task_id):
            self.require_task(task_id)
            self.store.clear_task_events(task_id)
        return {"ok": True, "taskId": task_id}

    def _remove_task_data_tree(self, category, task_id):
        parent = (self.data_dir / category).resolve()
        candidate = self.data_dir / category / task_id
        if candidate.is_symlink():
            candidate.unlink()
            return
        if not candidate.exists():
            return
        resolved = candidate.resolve()
        try:
            resolved.relative_to(parent)
        except ValueError as exc:
            raise RuntimeError("拒绝删除 FastLab 数据目录之外的路径。") from exc
        shutil.rmtree(resolved)

    def _cleanup_task_git(self, task_id):
        task = self.require_task(task_id)
        if task.get("cleaned_at"):
            return []
        root = (self.data_dir / "worktrees" / task_id).resolve()
        paths = [task.get("integration_worktree")]
        paths.extend(item.get("worktree") for item in task["subtasks"])
        branches = [task.get("integration_branch")]
        branches.extend(item.get("branch") for item in task["subtasks"])
        if not any(paths) and not any(branches):
            self.store.update_task(task_id, cleaned_at=utc_now())
            return []

        workspace = self._resolve_workspace(task["workspace"])
        for raw_path in dict.fromkeys(value for value in paths if value):
            path = Path(raw_path)
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise RuntimeError(
                    "拒绝清理任务数据目录之外的 Worktree：%s" % path
                ) from exc
            self._git(
                ["worktree", "remove", "--force", str(path)],
                cwd=workspace,
                check=False,
            )

        self._remove_task_data_tree("worktrees", task_id)
        self._git(
            ["worktree", "prune", "--expire", "now"],
            cwd=workspace,
            check=False,
        )

        prefix = "fastlab/task-%s-" % task_id[:8]
        deleted = []
        for branch in dict.fromkeys(value for value in branches if value):
            if not branch.startswith(prefix):
                raise RuntimeError("拒绝删除不属于当前任务的分支：%s" % branch)
            exists = self._git(
                ["show-ref", "--verify", "--quiet", "refs/heads/%s" % branch],
                cwd=workspace,
                check=False,
            )
            if exists.returncode:
                continue
            result = self._git(
                ["branch", "-D", "--", branch], cwd=workspace, check=False
            )
            if result.returncode:
                raise RuntimeError(
                    "无法删除临时分支 `%s`：%s"
                    % (branch, compact_error(result.stdout))
                )
            deleted.append(branch)

        for item in task["subtasks"]:
            self.store.update_subtask(item["id"], branch=None, worktree=None)
        self.store.update_task(
            task_id,
            integration_worktree=None,
            cleaned_at=utc_now(),
        )
        return deleted

    def delete_task(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] in {"planning", "running", "verifying"}:
                raise ValueError("任务仍在运行，请先停止任务。")
            deleted_branches = self._cleanup_task_git(task_id)
            self.store.delete_task(task_id)
            self._remove_task_data_tree("tasks", task_id)

        with self.task_locks_lock:
            self.task_locks.pop(task_id, None)
        return {
            "ok": True,
            "taskId": task_id,
            "branchesPreserved": False,
            "deletedBranches": deleted_branches,
        }

    def rename_task(self, task_id, title):
        title = str(title or "").strip()
        if not title:
            raise ValueError("任务名称不能为空。")
        if len(title) > 80:
            raise ValueError("任务名称不能超过 80 个字符。")
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["title"] == title:
                return self.task_payload(task_id)
            self.store.update_task(task_id, title=title)
            self.store.add_event(task_id, "task.renamed", "任务已重命名为：%s" % title)
            self._write_docs_unlocked(task_id)
            return self.task_payload(task_id)

    def health_payload(self):
        stored_default = (
            self.store.get_repository(self.default_repository_id)
            if self.default_repository_id else None
        )
        default = self._repository_status(stored_default) if stored_default else None
        ready = bool(default and default["available"])
        executors = self.list_executors()
        planner_setting = self.agent_preferences()["planner"]
        planner_health = self._planner_for_role(planner_setting).health()
        return {
            "ok": True,
            "workspace": str(self.workspace) if self.workspace else None,
            "repositoryId": self.default_repository_id,
            "setupRequired": not ready,
            "python": sys.version.split()[0],
            "pythonSupported": sys.version_info >= (3, 10),
            "planner": planner_health,
            "agentRuns": {
                **self.agent_gate.snapshot(),
                "timeoutSeconds": self.agent_timeout,
            },
            "executors": executors,
            "orchestration": {
                "ready": self.user_skill_path.is_file(),
                "skill": "orchestration",
                **self.skill_settings(include_content=False),
            },
        }

    def create_task(self, title, goal, constraints_text, max_concurrency,
                    repository_id=None, working_subdir="", parent_task_id=None,
                    base_branch=None, base_commit=None, agents=None):
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("请填写任务目标。")
        if len(goal) > 20000 or len(str(constraints_text or "")) > 10000:
            raise ValueError("任务内容过长。")
        try:
            concurrency = int(max_concurrency or DEFAULT_GLOBAL_CONCURRENCY)
        except (TypeError, ValueError) as exc:
            raise ValueError("任务并发必须是整数。") from exc
        if not 1 <= concurrency <= MAX_GLOBAL_CONCURRENCY:
            raise ValueError("任务并发必须在 1–%s 之间。" % MAX_GLOBAL_CONCURRENCY)
        display_title = str(title or "").strip() or goal.splitlines()[0][:80]
        repository = self._resolve_repository(repository_id)
        task_workspace = Path(repository["path"])
        normalized_subdir = self._normalize_working_subdir(task_workspace, working_subdir)
        role_settings = self.role_settings(include_skill=True)
        selections = agents if agents is not None else {}
        if not isinstance(selections, dict) or set(selections) - {'planner', 'worker', 'verifier'}:
            raise ValueError('Agent 选择无效。')
        defaults = self.store.get_setting('agent.library.defaults', {})
        for role in ('planner', 'worker', 'verifier'):
            selected = selections.get(role, defaults.get(role))
            if selected:
                # An archived default falls back to legacy settings; explicit selections fail.
                try:
                    definition = self.agent_library.get(selected, active=True)
                except KeyError:
                    if role in selections:
                        raise
                    continue
                if role not in definition['roles']:
                    raise ValueError('Agent 不适用于所选角色。')
                role_settings[role].update({k: definition[k] for k in ('executor', 'model', 'reasoningEffort')})
                role_settings[role]['agent'] = definition
                if role == 'planner':
                    role_settings[role]['backend'] = definition['executor']
        role_settings['agentCatalog'] = self.agent_library.list()
        planner_instance = self._planner_for_role(
            role_settings["planner"], role_settings["skill"]["content"]
        )
        planner_health = planner_instance.health()
        if not planner_health.get("available", True):
            raise ValueError("Planner 当前不可用：%s" % (
                planner_health.get("error") or "请检查环境变量和本地 CLI"
            ))
        task_id = self.store.create_task(
            display_title,
            goal,
            str(constraints_text or "").strip(),
            str(task_workspace),
            concurrency,
            repository_id=repository["id"],
            role_settings=role_settings,
            working_subdir=normalized_subdir,
            parent_task_id=parent_task_id,
            base_branch=base_branch,
            base_commit=base_commit,
        )
        self.store.add_event(task_id, "task.created", "任务已创建，正在生成执行计划。")
        self.write_docs(task_id)
        self._spawn(self._plan_task, task_id, "")
        return self.task_payload(task_id)

    def _resolve_repository(self, repository_id=None):
        if repository_id:
            repository = self.store.get_repository(str(repository_id))
        else:
            repository = self.store.get_repository(self.default_repository_id)
        if not repository:
            raise ValueError("只能选择设置页中已登记的仓库。")
        status = self._repository_status(repository)
        if not status["available"]:
            raise ValueError("仓库当前不可用：%s" % status["error"])
        current = str(self._resolve_workspace(repository["path"]))
        if current != repository["path"]:
            repository = self.store.update_repository(
                repository["id"], repository["alias"], current, repository["is_default"]
            )
        return repository

    @staticmethod
    def _normalize_working_subdir(repository_root, value):
        raw = str(value or "").strip().replace("\\", "/")
        if raw in {"", "."}:
            return ""
        windows_path = PureWindowsPath(raw)
        posix_path = PurePosixPath(raw)
        if windows_path.drive or windows_path.is_absolute() or posix_path.is_absolute():
            raise ValueError("工作目录必须是仓库内的相对目录。")
        parts = [part for part in posix_path.parts if part not in {"", "."}]
        if not parts or ".." in parts:
            raise ValueError("工作目录不能包含 ..。")
        root = Path(repository_root).resolve()
        candidate = root.joinpath(*parts).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("工作目录不能通过符号链接离开仓库。") from exc
        if not candidate.is_dir():
            raise ValueError("工作目录不是文件夹：%s" % "/".join(parts))
        return "/".join(parts)

    def replan_task(self, task_id, feedback):
        with self._task_lock(task_id):
            return self._replan_task_locked(task_id, feedback)

    def _replan_task_locked(self, task_id, feedback):
        task = self.require_task(task_id)
        if task["status"] not in {"awaiting_approval", "failed", "needs_attention"}:
            raise ValueError("当前状态不能重新规划。")
        if any(item["attempt"] for item in task["subtasks"]):
            raise ValueError("已经执行过的任务不能重新规划，请新建任务。")
        planner_setting = (task.get("role_settings") or {}).get("planner") or {}
        skill_content = ((task.get("role_settings") or {}).get("skill") or {}).get("content")
        planner_health = self._planner_for_role(planner_setting, skill_content).health()
        if not planner_health.get("available", True):
            raise ValueError("Planner 当前不可用：%s" % (
                planner_health.get("error") or "请检查环境变量和本地 CLI"
            ))
        self.store.update_task(task_id, cancel_requested=0)
        self._transition_task(
            task_id, "planning", "task.replan", "正在根据反馈重新规划。"
        )
        self._spawn(self._plan_task, task_id, str(feedback or "").strip())
        return self.task_payload(task_id)

    def _task_lock(self, task_id):
        with self.task_locks_lock:
            return self.task_locks.setdefault(task_id, threading.RLock())

    def _transition_task(self, task_id, status, kind, message, error=None, payload=None,
                         subtask_id=None):
        with self._task_lock(task_id):
            self.store.transition_task(
                task_id, status, kind, message, error=error, payload=payload,
                subtask_id=subtask_id,
            )
            self._write_docs_unlocked(task_id)

    def _update_subtask_state(self, task_id, subtask_id, *, kind=None, message=None,
                              notify=False, expected_dispatch_id=None, payload=None,
                              **values):
        """Publish a subtask state change as one task-visible operation."""
        with self._task_lock(task_id):
            if expected_dispatch_id:
                updated = self.store.update_subtask_for_dispatch(
                    subtask_id, expected_dispatch_id, **values
                )
                if not updated:
                    return False
            else:
                self.store.update_subtask(subtask_id, **values)
            event_payload = dict(payload or {})
            dispatch_id = expected_dispatch_id or values.get("dispatch_id")
            if dispatch_id:
                event_payload.update({
                    "task_id": task_id,
                    "subtask_id": subtask_id,
                    "dispatch_id": dispatch_id,
                })
            if kind and message:
                self.store.add_event(
                    task_id, kind, message, subtask_id=subtask_id,
                    payload=event_payload or None,
                )
            self._write_docs_unlocked(task_id)
            return True

    @staticmethod
    def _subtask_run_key(subtask_id, dispatch_id):
        task_id, separator, plan_key = str(subtask_id).partition(":")
        if separator:
            return "subtask-%s-dispatch-%s:%s" % (task_id, dispatch_id, plan_key)
        return "subtask-%s-dispatch-%s" % (subtask_id, dispatch_id)

    def _dispatch_is_current(self, task_id, subtask_id, dispatch_id):
        task = self.require_task(task_id)
        return any(
            item["id"] == subtask_id
            and item.get("dispatch_id") == dispatch_id
            and item["status"] == "running"
            for item in task["subtasks"]
        )

    def _rebuild_documents(self):
        for task in self.store.list_tasks():
            self.write_docs(task["id"])

    def _timeout_guard(self, key, runner):
        return self.runtime.timeout_guard(key, runner)

    def _timeout_error(self, role):
        return self.runtime.timeout_error(role)

    @staticmethod
    def _finish_timeout_guard(timer):
        return AgentRuntime.finish_timeout_guard(timer)

    def _run_agent(self, task_id, key, cwd, prompt, sandbox, executor, capability, **options):
        return self.runtime.run_agent(
            task_id, key, cwd, prompt, sandbox, executor, capability, **options
        )

    def _run_planner(self, task_id, key, prompt, context, planner=None):
        return self.runtime.run_planner(task_id, key, prompt, context, planner=planner)

    def _spawn(self, target, *arguments):
        def wrapped():
            try:
                target(*arguments)
            finally:
                with self.thread_lock:
                    self.background_threads.discard(threading.current_thread())

        with self.thread_lock:
            if self.shutting_down:
                return None
            thread = threading.Thread(target=wrapped, daemon=True)
            self.background_threads.add(thread)
        thread.start()
        return thread

    def _plan_task(self, task_id, feedback):
        task = self.require_task(task_id)
        roles = task.get("role_settings") or {}
        planner_setting = roles.get("planner") or {}
        worker_setting = roles.get("worker") or {}
        skill_content = (roles.get("skill") or {}).get("content")
        planner_instance = self._planner_for_role(planner_setting, skill_content)
        available_executors = {
            item["id"] for item in self.list_executors() if item["available"]
        }
        prompt = self.planner_prompt(task, feedback, available_executors)
        catalog = [item for item in roles.get('agentCatalog', [])
                   if item['executor'] in available_executors and 'worker' in item['roles']]
        prompt += "\n可选 Agent 定义（使用 agentId 引用）：\n" + json.dumps(catalog, ensure_ascii=False)
        session = {"id": None}

        def event(kind, message, payload):
            self.store.add_event(task_id, kind, message, payload=payload)
            if isinstance(payload, dict):
                session["id"] = (
                    payload.get("sessionId") or payload.get("thread_id") or session["id"]
                )

        try:
            workspace = self._resolve_workspace(task["workspace"])
            plan = self._run_planner(
                task_id,
                "task-%s-planner" % task_id,
                prompt,
                {
                    "key": "task-%s-planner" % task_id,
                    "cwd": workspace,
                    "repositoryPath": str(workspace),
                    "workingSubdir": task.get("working_subdir") or "",
                    "availableExecutors": sorted(available_executors),
                    "feedback": feedback or "",
                    "on_event": event,
                },
                planner=planner_instance,
            )
            self.store.update_task(task_id, planner_thread_id=session["id"])
            if self.require_task(task_id)["cancel_requested"]:
                self._transition_task(
                    task_id, "cancelled", "task.cancelled", "任务已取消。"
                )
                return
            plan = self.agent_library.resolve_plan(plan, catalog, worker_setting.get('agent', {}))
            plan = self.validate_plan(plan, available_executors=available_executors)
            with self._task_lock(task_id):
                self.store.replace_subtasks(task_id, plan["subtasks"])
                for item in self.store.get_task(task_id, include_events=False)["subtasks"]:
                    executor = next(
                        value["executor"] for value in plan["subtasks"]
                        if value["key"] == item["plan_key"]
                    )
                    planned = next(value for value in plan['subtasks'] if value['key'] == item['plan_key'])
                    definition = planned.get('agentSnapshot') or {}
                    self._assign_subtask_executor(
                        item,
                        executor,
                        definition.get("model") if definition else (worker_setting.get("model")
                        if executor == worker_setting.get("executor") else None),
                        definition.get("reasoningEffort") if definition else (worker_setting.get("reasoningEffort")
                        if executor == worker_setting.get("executor") else None),
                    )
                    if definition:
                        current = self.require_subtask(item['id'])[1]
                        snapshot = {**current['executor_snapshot'], 'agent': definition}
                        self.store.update_subtask(item['id'], executor_snapshot_json=json.dumps(snapshot, ensure_ascii=False))
                values = {
                    "plan_json": json.dumps(plan, ensure_ascii=False),
                    "error": None,
                }
                if not task["title"].strip():
                    values["title"] = plan["title"]
                self.store.update_task(task_id, **values)
                self._transition_task(
                    task_id, "awaiting_approval", "task.planned",
                    "执行计划和执行器分配已生成，等待用户确认并选择验收执行器。"
                )
        except Exception as exc:
            current = self.store.get_task(task_id)
            if current and current["cancel_requested"]:
                self._transition_task(
                    task_id, "cancelled", "task.cancelled", "任务已取消。"
                )
            else:
                self._transition_task(
                    task_id, "failed", "task.error", "规划失败：%s" % compact_error(exc),
                    error=compact_error(exc),
                )

    @staticmethod
    @staticmethod
    def _schema_for_adapter(adapter, path):
        return PlanningService.schema_for_adapter(adapter, path)

    @staticmethod
    def planner_prompt(task, feedback, available_executors=None):
        return PlanningService.planner_prompt(task, feedback, available_executors)

    @staticmethod
    def parse_json_result(value):
        return PlanningService.parse_json_result(value)

    @staticmethod
    def validate_plan(plan, available_executors=None):
        return PlanningService.validate_plan(plan, available_executors)

    def start_task(self, task_id):
        with self._task_lock(task_id):
            return self._start_task_locked(task_id)

    def _start_task_locked(self, task_id):
        task = self.require_task(task_id)
        if task["status"] != "awaiting_approval":
            raise ValueError("任务尚未准备好或已经开始。")
        for subtask in task["subtasks"]:
            executor = subtask.get("executor")
            if not executor:
                raise ValueError("子任务 %s 尚未分配执行器。" % subtask["plan_key"])
            self._executor_adapter(executor, WRITE_CAPABILITY)
        verifier = task["role_settings"].get("verifier") or {}
        verifier_executor = verifier.get("executor")
        if not verifier_executor:
            raise ValueError("请先指定最终验收执行器。")
        self._executor_adapter(verifier_executor, VERIFY_CAPABILITY)
        workspace = self._resolve_workspace(task["workspace"])
        self._normalize_working_subdir(workspace, task.get("working_subdir"))
        head = self._git(
            ["rev-parse", "--verify", "HEAD"], cwd=workspace, check=False
        )
        current_commit = head.stdout.strip()
        if head.returncode or not current_commit:
            raise ValueError(
                "目标仓库还没有任何提交。请先创建初始提交，再开始执行。"
            )
        dirty = self._git(
            ["status", "--porcelain", "--untracked-files=all"], cwd=workspace
        ).stdout.strip()
        if dirty:
            raise ValueError("目标仓库存在未提交改动。请先提交或暂存，再开始执行。")
        current_branch = self._git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workspace, check=False
        ).stdout.strip()
        if task.get("parent_task_id") and task.get("base_commit"):
            if current_branch != task.get("base_branch"):
                raise ValueError(
                    "继续任务创建后目标分支发生变化；请切回 `%s` 或重新创建继续任务。"
                    % task.get("base_branch")
                )
            if current_commit != task.get("base_commit"):
                raise ValueError(
                    "继续任务创建后目标分支 HEAD 已变化；请重新创建继续任务。"
                )
            base_branch = task["base_branch"]
            base_commit = task["base_commit"]
        else:
            base_branch = current_branch or "(detached HEAD)"
            base_commit = current_commit
        short_id = task_id[:8]
        integration_branch = "fastlab/task-%s-integration" % short_id
        integration_path = self.data_dir / "worktrees" / task_id / "integration"
        integration_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            ["worktree", "add", "-b", integration_branch, str(integration_path), base_commit],
            cwd=workspace,
        )
        self.store.update_task(
            task_id,
            base_branch=base_branch,
            base_commit=base_commit,
            integration_branch=integration_branch,
            integration_worktree=str(integration_path),
            cancel_requested=0,
            error=None,
        )
        self._transition_task(
            task_id, "running", "task.started", "任务已确认，开始调度子任务。"
        )
        self._ensure_scheduler(task_id)
        return self.task_payload(task_id)

    def _ensure_scheduler(self, task_id):
        with self.scheduler_lock:
            current = self.schedulers.get(task_id)
            if current and current.is_alive():
                return
            thread = self._spawn(self._scheduler, task_id)
            if thread:
                self.schedulers[task_id] = thread

    def _scheduler(self, task_id):
        try:
            while True:
                task = self.require_task(task_id)
                if task["cancel_requested"]:
                    self._finish_cancel(task)
                    return
                subtasks = task["subtasks"]
                running = [item for item in subtasks if item["status"] == "running"]
                if subtasks and all(item["status"] == "succeeded" for item in subtasks):
                    self._verify_task(task_id)
                    return
                status_by_key = {item["plan_key"]: item["status"] for item in subtasks}
                ready = [
                    item
                    for item in subtasks
                    if item["status"] == "pending"
                    and all(status_by_key.get(dep) == "succeeded" for dep in item["dependencies"])
                ]
                available = max(0, int(task["max_concurrency"]) - len(running))
                launched = 0
                for item in ready[:available]:
                    try:
                        prepared = self._prepare_subtask(task, item)
                        dispatch_id = uuid.uuid4().hex
                        self._update_subtask_state(
                            task_id, item["id"],
                            status="running",
                            attempt=int(item["attempt"]) + 1,
                            dispatch_id=dispatch_id,
                            error=None,
                            kind="subtask.started",
                            message="开始执行 %s（Dispatch %s）。"
                            % (item["title"], dispatch_id[:8]),
                        )
                        prepared = next(
                            current for current in self.require_task(task_id)["subtasks"]
                            if current["id"] == item["id"]
                        )
                        self._spawn(
                            self._run_subtask,
                            task_id,
                            prepared,
                            item.get("pending_message"),
                            dispatch_id,
                            bool(item.get("pending_message") and item.get("session_id")),
                        )
                        launched += 1
                    except Exception as exc:
                        self._update_subtask_state(
                            task_id, item["id"], status="failed", error=compact_error(exc),
                            kind="subtask.error",
                            message="准备子任务失败：%s" % compact_error(exc),
                        )
                if launched:
                    time.sleep(0.25)
                    continue
                if not running and not ready:
                    failed_keys = {
                        item["plan_key"]
                        for item in subtasks
                        if item["status"] in {"failed", "blocked", "cancelled"}
                    }
                    for item in subtasks:
                        if item["status"] == "pending" and any(
                            dep in failed_keys for dep in item["dependencies"]
                        ):
                            self._update_subtask_state(
                                task_id, item["id"], status="blocked",
                                error="依赖任务尚未成功。"
                            )
                    self._transition_task(
                        task_id,
                        "needs_attention",
                        "task.attention",
                        "执行已暂停，需要处理失败或阻塞的子任务。",
                        error="有子任务失败或被依赖阻塞，请检查后重试。",
                    )
                    return
                time.sleep(0.4)
        except Exception as exc:
            self._transition_task(
                task_id, "failed", "task.error", "调度失败：%s" % compact_error(exc),
                error=compact_error(exc),
            )

    def _prepare_subtask(self, task, subtask):
        if subtask.get("worktree") and Path(subtask["worktree"]).exists():
            return self.store.get_task(task["id"])["subtasks"][subtask["ordinal"] - 1]
        branch = "fastlab/task-%s-sub-%02d" % (task["id"][:8], subtask["ordinal"])
        path = self.data_dir / "worktrees" / task["id"] / ("sub-%02d" % subtask["ordinal"])
        integration_path = Path(task["integration_worktree"])
        base = self._git(["rev-parse", "HEAD"], cwd=integration_path).stdout.strip()
        self._git(
            ["worktree", "add", "-b", branch, str(path), base], cwd=integration_path
        )
        self.store.update_subtask(subtask["id"], branch=branch, worktree=str(path))
        refreshed = self.store.get_task(task["id"])
        return next(item for item in refreshed["subtasks"] if item["id"] == subtask["id"])

    def _run_subtask(self, task_id, subtask, instruction_message, dispatch_id,
                     resume_existing=False):
        task = self.require_task(task_id)
        key = self._subtask_run_key(subtask["id"], dispatch_id)
        if instruction_message and resume_existing:
            prompt = str(instruction_message)
        elif instruction_message:
            prompt = self.worker_revision_prompt(task, subtask, instruction_message)
        else:
            prompt = self.worker_prompt(task, subtask)

        def event(kind, message, payload):
            self.store.add_event(
                task_id,
                kind,
                message,
                subtask["id"],
                {
                    "task_id": task_id,
                    "subtask_id": subtask["id"],
                    "dispatch_id": dispatch_id,
                    "agent_event": payload,
                },
            )

        try:
            snapshot = subtask.get("executor_snapshot") or {}
            executor = subtask.get("executor") or snapshot.get("executor")
            if not executor:
                raise RuntimeError("子任务尚未分配执行器。")
            executor, _ = self._executor_adapter(
                executor, RESUME_CAPABILITY if resume_existing else WRITE_CAPABILITY
            )
            model = subtask.get("model") or snapshot.get("model") or None
            effort = subtask.get("reasoning_effort") or snapshot.get("reasoningEffort") \
                or "high"
            self.store.update_subtask(
                subtask["id"],
                model=model,
                reasoning_effort=effort,
                pending_message=None,
            )
            code, session_id, final = self._run_agent(
                task_id,
                key,
                Path(subtask["worktree"]),
                prompt,
                "workspace_write",
                executor=executor,
                capability=RESUME_CAPABILITY if resume_existing else WRITE_CAPABILITY,
                resume_session=subtask.get("session_id") if resume_existing else None,
                on_event=event,
                model=model,
                effort=effort,
                skills=[],
            )
            if not self._update_subtask_state(
                task_id,
                subtask["id"],
                expected_dispatch_id=dispatch_id,
                session_id=session_id,
                exit_code=code,
            ):
                self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
                return
            task = self.require_task(task_id)
            if task["cancel_requested"]:
                updated = self._update_subtask_state(
                    task_id, subtask["id"], status="cancelled", error="用户取消任务。",
                    expected_dispatch_id=dispatch_id,
                )
                if not updated:
                    self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
                return
            if code:
                detail = compact_error(final, 800)
                raise RuntimeError(
                    "实现 Agent 退出，代码 %s。%s"
                    % (code, (" 原因：%s" % detail) if detail else "")
                )
            self._enforce_working_subdir(task, Path(subtask["worktree"]))
            self._commit_and_merge(task_id, subtask["id"], dispatch_id)
            updated = self._update_subtask_state(
                task_id, subtask["id"], status="succeeded", error=None,
                kind="subtask.completed",
                message="%s 已完成并合入任务分支（Dispatch %s）。"
                % (subtask["title"], dispatch_id[:8]),
                notify=True,
                expected_dispatch_id=dispatch_id,
            )
            if not updated:
                self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
        except Exception as exc:
            current = self.require_task(task_id)
            if current["cancel_requested"]:
                updated = self._update_subtask_state(
                    task_id, subtask["id"], status="cancelled", error="用户取消任务。"
                    , expected_dispatch_id=dispatch_id
                )
            else:
                updated = self._update_subtask_state(
                    task_id, subtask["id"], status="failed", error=compact_error(exc),
                    kind="subtask.error",
                    message="%s 执行失败：%s" % (subtask["title"], compact_error(exc)),
                    notify=True,
                    expected_dispatch_id=dispatch_id,
                )
            if not updated:
                self._record_stale_dispatch(task_id, subtask["id"], dispatch_id)
        finally:
            self._ensure_scheduler(task_id)

    def _record_stale_dispatch(self, task_id, subtask_id, dispatch_id):
        self.store.add_event(
            task_id,
            "dispatch.stale",
            "忽略已经失效的 Dispatch %s 返回结果。" % dispatch_id[:8],
            subtask_id=subtask_id,
            payload={
                "task_id": task_id,
                "subtask_id": subtask_id,
                "dispatch_id": dispatch_id,
            },
        )

    @staticmethod
    def worker_prompt(task, subtask):
        acceptance = "\n".join(
            "- [%s] %s" % (item["id"], item["criterion"])
            for item in (task.get("plan") or {}).get("acceptance", [])
        )
        return """你是 FastLab 启动的实现 Agent。只完成分配给你的子任务，不要管理 Git 分支或 worktree，不要修改任务调度数据。你可以读取完整仓库，但只能修改“允许写入范围”内的文件；FastLab 会在提交前检查越界改动。检查现有代码，实施改动并运行与改动相关的测试。结束前确保工作区只包含本子任务需要的改动。

顶层目标：{goal}
允许写入范围：{working_subdir}
子任务 ID：{key}
子任务：{title}
具体要求：
{instructions}

整体验收标准：
{acceptance}
""".format(
            goal=task["goal"],
            working_subdir=(task.get("working_subdir") + "/**") if task.get("working_subdir") else "整个仓库",
            key=subtask["plan_key"],
            title=subtask["title"],
            instructions=subtask["instructions"],
            acceptance=acceptance,
        )

    @classmethod
    def worker_revision_prompt(cls, task, subtask, message):
        return "%s\n\n本轮是对已完成子任务的再次修改。已有实现保留在当前 Worktree 中；请先检查现状，再完成下面的新增要求。不要撤销原有正确成果。\n\n本轮新增要求：\n%s" % (
            cls.worker_prompt(task, subtask), str(message).strip()
        )

    def _enforce_working_subdir(self, task, worktree):
        scope = str(task.get("working_subdir") or "").strip("/")
        if not scope:
            return
        tracked = self._git(
            [
                "diff", "--name-only", "--no-renames", "-z",
                task.get("base_commit") or "HEAD",
            ],
            cwd=worktree,
        ).stdout
        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree
        ).stdout
        changed = sorted({item for item in (tracked + untracked).split("\0") if item})
        prefix = scope + "/"
        outside = [path for path in changed if path != scope and not path.startswith(prefix)]
        if outside:
            shown = ", ".join(outside[:12])
            if len(outside) > 12:
                shown += " 等 %s 个文件" % len(outside)
            raise RuntimeError(
                "检测到工作目录 `%s` 之外的改动，已停止提交与合并：%s" % (scope, shown)
            )

    def _commit_and_merge(self, task_id, subtask_id, dispatch_id):
        # Keep the task lock through commit and merge. A cancelled/retried Dispatch
        # must never commit after a newer Dispatch has taken ownership.
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            subtask = next(
                item for item in task["subtasks"] if item["id"] == subtask_id
            )
            if (
                task["cancel_requested"]
                or subtask.get("dispatch_id") != dispatch_id
                or subtask["status"] != "running"
            ):
                raise AgentRunCancelled("该 Dispatch 已失效，停止提交与合并。")
            worktree = Path(subtask["worktree"])
            changes = self._git(["status", "--porcelain"], cwd=worktree).stdout.strip()
            if changes:
                self._git(["add", "-A"], cwd=worktree)
                self._git(
                    [
                        "-c",
                        "user.name=FastLab",
                        "-c",
                        "user.email=fastlab@local",
                        "commit",
                        "-m",
                        "fastlab: %s" % subtask["title"][:120],
                    ],
                    cwd=worktree,
                )
            with self.merge_lock:
                integration = Path(task["integration_worktree"])
                result = self._git(
                    [
                        "-c",
                        "user.name=FastLab",
                        "-c",
                        "user.email=fastlab@local",
                        "merge",
                        "--no-ff",
                        subtask["branch"],
                        "-m",
                        "fastlab: merge %s" % subtask["plan_key"],
                    ],
                    cwd=integration,
                    check=False,
                )
                if result.returncode:
                    self._git(["merge", "--abort"], cwd=integration, check=False)
                    raise RuntimeError(
                        "合并冲突，子任务分支已保留。\n%s"
                        % compact_error(result.stdout)
                    )

    def _deliver_integration(self, task_id):
        """Safely deliver a verified integration branch to the original worktree.

        The user's worktree is never stashed or overwritten. If its branch advanced
        after the task started, reconcile those commits in the isolated integration
        worktree first and require a new verification pass before delivery.
        """
        task = self.require_task(task_id)
        workspace = self._resolve_workspace(task["workspace"])
        base_branch = str(task.get("base_branch") or "")
        if not base_branch or base_branch == "(detached HEAD)":
            raise RuntimeError("目标仓库没有可交付的当前分支，请先切换到普通分支。")
        integration_branch = str(task.get("integration_branch") or "")
        if not integration_branch:
            raise RuntimeError("任务集成分支不存在。")

        with self.merge_lock:
            dirty = self._git(
                ["status", "--porcelain", "--untracked-files=all"], cwd=workspace
            ).stdout.strip()
            if dirty:
                raise RuntimeError("目标目录存在未提交改动，已停止自动交付。")
            current_branch = self._git(
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=workspace,
                check=False,
            ).stdout.strip()
            if current_branch != base_branch:
                raise RuntimeError(
                    "目标目录当前分支是 `%s`，任务开始时是 `%s`，已停止自动交付。"
                    % (current_branch or "分离 HEAD", base_branch)
                )
            current_commit = self._git(
                ["rev-parse", "--verify", "HEAD"], cwd=workspace
            ).stdout.strip()
            integration_commit = self._git(
                ["rev-parse", "--verify", integration_branch], cwd=workspace
            ).stdout.strip()

            def is_ancestor(ancestor, descendant):
                return self._git(
                    ["merge-base", "--is-ancestor", ancestor, descendant],
                    cwd=workspace,
                    check=False,
                ).returncode == 0

            if current_commit == integration_commit:
                delivered_commit = current_commit
            elif is_ancestor(current_commit, integration_commit):
                # The target has not diverged from the integration result. A
                # non-base target may be left by an earlier reconciliation; do
                # not deliver it until the current integration HEAD was verified.
                verified_commit = str(
                    (task.get("plan") or {}).get("verificationCommit") or ""
                )
                if (
                    current_commit != task.get("base_commit")
                    and verified_commit != integration_commit
                ):
                    raise DeliveryReverificationRequired(
                        "目标分支的新提交已合入任务结果，正在重新验收。"
                    )
                result = self._git(
                    ["merge", "--ff-only", integration_branch],
                    cwd=workspace,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(
                        "无法安全快进到任务集成分支：%s" % compact_error(result.stdout)
                    )
                delivered_commit = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=workspace
                ).stdout.strip()
            else:
                base_commit = str(task.get("base_commit") or "")
                if not base_commit or not is_ancestor(base_commit, current_commit):
                    raise RuntimeError(
                        "目标分支历史已被改写，无法确认安全合并。目标目录未修改；"
                        "请切回任务开始时的分支历史，或使用“继续修改”创建新任务。"
                    )

                integration = Path(str(task.get("integration_worktree") or ""))
                if not integration.is_dir():
                    raise RuntimeError("任务集成 Worktree 不存在，无法同步目标分支。")
                integration_dirty = self._git(
                    ["status", "--porcelain", "--untracked-files=all"], cwd=integration
                ).stdout.strip()
                if integration_dirty:
                    raise RuntimeError("任务集成 Worktree 存在未提交改动，无法安全同步。")
                checked_out_branch = self._git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=integration,
                    check=False,
                ).stdout.strip()
                if checked_out_branch != integration_branch:
                    raise RuntimeError("任务集成 Worktree 未位于预期分支，无法安全同步。")
                integration_head = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=integration
                ).stdout.strip()
                if integration_head != integration_commit:
                    raise RuntimeError("任务集成 Worktree 与集成分支不一致，无法安全同步。")

                if is_ancestor(integration_commit, current_commit):
                    result = self._git(
                        ["merge", "--ff-only", current_commit],
                        cwd=integration,
                        check=False,
                    )
                else:
                    result = self._git(
                        [
                            "-c", "user.name=FastLab",
                            "-c", "user.email=fastlab@local",
                            "merge", "--no-ff", current_commit,
                            "-m", "fastlab: reconcile target before delivery",
                        ],
                        cwd=integration,
                        check=False,
                    )
                if result.returncode:
                    self._git(["merge", "--abort"], cwd=integration, check=False)
                    raise RuntimeError(
                        "目标分支的新提交与任务结果发生冲突，目标目录未修改。"
                        "请处理冲突或使用“继续修改”创建新任务。\n%s"
                        % compact_error(result.stdout)
                    )
                reconciled_commit = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=integration
                ).stdout.strip()
                self.store.add_event(
                    task_id,
                    "task.delivery.reconciled",
                    "目标分支的新提交已安全合入任务集成分支；交付前将重新验收。",
                    payload={
                        "targetCommit": current_commit,
                        "integrationCommit": reconciled_commit,
                    },
                )
                raise DeliveryReverificationRequired(
                    "目标分支的新提交已安全合入任务结果，正在重新验收。"
                )

            if delivered_commit != integration_commit:
                raise RuntimeError("交付后的提交与任务集成分支不一致。")
            self.store.update_task(
                task_id,
                delivered_commit=delivered_commit,
                delivered_at=utc_now(),
                error=None,
            )
            return delivered_commit

    def _queue_delivery_reverification(self, task_id, message):
        """Discard stale evidence and verify the reconciled integration HEAD."""
        task = self.require_task(task_id)
        plan = dict(task.get("plan") or {})
        plan.pop("verification", None)
        plan.pop("verificationCommit", None)
        self.store.update_task(
            task_id,
            plan_json=json.dumps(plan, ensure_ascii=False),
            error=None,
            cancel_requested=0,
        )
        self._transition_task(
            task_id,
            "verifying",
            "task.delivery.reverify",
            str(message),
            error=None,
        )
        self._spawn(self._verify_task, task_id)

    def _verification_workspace(self, task):
        return self.verification._verification_workspace(task)

    def _create_verification_worktree(self, task):
        return self.verification._create_verification_worktree(task)

    def _remove_verification_worktree(self, task, path):
        return self.verification._remove_verification_worktree(task, path)

    def _verify_task(self, task_id):
        return self.verification._verify_task(task_id)

    def verify_task(self, task_id):
        with self._task_lock(task_id):
            return self._verify_task_locked(task_id)

    def _verify_task_locked(self, task_id):
        task = self.require_task(task_id)
        if task["status"] not in {"completed", "needs_attention", "failed"}:
            raise ValueError("当前状态不能重新验收。")
        if not task["subtasks"] or any(item["status"] != "succeeded" for item in task["subtasks"]):
            raise ValueError("只有全部子任务成功后才能重新验收。")
        self._verification_workspace(task)
        self.store.update_task(task_id, error=None, cancel_requested=0)
        self._transition_task(
            task_id, "verifying", "task.verifying", "用户要求重新执行最终验收。"
        )
        self._spawn(self._verify_task, task_id)
        return self.task_payload(task_id)

    def accept_verification(self, task_id, evidence):
        evidence = str(evidence or "").strip()
        if not evidence:
            raise ValueError("请填写你实际完成的人工验收步骤和结果。")
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] != "needs_attention":
                raise ValueError("只有自动验收存在不确定项时才能人工确认。")
            if not task["subtasks"] or any(
                item["status"] != "succeeded" for item in task["subtasks"]
            ):
                raise ValueError("子任务尚未全部成功，不能人工确认验收。")
            plan = dict(task.get("plan") or {})
            verification = dict(plan.get("verification") or {})
            results = [dict(item) for item in verification.get("results", [])]
            if not results or any(item.get("status") == "failed" for item in results):
                raise ValueError("存在明确失败项，不能用人工确认覆盖；请先修复后重试。")
            unclear = [item for item in results if item.get("status") == "unclear"]
            if not unclear:
                raise ValueError("当前验收没有可人工确认的不确定项。")
            for item in unclear:
                item["agentStatus"] = "unclear"
                item["status"] = "passed"
                item["evidence"] = "%s 人工确认：%s" % (
                    str(item.get("evidence") or "").rstrip("。"), evidence
                )
            verification.update({
                "passed": True,
                "summary": "自动验收未发现明确失败；用户已完成人工运行时验收：%s" % evidence,
                "results": results,
                "manualReview": {"evidence": evidence, "confirmedAt": utc_now()},
            })
            plan["verification"] = verification
            self.store.update_task(
                task_id, plan_json=json.dumps(plan, ensure_ascii=False), error=None
            )
            self.store.add_event(
                task_id, "verification.manual",
                "用户已为自动验收的不确定项补充人工证据。",
                payload={"evidence": evidence},
            )
            self._write_docs_unlocked(task_id)
            try:
                return self.deliver_task(task_id)
            except ValueError:
                # The manual evidence remains recorded. A dirty target branch or
                # another delivery guard can be fixed before clicking Apply.
                return self.task_payload(task_id)

    def deliver_task(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            verification = (task.get("plan") or {}).get("verification") or {}
            if not verification.get("passed"):
                raise ValueError("只有最终验收通过的任务可以交付。")
            if not task["subtasks"] or any(
                item["status"] != "succeeded" for item in task["subtasks"]
            ):
                raise ValueError("只有全部子任务成功后才能交付。")
            try:
                delivered_commit = (
                    task.get("delivered_commit") or self._deliver_integration(task_id)
                )
            except DeliveryReverificationRequired as exc:
                self._queue_delivery_reverification(task_id, exc)
                return self.task_payload(task_id)
            except Exception as exc:
                self._transition_task(
                    task_id, "needs_attention", "task.delivery.error",
                    "成果未能交付到目标目录：%s" % compact_error(exc),
                    error=compact_error(exc),
                )
                raise ValueError(compact_error(exc)) from exc
            try:
                deleted_branches = self._cleanup_task_git(task_id)
            except Exception as exc:
                self._transition_task(
                    task_id, "needs_attention", "task.cleanup.error",
                    "成果已交付，但临时 Git 数据清理失败：%s"
                    % compact_error(exc),
                    error=compact_error(exc),
                    payload={"deliveredCommit": delivered_commit},
                )
                raise ValueError(compact_error(exc)) from exc
            self._transition_task(
                task_id, "completed", "task.delivered",
                "成果已交付；临时 Worktree 和分支已清理。",
                error=None,
                payload={
                    "deliveredCommit": delivered_commit,
                    "deletedBranches": deleted_branches,
                },
            )
            return self.task_payload(task_id)

    def cleanup_task_git(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task["status"] in {"planning", "running", "verifying"}:
                raise ValueError("任务仍在运行，请先停止任务。")
            deleted_branches = self._cleanup_task_git(task_id)
            refreshed = self.require_task(task_id)
            verification = (refreshed.get("plan") or {}).get("verification") or {}
            if refreshed.get("delivered_commit") and verification.get("passed"):
                self._transition_task(
                    task_id, "completed", "task.cleaned",
                    "临时 Worktree 和任务分支已清理。",
                    error=None,
                    payload={"deletedBranches": deleted_branches},
                )
            else:
                self.store.add_event(
                    task_id,
                    "task.cleaned",
                    "临时 Worktree 和任务分支已清理。",
                    payload={"deletedBranches": deleted_branches},
                )
                self._write_docs_unlocked(task_id)
            return self.task_payload(task_id)

    def cancel_task(self, task_id):
        with self._task_lock(task_id):
            return self._cancel_task_locked(task_id)

    def _cancel_task_locked(self, task_id):
        task = self.require_task(task_id)
        if task["status"] not in {"planning", "running", "verifying"}:
            raise ValueError("当前任务不在运行。")
        self.store.update_task(task_id, cancel_requested=1)
        self.agent_gate.wake_all()
        self.store.add_event(task_id, "task.cancel", "已请求取消，正在停止运行中的 Agent。")
        keys = ["task-%s-planner" % task_id, "task-%s-verifier" % task_id]
        keys.extend(
            self._subtask_run_key(item["id"], item["dispatch_id"])
            for item in task["subtasks"]
            if item.get("dispatch_id")
        )
        for key in keys:
            try:
                with self.active_run_lock:
                    adapter = self.active_run_adapters.get(key)
                if adapter:
                    adapter.interrupt(key)
            except Exception as exc:
                self.store.add_event(task_id, "task.cancel.error", compact_error(exc))
        self._spawn(self._finish_cancel, task)
        return self.task_payload(task_id)

    def _finish_cancel(self, task):
        current = self.require_task(task["id"])
        if current["status"] == "cancelled":
            return
        for item in current["subtasks"]:
            if item["status"] in {"pending", "running", "blocked"}:
                self._update_subtask_state(
                    task["id"], item["id"], status="cancelled", error="用户取消任务。"
                )
        self._transition_task(
            task["id"], "cancelled", "task.cancelled", "任务已取消。"
        )

    @staticmethod
    def _terminate_process(process, platform=None):
        if process.poll() is not None:
            return
        platform = os.name if platform is None else platform
        if platform == "nt":
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
                process.wait(timeout=2)
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
            process.wait(timeout=2)

    def retry_subtask(self, subtask_id, executor=None, model=None, effort=None,
                      message=None):
        message = str(message or "").strip()
        task, subtask = self.require_subtask(subtask_id)
        if subtask["status"] not in {"failed", "blocked", "cancelled"}:
            raise ValueError("只有失败、阻塞或取消的子任务可以重试。")
        with self._task_lock(task["id"]):
            retry_executor = subtask.get("executor")
            retry_model = subtask.get("model") or ""
            if executor is not None or model is not None or effort is not None:
                current_snapshot = subtask.get("executor_snapshot") or {}
                selected_executor = (
                    str(executor or "").strip()
                    or subtask.get("executor")
                    or current_snapshot.get("executor")
                )
                selected_model = (
                    model if model is not None
                    else subtask.get("model") or current_snapshot.get("model")
                )
                selected_effort = (
                    effort if effort is not None
                    else subtask.get("reasoning_effort")
                    or current_snapshot.get("reasoningEffort")
                    or "high"
                )
                retry_executor, retry_snapshot = self._assign_subtask_executor(
                    subtask, selected_executor, selected_model, selected_effort
                )
                retry_model = retry_snapshot.get("model") or ""
            self.store.update_subtask(
                subtask_id,
                status="pending",
                error=None,
                exit_code=None,
                dispatch_id=None,
                session_id=None,
                pending_message=message or None,
            )
            for item in task["subtasks"]:
                if item["status"] == "blocked":
                    self.store.update_subtask(
                        item["id"], status="pending", error=None, dispatch_id=None
                    )
            self.store.update_task(task["id"], cancel_requested=0)
            self._transition_task(
                task["id"], "running", "subtask.retry",
                "已安排重试 %s（%s / %s）。" % (
                    subtask["title"],
                    EXECUTOR_LABELS.get(str(retry_executor or "").lower(), "原执行器"),
                    retry_model or "默认模型",
                ),
                subtask_id=subtask_id,
            )
        self._ensure_scheduler(task["id"])
        return self.task_payload(task["id"])

    def revise_subtask(self, subtask_id, message, executor=None, model=None,
                       effort=None):
        message = str(message or "").strip()
        if not message:
            raise ValueError("新增要求不能为空。")
        task, subtask = self.require_subtask(subtask_id)
        if subtask["status"] != "succeeded":
            raise ValueError("只有已经成功完成的子任务可以修改并重跑。")
        worktree = subtask.get("worktree")
        if task.get("cleaned_at") or not worktree or not Path(worktree).is_dir():
            raise ValueError("原 Agent 现场已清理，请使用‘继续修改’创建新任务。")
        if task["status"] not in {"needs_attention", "failed", "cancelled"}:
            raise ValueError("请等待任务暂停后再修改已完成的子任务。")
        with self._task_lock(task["id"]):
            current = self.require_task(task["id"])
            if current.get("delivered_commit"):
                workspace = self._resolve_workspace(current["workspace"])
                branch = self._git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=workspace,
                    check=False,
                ).stdout.strip()
                head = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=workspace
                ).stdout.strip()
                if branch != current.get("base_branch") or head != current.get(
                    "delivered_commit"
                ):
                    raise ValueError(
                        "目标目录已离开原交付提交，请使用‘继续修改’创建新任务。"
                    )
                self.store.update_task(
                    task["id"], base_commit=head, delivered_commit=None,
                    delivered_at=None,
                )

            snapshot = subtask.get("executor_snapshot") or {}
            selected_executor = (
                str(executor or "").strip()
                or subtask.get("executor")
                or snapshot.get("executor")
            )
            selected_model = (
                model if model is not None
                else subtask.get("model") or snapshot.get("model")
            )
            selected_effort = (
                effort if effort is not None
                else subtask.get("reasoning_effort")
                or snapshot.get("reasoningEffort")
                or "high"
            )
            selected_executor, selected_snapshot = self._assign_subtask_executor(
                subtask, selected_executor, selected_model, selected_effort
            )
            dispatch_id = uuid.uuid4().hex
            plan = dict(current.get("plan") or {})
            plan.pop("verification", None)
            self.store.update_task(
                task["id"], cancel_requested=0,
                plan_json=json.dumps(plan, ensure_ascii=False),
            )
            self.store.update_subtask(
                subtask_id,
                status="running",
                attempt=int(subtask.get("attempt") or 0) + 1,
                error=None,
                exit_code=None,
                dispatch_id=dispatch_id,
                session_id=None,
                pending_message=None,
            )
            self._transition_task(
                task["id"], "running", "subtask.revise",
                "按新增要求重新执行 %s（%s / %s，Dispatch %s）。" % (
                    subtask["title"], EXECUTOR_LABELS[selected_executor],
                    selected_snapshot.get("model") or "默认模型",
                    dispatch_id[:8],
                ),
                subtask_id=subtask_id,
            )
            refreshed = next(
                item for item in self.require_task(task["id"])["subtasks"]
                if item["id"] == subtask_id
            )
            thread = self._spawn(
                self._run_subtask, task["id"], refreshed, message, dispatch_id, False
            )
            if thread is None:
                self.store.update_subtask(
                    subtask_id, status="failed", error="服务正在关闭，未启动重跑。"
                )
                raise ValueError("服务正在关闭，未启动重跑。")
        return self.task_payload(task["id"])

    def _assign_subtask_executor(self, subtask, executor, model=None, effort=None):
        executor, adapter = self._executor_adapter(executor, WRITE_CAPABILITY)
        selected_model = str(model or "").strip()
        available = {
            str(item.get("model") or item.get("slug") or item.get("id"))
            for item in adapter.models() if isinstance(item, dict)
        }
        if selected_model and available and selected_model not in available:
            raise ValueError("模型 %s 不属于 %s。" % (selected_model, EXECUTOR_LABELS[executor]))
        selected_effort = str(effort or "high").lower()
        if selected_effort not in EFFORTS:
            raise ValueError("推理等级无效。")
        snapshot = self._executor_snapshot(executor, selected_model or None, selected_effort)
        definition = (subtask.get('executor_snapshot') or {}).get('agent')
        if definition:
            snapshot['agent'] = definition
        self.store.update_subtask(
            subtask["id"],
            executor=executor,
            executor_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            model=snapshot.get("model") or None,
            reasoning_effort=selected_effort,
        )
        return executor, snapshot

    def assign_agent(self, task_id, role, agent_id=None, subtask_id=None, temporary=None):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            if task['status'] != 'awaiting_approval':
                raise ValueError('只能在确认执行前分配 Agent。')
            if role not in ('worker', 'verifier'):
                raise ValueError('不能修改当前任务的规划 Agent。')
            if bool(agent_id) == bool(temporary):
                raise ValueError('请选择已有 Agent 或提供临时定义。')
            if temporary is not None and not isinstance(temporary, dict):
                raise ValueError('临时 Agent 必须是定义对象。')
            definition = (self.agent_library.snapshot(agent_id, role) if agent_id else
                          self.agent_library.normalize({**temporary, 'roles': [role]}))
            if role == 'worker':
                subtask = next((item for item in task['subtasks'] if item['id'] == subtask_id), None)
                if not subtask or subtask['attempt']:
                    raise ValueError('子任务不存在或已执行。')
                self._assign_subtask_executor(subtask, definition['executor'], definition['model'], definition['reasoningEffort'])
                current = self.require_subtask(subtask_id)[1]
                snapshot = {**current['executor_snapshot'], 'agent': definition}
                self.store.update_subtask(subtask_id, executor_snapshot_json=json.dumps(snapshot, ensure_ascii=False))
                plan = task.get('plan') or {}
                for item in plan.get('subtasks', []):
                    if item['key'] == subtask['plan_key']:
                        item.update(agentSnapshot=definition, executor=definition['executor'], agentId=definition.get('id'))
                        item.pop('temporaryAgent', None)
                self.store.update_task(task_id, plan_json=json.dumps(plan, ensure_ascii=False))
            else:
                roles = task['role_settings']
                roles['verifier'] = {**definition, 'agent': definition}
                self.store.update_task(task_id, role_settings_json=json.dumps(roles, ensure_ascii=False))
            self.store.add_event(task_id, 'agent.assigned', '已更新 Agent 分配。', subtask_id=subtask_id)
            self._write_docs_unlocked(task_id)
        return self.task_payload(task_id)

    def update_subtask_executor(self, subtask_id, executor, model=None, effort=None):
        task, subtask = self.require_subtask(subtask_id)
        if task["status"] != "awaiting_approval" or subtask["attempt"]:
            raise ValueError("只有等待确认且尚未执行的子任务可以更换执行器。")
        with self._task_lock(task["id"]):
            executor, snapshot = self._assign_subtask_executor(
                subtask, executor, model, effort
            )
            self.store.add_event(
                task["id"], "subtask.executor", "%s 改用 %s。" % (
                    subtask["plan_key"], EXECUTOR_LABELS[executor]
                ), subtask_id=subtask_id,
            )
            self._write_docs_unlocked(task["id"])
        return self.task_payload(task["id"])

    def update_subtask_plan(self, subtask_id, title, instructions, executor,
                            model=None, effort=None):
        """Edit one generated subtask before the user approves execution."""
        title = str(title or "").strip()
        instructions = str(instructions or "").strip()
        if not title:
            raise ValueError("子任务简介不能为空。")
        if len(title) > 80:
            raise ValueError("子任务简介不能超过 80 个字符。")
        if not instructions:
            raise ValueError("子任务具体要求不能为空。")
        task, subtask = self.require_subtask(subtask_id)
        if task["status"] != "awaiting_approval" or subtask["attempt"]:
            raise ValueError("只有等待确认且尚未执行的子任务可以修改。")
        with self._task_lock(task["id"]):
            selected_executor, snapshot = self._assign_subtask_executor(
                subtask, executor, model, effort
            )
            self.store.update_subtask(
                subtask_id, title=title, instructions=instructions
            )
            current = self.require_task(task["id"])
            plan = dict(current.get("plan") or {})
            planned_subtasks = [dict(item) for item in plan.get("subtasks", [])]
            for item in planned_subtasks:
                if item.get("key") == subtask["plan_key"]:
                    item.update({
                        "title": title,
                        "instructions": instructions,
                        "executor": selected_executor,
                    })
                    break
            plan["subtasks"] = planned_subtasks
            self.store.update_task(
                task["id"], plan_json=json.dumps(plan, ensure_ascii=False)
            )
            self.store.add_event(
                task["id"], "subtask.updated",
                "%s 的内容和执行设置已更新（%s / %s）。" % (
                    subtask["plan_key"], EXECUTOR_LABELS[selected_executor],
                    snapshot.get("model") or "默认模型",
                ),
                subtask_id=subtask_id,
            )
            self._write_docs_unlocked(task["id"])
        return self.task_payload(task["id"])

    def update_task_verifier(self, task_id, executor, model=None, effort=None):
        task = self.require_task(task_id)
        retrying_verification = (
            task["status"] in {"needs_attention", "failed"}
            and bool(task["subtasks"])
            and all(item["status"] == "succeeded" for item in task["subtasks"])
        )
        if task["status"] != "awaiting_approval" and not retrying_verification:
            raise ValueError(
                "只有确认执行前，或全部子任务成功后的重新验收阶段，"
                "可以选择验收执行器。"
            )
        executor, adapter = self._executor_adapter(executor, VERIFY_CAPABILITY)
        selected_model = str(model or "").strip()
        available = {
            str(item.get("model") or item.get("slug") or item.get("id"))
            for item in adapter.models() if isinstance(item, dict)
        }
        if selected_model and available and selected_model not in available:
            raise ValueError("模型 %s 不属于 %s。" % (selected_model, EXECUTOR_LABELS[executor]))
        selected_effort = str(effort or "high").lower()
        if selected_effort not in EFFORTS:
            raise ValueError("验收推理等级无效。")
        snapshot = self._executor_snapshot(executor, selected_model or None, selected_effort)
        verifier = {
            "executor": executor,
            "name": EXECUTOR_LABELS[executor],
            "kind": adapter.kind,
            "model": snapshot.get("model") or "",
            "reasoningEffort": selected_effort,
            "snapshot": snapshot,
        }
        with self._task_lock(task_id):
            current = self.require_task(task_id)
            roles = dict(current.get("role_settings") or {})
            if roles.get('verifier', {}).get('agent'):
                verifier['agent'] = roles['verifier']['agent']
            roles["verifier"] = verifier
            self.store.update_task(
                task_id, role_settings_json=json.dumps(roles, ensure_ascii=False)
            )
            self.store.add_event(
                task_id, "task.verifier",
                "最终验收将由 %s 执行。" % EXECUTOR_LABELS[executor],
            )
            self._write_docs_unlocked(task_id)
        return self.task_payload(task_id)

    def message_subtask(self, subtask_id, message):
        task, subtask = self.require_subtask(subtask_id)
        message = str(message or "").strip()
        if not message:
            raise ValueError("追加指令不能为空。")
        if subtask["status"] in {"running", "pending"}:
            raise ValueError("子任务正在运行，请等待当前回合结束。")
        worktree = subtask.get("worktree")
        if task.get("cleaned_at") or not worktree or not Path(worktree).is_dir():
            raise ValueError("原 Agent 现场已清理，请使用‘继续修改’创建新任务。")
        if not subtask.get("session_id"):
            raise ValueError("该子任务还没有可续跑的 Agent 会话。")
        executor = subtask.get("executor")
        if not executor:
            raise ValueError("该子任务没有记录执行器。")
        self._executor_adapter(executor, RESUME_CAPABILITY)
        with self._task_lock(task["id"]):
            current = self.require_task(task["id"])
            if current.get("delivered_commit"):
                workspace = self._resolve_workspace(current["workspace"])
                branch = self._git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"],
                    cwd=workspace,
                    check=False,
                ).stdout.strip()
                head = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=workspace
                ).stdout.strip()
                if branch != current.get("base_branch") or head != current.get(
                    "delivered_commit"
                ):
                    raise ValueError(
                        "目标目录已离开原交付提交，请使用‘继续修改’创建新任务。"
                    )
                self.store.update_task(
                    task["id"],
                    base_commit=head,
                    delivered_commit=None,
                    delivered_at=None,
                )
            self.store.update_subtask(
                subtask_id,
                status="pending",
                error=None,
                pending_message=message,
                dispatch_id=None,
            )
            self.store.update_task(task["id"], cancel_requested=0)
            self._transition_task(
                task["id"], "running", "subtask.message", "已向 %s 追加指令。" % subtask["title"],
                subtask_id=subtask_id,
            )
        self._ensure_scheduler(task["id"])
        return self.task_payload(task["id"])

    def require_task(self, task_id):
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("找不到任务。")
        return task

    def require_subtask(self, subtask_id):
        task_id = subtask_id.split(":", 1)[0]
        task = self.require_task(task_id)
        for item in task["subtasks"]:
            if item["id"] == subtask_id:
                return task, item
        raise KeyError("找不到子任务。")

    def task_payload(self, task_id):
        with self._task_lock(task_id):
            task = self.require_task(task_id)
            repository = self.store.get_repository(task.get("repository_id"))
            task["repository_alias"] = (
                repository["alias"] if repository else Path(task["workspace"]).name
            )
            docs = {}
            folder = self.data_dir / "tasks" / task_id
            for name in ("task.md", "progress.md", "acceptance.md"):
                path = folder / name
                docs[name] = path.read_text(encoding="utf-8") if path.exists() else ""
            task["documents"] = docs
            task["runs"] = self.agent_library.runs(task_id)
            return self._public_task(task)

    def list_payload(self):
        return [self._public_task(task) for task in self.store.list_tasks()]

    @staticmethod
    def _public_task(task):
        task["role_settings"] = {
            role: {
                key: value for key, value in setting.items()
                if key in {
                    "executor", "name", "model", "reasoningEffort", "backend",
                    "kind", "source", "readOnly", "agent",
                }
            }
            for role, setting in (task.get("role_settings") or {}).items()
            if role in {"planner", "worker", "verifier"}
        }
        for item in task.get("subtasks", []):
            snapshot = item.get("executor_snapshot", {}) or {}
            item["executor"] = item.get("executor") or snapshot.get("executor")
            item["executor_snapshot"] = {
                key: value for key, value in snapshot.items()
                if key in {"executor", "name", "model", "reasoningEffort", "agent"}
            }
            # Never expose columns left behind by a database created by an older build.
            item.pop("agent_profile_id", None)
            item.pop("agent_profile_snapshot_json", None)
            item.pop("planned_skills", None)
            item.pop("injected_skills", None)
        return task

    def write_docs(self, task_id):
        with self._task_lock(task_id):
            self._write_docs_unlocked(task_id)

    def _write_docs_unlocked(self, task_id):
        self.documents.write(task_id)

    def shutdown(self):
        with self.thread_lock:
            self.shutting_down = True
        self.agent_gate.wake_all()
        self.global_agent_gate.wake_all()
        adapters = set(self.executors.values())
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            self.planner.close()
        except Exception:
            pass
        close_runner = getattr(self.command_runner, "close", None)
        if callable(close_runner):
            try:
                close_runner()
            except Exception:
                pass
        current = threading.current_thread()
        deadline = time.time() + 6
        while time.time() < deadline:
            with self.thread_lock:
                threads = [
                    thread
                    for thread in self.background_threads
                    if thread is not current and thread.is_alive()
                ]
            if not threads:
                break
            for thread in threads:
                thread.join(timeout=min(0.5, max(0, deadline - time.time())))
