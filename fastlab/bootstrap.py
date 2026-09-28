"""Command-line bootstrap and graceful server lifecycle."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from .application import FastLab
from .config import APP_ROOT, load_local_env
from .http_server import FastLabHandler, ThreadingHTTPServer
from .tenancy import FastLabService


def build_parser():
    parser = argparse.ArgumentParser(description="FastLab multi-user Agent task console")
    parser.add_argument(
        "--workspace", default=None,
        help="Legacy local-mode path inside an existing Git repository",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("FASTLAB_PORT", "4180")))
    parser.add_argument("--data-dir", default=str(APP_ROOT / ".fastlab"))
    parser.add_argument(
        "--env-file", default=None,
        help="Local secrets/config file; defaults to <data-dir>/fastlab.env",
    )
    parser.add_argument(
        "--codex-bin", default=None,
        help="Codex CLI path; defaults to FASTLAB_CODEX_BIN or codex on PATH",
    )
    parser.add_argument(
        "--claude-bin", default=None,
        help="Claude CLI path; defaults to FASTLAB_CLAUDE_BIN or claude on PATH",
    )
    return parser


def _preparse_environment(argv):
    """Resolve the env file before environment-backed parser defaults."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=str(APP_ROOT / ".fastlab"))
    parser.add_argument("--env-file", default=None)
    known, _unknown = parser.parse_known_args(argv)
    path = Path(known.env_file) if known.env_file else Path(known.data_dir) / "fastlab.env"
    loaded = load_local_env(path, required=bool(known.env_file))
    return path, loaded


def parse_arguments(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    # Help must remain available even when an old or partially edited env file
    # is invalid; argparse exits before any service configuration is consumed.
    if "-h" in values or "--help" in values:
        return build_parser().parse_args(values), None, []
    env_path, loaded = _preparse_environment(values)
    return build_parser().parse_args(values), env_path, loaded


def _install_signal_handlers(server):
    previous = {}

    def stop(_signum, _frame):
        # BaseServer.shutdown must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, name="fastlab-shutdown", daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)
        except (OSError, RuntimeError, ValueError):
            pass
    return previous


def _restore_signal_handlers(previous):
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, RuntimeError, ValueError):
            pass


def local_repository_root(data_dir, workspace=None):
    """Use managed repositories for normal local runs; preserve explicit legacy workspaces."""
    if str(workspace or "").strip():
        return None
    return Path(data_dir).expanduser().resolve() / "repositories"


def main(argv=None):
    try:
        arguments, env_path, loaded_env = parse_arguments(argv)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if loaded_env:
        print("Loaded local environment config: %s" % env_path.expanduser().resolve(), flush=True)
    if sys.version_info < (3, 10):
        raise SystemExit("FastLab 需要 Python 3.10 或更高版本。")
    try:
        auth_mode = str(os.environ.get("FASTLAB_AUTH_MODE") or "local").strip().lower()
        if auth_mode == "research":
            app = FastLabService(arguments.data_dir)
        elif auth_mode == "local":
            app = FastLab(
                arguments.workspace,
                arguments.data_dir,
                arguments.codex_bin or os.environ.get("FASTLAB_CODEX_BIN"),
                claude_bin=arguments.claude_bin or os.environ.get("FASTLAB_CLAUDE_BIN"),
                repository_root=local_repository_root(
                    arguments.data_dir, arguments.workspace
                ),
            )
        else:
            raise ValueError("FASTLAB_AUTH_MODE 只能是 local 或 research。")
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit("FastLab 启动失败：%s" % exc) from exc
    if isinstance(app, FastLab) and app.store.repaired_event_payloads:
        print("已修复 %s 条旧版本截断的日志记录。" % app.store.repaired_event_payloads, flush=True)
    FastLabHandler.app = app
    try:
        server = ThreadingHTTPServer(("127.0.0.1", arguments.port), FastLabHandler)
    except OSError as exc:
        app.shutdown()
        raise SystemExit("FastLab 监听端口失败：%s" % exc) from exc
    server.daemon_threads = True
    previous_handlers = _install_signal_handlers(server)
    print("FastLab is running at http://127.0.0.1:%s" % server.server_address[1], flush=True)
    print("Mode: %s" % ("research" if isinstance(app, FastLabService) else "local"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.shutdown()
        _restore_signal_handlers(previous_handlers)


__all__ = ["build_parser", "local_repository_root", "main", "parse_arguments"]
