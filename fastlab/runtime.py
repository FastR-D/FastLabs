"""Agent invocation, timeout, cancellation, and gate coordination."""

import threading

from .errors import AgentRunCancelled, AgentRunTimeout


class AgentRuntime:
    def __init__(self, application):
        self.application = application

    def __getattr__(self, name):
        return getattr(self.application, name)

    def timeout_guard(self, key, runner):
        timed_out = threading.Event()
        if not self.agent_timeout:
            return None, timed_out

        def expire():
            timed_out.set()
            try:
                runner.interrupt(key)
            except Exception:
                pass

        timer = threading.Timer(self.agent_timeout, expire)
        timer.daemon = True
        timer.start()
        return timer, timed_out

    def timeout_error(self, role):
        seconds = (
            int(self.agent_timeout)
            if float(self.agent_timeout).is_integer() else self.agent_timeout
        )
        return AgentRunTimeout("%s运行超过 %s 秒，已终止整个进程组。" % (role, seconds))

    @staticmethod
    def finish_timeout_guard(timer):
        if timer is None:
            return
        timer.cancel()
        if timer is not threading.current_thread():
            timer.join(timeout=1)

    def run_agent(self, task_id, key, cwd, prompt, sandbox, executor,
                   capability, **options):
        def cancelled():
            task = self.store.get_task(task_id, include_events=False)
            return self.shutting_down or task is None or task["cancel_requested"]

        _, adapter = self._executor_adapter(executor, capability)
        self.agent_gate.acquire(cancelled)
        try:
            self.global_agent_gate.acquire(cancelled)
        except Exception:
            self.agent_gate.release()
            raise
        try:
            if cancelled():
                raise AgentRunCancelled("Agent 运行在启动前被取消。")
            with self.active_run_lock:
                self.active_run_adapters[key] = adapter
            timer, timed_out = self.timeout_guard(key, adapter)
            try:
                try:
                    result = adapter.run(key, cwd, prompt, sandbox, **options)
                except Exception as exc:
                    if timed_out.is_set():
                        raise self.timeout_error("Agent ") from exc
                    raise
                if timed_out.is_set():
                    raise self.timeout_error("Agent ")
                return result
            finally:
                self.finish_timeout_guard(timer)
        finally:
            with self.active_run_lock:
                self.active_run_adapters.pop(key, None)
            self.global_agent_gate.release()
            self.agent_gate.release()

    def run_planner(self, task_id, key, prompt, context, planner=None):
        def cancelled():
            task = self.store.get_task(task_id, include_events=False)
            return self.shutting_down or task is None or task["cancel_requested"]

        planner = planner or self.planner
        self.agent_gate.acquire(cancelled)
        try:
            self.global_agent_gate.acquire(cancelled)
        except Exception:
            self.agent_gate.release()
            raise
        try:
            if cancelled():
                raise AgentRunCancelled("Planner 在启动前被取消。")
            with self.active_run_lock:
                self.active_run_adapters[key] = planner
            timer, timed_out = self.timeout_guard(key, planner)
            try:
                try:
                    result = planner.plan(prompt, context)
                except Exception as exc:
                    if timed_out.is_set():
                        raise self.timeout_error("Planner ") from exc
                    raise
                if timed_out.is_set():
                    raise self.timeout_error("Planner ")
                return result
            finally:
                self.finish_timeout_guard(timer)
        finally:
            with self.active_run_lock:
                self.active_run_adapters.pop(key, None)
            self.global_agent_gate.release()
            self.agent_gate.release()
