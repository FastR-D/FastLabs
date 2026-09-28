"""Execution backends used by FastLab's Agent adapters."""

from fastlab.contracts import ExecutionRunner

from .docker import DockerCommandRunner
from .process import ProcessCommandRunner

__all__ = ["DockerCommandRunner", "ExecutionRunner", "ProcessCommandRunner"]
