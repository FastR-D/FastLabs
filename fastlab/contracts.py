"""Stable service contracts used by FastLab's modular core."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class ExecutionRunner(Protocol):
    """Prepare and supervise an Agent command without knowing its provider."""

    def wrap(
        self,
        command: Sequence[str],
        cwd: str | Path,
        writable: bool,
        executor: str,
        key: str = "run",
    ) -> tuple[list[str], str, Mapping[str, str]]:
        """Return the executable command, effective cwd, and sanitized env."""

    def interrupt(self, key: str) -> None:
        """Interrupt the full process tree associated with ``key``."""

    def started(self, key: str, process: object) -> None:
        """Associate a newly spawned process with ``key`` for supervision."""

    def finished(self, key: str) -> None:
        """Release bookkeeping for a completed command."""

    def check(self) -> None:
        """Fail closed when required execution isolation is unavailable."""

    def shutdown(self) -> None:
        """Terminate all active commands and release runner resources."""

    def close(self) -> None:
        """Compatibility alias for :meth:`shutdown`."""
