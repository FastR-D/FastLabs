"""Shared Agent interfaces, capabilities, and process supervision helpers."""

import os
import signal
import subprocess


PLAN_CAPABILITY = "plan"
WRITE_CAPABILITY = "write"
RESUME_CAPABILITY = "resume"
VERIFY_CAPABILITY = "verify"
SKILLS_CAPABILITY = "skills"


class PlannerError(RuntimeError):
    """A safe, user-facing planner failure."""


def process_group_options():
    """Create a process group so cancellation reaches every CLI child."""
    if os.name == "nt":
        return {
            "creationflags": getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            )
        }
    return {"start_new_session": True}


def terminate_process(process):
    """Terminate *process* and its complete platform process group."""
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
    """Provider-neutral CLI adapter contract used by FastLab."""

    kind = "unknown"
    capabilities = frozenset()

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


__all__ = [
    "AgentAdapter",
    "PLAN_CAPABILITY",
    "PlannerError",
    "RESUME_CAPABILITY",
    "SKILLS_CAPABILITY",
    "VERIFY_CAPABILITY",
    "WRITE_CAPABILITY",
    "process_group_options",
    "terminate_process",
]
