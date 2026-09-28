#!/usr/bin/env python3
"""Backward-compatible FastLab server entry point.

The implementation lives in :mod:`fastlab`; this module intentionally keeps
the historical import surface for integrations and operator scripts.
"""

from fastlab.application import *  # noqa: F401,F403
from fastlab.bootstrap import build_parser, main, parse_arguments  # noqa: F401
from fastlab.config import (  # noqa: F401
    APP_ROOT,
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_GLOBAL_CONCURRENCY,
    EFFORTS,
    EXECUTOR_LABELS,
    LOCAL_ENV_KEYS,
    MAX_EVENT_PAYLOAD,
    MAX_GLOBAL_CONCURRENCY,
    PLANNER_SCHEMA,
    PLANNER_SKILL,
    ROLE_NAMES,
    SUBTASK_STATES,
    TASK_STATES,
    VERIFIER_SCHEMA,
    WEB_ROOT,
    compact_error,
    executable_command,
    load_local_env,
    parse_agent_timeout,
    parse_claude_extra_allowed_tools,
    process_group_options,
    resolve_executable,
    slug,
    utc_now,
)
from fastlab.errors import AgentRunTimeout  # noqa: F401
from fastlab.http_server import FastLabHandler, ThreadingHTTPServer  # noqa: F401
from fastlab.tenancy import FastLabService  # noqa: F401


if __name__ == "__main__":
    main()
