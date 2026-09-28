"""Fair, cancellation-aware scheduling primitives."""

import threading
from collections import deque

from .config import DEFAULT_GLOBAL_CONCURRENCY, MAX_GLOBAL_CONCURRENCY
from .errors import AgentRunCancelled


class AgentRunGate:
    """FIFO global/per-tenant concurrency limiter."""

    def __init__(self, limit=DEFAULT_GLOBAL_CONCURRENCY):
        self.limit = max(1, min(MAX_GLOBAL_CONCURRENCY, int(limit)))
        self.active = 0
        self.waiters = deque()
        self.condition = threading.Condition()

    def set_limit(self, limit):
        with self.condition:
            self.limit = max(1, min(MAX_GLOBAL_CONCURRENCY, int(limit)))
            self.condition.notify_all()

    def acquire(self, cancelled):
        token = {"id": object()}
        with self.condition:
            self.waiters.append(token)
            while True:
                if cancelled():
                    try:
                        self.waiters.remove(token)
                    except ValueError:
                        pass
                    self.condition.notify_all()
                    raise AgentRunCancelled("Agent 运行在排队期间被取消。")
                if self.waiters[0] is token and self.active < self.limit:
                    self.waiters.remove(token)
                    self.active += 1
                    return
                self.condition.wait(timeout=0.1)

    def release(self):
        with self.condition:
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    def wake_all(self):
        with self.condition:
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"active": self.active, "queued": len(self.waiters), "limit": self.limit}
