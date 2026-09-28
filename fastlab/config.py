"""Configuration parsing and immutable FastLab constants."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
APP_ROOT = PACKAGE_ROOT.parent
WEB_ROOT = APP_ROOT / "web"
PLANNER_SCHEMA = APP_ROOT / "schemas" / "planner.schema.json"
VERIFIER_SCHEMA = APP_ROOT / "schemas" / "verifier.schema.json"
PLANNER_SKILL = APP_ROOT / "skills" / "orchestration" / "SKILL.md"

ROLE_NAMES = ("planner", "worker", "verifier")
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
TASK_STATES = {
    "planning", "awaiting_approval", "running", "verifying", "completed",
    "needs_attention", "failed", "cancelled",
}
SUBTASK_STATES = {"pending", "running", "succeeded", "failed", "blocked", "cancelled"}
DEFAULT_GLOBAL_CONCURRENCY = 4
MAX_GLOBAL_CONCURRENCY = 32
DEFAULT_AGENT_TIMEOUT = 3600
EXECUTOR_LABELS = {"codex": "Codex", "claude": "Claude Code"}
MAX_EVENT_PAYLOAD = 20000

# Environment files are an explicit administrative surface. Keep this list in
# one module so code, examples, and contract tests cannot drift silently.
LOCAL_ENV_KEYS = {
    "FASTLAB_PLANNER_BACKEND",
    "FASTLAB_CODEX_BIN",
    "FASTLAB_CLAUDE_BIN",
    "FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS",
    "FASTLAB_AGENT_TIMEOUT",
    "FASTLAB_AUTH_MODE",
    "FASTRESEARCH_API_URL",
    "FASTRESEARCH_COOKIE_NAME",
    "FASTRESEARCH_PANEL_URL",
    "FASTLAB_PUBLIC_URL",
    "FASTLAB_PUBLIC_PATH",
    "FASTLAB_TENANT_ROOT",
    "FASTLAB_CREDENTIAL_MASTER_KEY",
    "FASTLAB_EXECUTION_BACKEND",
    "FASTLAB_WORKER_IMAGE",
    "FASTLAB_MAX_CONCURRENCY_PER_USER",
    "FASTLAB_GLOBAL_CONCURRENCY",
    "FASTLAB_CONTAINER_CPUS",
    "FASTLAB_CONTAINER_MEMORY",
    "FASTLAB_CONTAINER_PIDS",
    "FASTLAB_PORT",
    "FASTLAB_CODEX_MODELS",
    "FASTLAB_CLAUDE_MODELS",
}


def documented_environment_keys(path=None):
    """Return assignment keys documented by ``fastlab.env.example``.

    Commented optional assignments count as documentation, while prose and
    shell snippets do not. CI uses this to keep the administrator contract and
    the parser allowlist synchronized.
    """
    source = Path(path or APP_ROOT / "fastlab.env.example")
    keys = set()
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# "):
            line = line[2:].lstrip()
        match = re.match(r"([A-Z_][A-Z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def validate_environment_key_contract(path=None):
    documented = documented_environment_keys(path)
    missing = sorted(LOCAL_ENV_KEYS - documented)
    unknown = sorted(documented - LOCAL_ENV_KEYS)
    if missing or unknown:
        details = []
        if missing:
            details.append("示例缺少：%s" % ", ".join(missing))
        if unknown:
            details.append("示例含未允许项：%s" % ", ".join(unknown))
        raise ValueError("环境配置契约不一致（%s）。" % "；".join(details))
    return True


@dataclass(frozen=True)
class CapacityConfig:
    """Validated concurrency settings for one FastLab server."""

    global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY
    per_user_concurrency: int = 1

    @classmethod
    def from_environment(cls) -> "CapacityConfig":
        def limit(name, default):
            try:
                value = int(os.environ.get(name, default))
            except (TypeError, ValueError) as exc:
                raise ValueError("%s 必须是整数。" % name) from exc
            if not 1 <= value <= MAX_GLOBAL_CONCURRENCY:
                raise ValueError("%s 必须在 1–%s 之间。" % (name, MAX_GLOBAL_CONCURRENCY))
            return value

        return cls(
            limit("FASTLAB_GLOBAL_CONCURRENCY", DEFAULT_GLOBAL_CONCURRENCY),
            limit("FASTLAB_MAX_CONCURRENCY_PER_USER", 1),
        )


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_error(value, limit=1200):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[-limit:]


def slug(value, limit=52):
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
    return (cleaned or "task")[:limit]


def resolve_executable(value):
    """Resolve commands once so Windows PATHEXT launchers also work."""
    raw = os.path.expanduser(str(value or "").strip())
    if not raw:
        return None
    if os.path.dirname(raw) or Path(raw).is_absolute():
        candidate = Path(raw)
        return str(candidate.resolve()) if candidate.is_file() else None
    return shutil.which(raw)


def executable_command(binary):
    """Use the current interpreter for Python command doubles."""
    binary = str(binary)
    if Path(binary).suffix.lower() == ".py":
        return [sys.executable, binary]
    return [binary]


def process_group_options(platform=None):
    platform = os.name if platform is None else platform
    if platform == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


def load_local_env(path, required=False):
    """Load FastLab's small explicit environment-file surface."""
    path = Path(path).expanduser()
    if not path.exists():
        if required:
            raise ValueError("找不到环境配置文件：%s" % path)
        return []
    if not path.is_file():
        raise ValueError("环境配置路径不是文件：%s" % path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        print("警告：%s 可被其他用户读取，建议执行 chmod 600。" % path, file=sys.stderr)
    loaded = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError("%s:%s 不是有效的 KEY=VALUE 配置。" % (path, line_number))
        if key.startswith("FASTLAB_PLANNER_") and key != "FASTLAB_PLANNER_BACKEND":
            continue
        if key not in LOCAL_ENV_KEYS:
            raise ValueError("%s:%s 不支持配置 %s。" % (path, line_number, key))
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def parse_claude_extra_allowed_tools(value):
    raw = str(value or "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS 必须是 JSON 字符串数组。") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) and item.strip() for item in parsed):
        raise ValueError("FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS 必须是非空字符串组成的 JSON 数组。")
    tools = []
    for item in parsed:
        rule = item.strip()
        if rule in {"Bash", "Bash(*)"}:
            raise ValueError("FastLab 不允许通过环境变量开放全部 Bash；请配置具体命令规则。")
        if "\n" in rule or "\r" in rule:
            raise ValueError("Claude 命令权限规则不能包含换行。")
        if rule not in tools:
            tools.append(rule)
    return tuple(tools)


def parse_agent_timeout(value):
    raw = str(DEFAULT_AGENT_TIMEOUT if value is None else value).strip()
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("FASTLAB_AGENT_TIMEOUT 必须是大于等于 0 的秒数。") from exc
    if timeout < 0:
        raise ValueError("FASTLAB_AGENT_TIMEOUT 必须是大于等于 0 的秒数。")
    return timeout
