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

    def _execute_agent(self, task_id, key, cwd, prompt, sandbox, executor,
                   capability, _run_id=None, **options):
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
                    self.agent_library.running(_run_id)
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

    def _execute_planner(self, task_id, key, prompt, context, planner=None, _run_id=None):
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
                    self.agent_library.running(_run_id)
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

    def run_agent(self, task_id, key, cwd, prompt, sandbox, executor, capability, **options):
        task = self.require_task(task_id)
        subtask = next((item for item in task['subtasks']
                        if item.get('dispatch_id') and self._subtask_run_key(item['id'], item['dispatch_id']) == key), None)
        role = 'worker' if subtask else 'verifier'
        snapshot = (subtask.get('executor_snapshot') or {}) if subtask else task['role_settings'].get(role, {})
        instance = subtask['id'] if subtask else task_id + ':verifier'
        if not options.get('resume_session'):
            prompt += self.agent_library.prompt(snapshot.get('agent'))
        with self.agent_library.record(task_id, key, role, executor, snapshot, instance, options.get('resume_session')) as outcome:
            callback = options.get('on_event')
            def event(kind, message, payload):
                if isinstance(payload, dict):
                    outcome['session_id'] = payload.get('thread_id') or payload.get('sessionId') or payload.get('session_id') or outcome['session_id']
                if callback:
                    callback(kind, message, {**(payload or {}), 'run_id': outcome['run_id']} if isinstance(payload, (dict, type(None))) else payload)
            options['on_event'] = event
            result = self._execute_agent(task_id, key, cwd, prompt, sandbox, executor, capability, _run_id=outcome['run_id'], **options)
            code, session, final = result
            cancelled = self.shutting_down or self.require_task(task_id)['cancel_requested']
            outcome.update(exit_code=code, session_id=session or outcome['session_id'], status='cancelled' if cancelled else 'succeeded' if code == 0 else 'failed', error=self.store.redactor(str(final))[:1000] if code else None)
            return result

    def run_planner(self, task_id, key, prompt, context, planner=None):
        task = self.require_task(task_id)
        snapshot = task['role_settings'].get('planner', {})
        prompt += self.agent_library.prompt(snapshot.get('agent'))
        with self.agent_library.record(task_id, key, 'planner', snapshot.get('executor', 'codex'), snapshot, task_id + ':planner') as outcome:
            callback = context.get('on_event')
            def event(kind, message, payload):
                if isinstance(payload, dict):
                    outcome['session_id'] = payload.get('thread_id') or payload.get('sessionId') or payload.get('session_id') or outcome['session_id']
                if callback:
                    callback(kind, message, {**(payload or {}), 'run_id': outcome['run_id']} if isinstance(payload, (dict, type(None))) else payload)
            result = self._execute_planner(task_id, key, prompt, {**context, 'on_event': event}, planner, _run_id=outcome['run_id'])
            if self.shutting_down or self.require_task(task_id)['cancel_requested']:
                outcome['status'] = 'cancelled'
            return result
