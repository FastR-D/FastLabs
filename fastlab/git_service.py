"""Git command boundary used by FastLab workflows."""

import os
import subprocess

from .config import compact_error


class GitService:
    """Run non-interactive Git commands with consistent errors and timeouts."""

    def __init__(self, binary, default_workspace=None):
        self.binary = binary
        self.default_workspace = default_workspace

    def set_default_workspace(self, workspace):
        self.default_workspace = workspace

    def run(self, arguments, cwd=None, check=True, timeout=None):
        try:
            result = subprocess.run(
                [self.binary] + list(arguments),
                cwd=str(cwd or self.default_workspace),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Git 命令超时。") from exc
        if check and result.returncode:
            raise RuntimeError(compact_error(result.stdout) or "Git 命令失败")
        return result
