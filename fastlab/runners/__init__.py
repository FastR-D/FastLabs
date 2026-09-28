"""Execution backends used by FastLab's Agent adapters."""

from fastlab.contracts import ExecutionRunner

from .process import ProcessCommandRunner

__all__ = ["ExecutionRunner", "ProcessCommandRunner"]
