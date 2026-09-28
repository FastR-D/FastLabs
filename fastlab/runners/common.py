"""Shared constants and validation helpers for Agent execution backends."""

from __future__ import annotations

from pathlib import Path


PROVIDER_BY_EXECUTOR = {
    "codex": "openai",
    "claude": "anthropic",
}

PROVIDER_ENVIRONMENT = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

DANGEROUS_CLI_ARGUMENTS = frozenset({
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--yolo",
})


def executor_provider(executor: str) -> tuple[str, str]:
    """Return the credential provider and environment variable for an executor."""
    executor = str(executor or "").strip().lower()
    try:
        provider = PROVIDER_BY_EXECUTOR[executor]
    except KeyError as exc:
        raise ValueError("不支持的 Agent 执行器：%s。" % (executor or "空值")) from exc
    return provider, PROVIDER_ENVIRONMENT[provider]


def validate_command(command) -> list[str]:
    """Normalize a command and reject flags which disable CLI security controls."""
    normalized = [str(item) for item in command]
    if not normalized:
        raise ValueError("Agent 命令不能为空。")
    dangerous = sorted(DANGEROUS_CLI_ARGUMENTS.intersection(normalized))
    if dangerous:
        raise ValueError("Agent 命令包含禁止的权限绕过参数：%s。" % ", ".join(dangerous))
    return normalized


def require_within(path: Path, root: Path, message: str) -> Path:
    """Resolve *path* and require it to remain below *root*."""
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ValueError(message) from exc
    return resolved
