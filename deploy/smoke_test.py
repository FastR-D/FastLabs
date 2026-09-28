#!/usr/bin/env python3
"""Start the service entry point and verify its anonymous health API."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    port = available_port()
    environment = dict(os.environ)
    environment["FASTLAB_AUTH_MODE"] = "local"
    with tempfile.TemporaryDirectory(prefix="fastlab-deploy-smoke-") as data_dir:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "server.py"),
                "--data-dir",
                data_dir,
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        error = None
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("FastLab exited before its health check")
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:%s/api/health" % port,
                        timeout=1,
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    if response.status == 200 and payload.get("ok") is True:
                        print("FastLab deployment smoke test passed on port %s" % port)
                        return 0
                    raise RuntimeError("unexpected health response: %r" % payload)
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.1)
            raise RuntimeError("FastLab health endpoint did not become ready")
        except Exception as exc:  # Report server output below after cleanup.
            error = exc
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError("%s\n%s" % (error, output.strip()))


if __name__ == "__main__":
    raise SystemExit(main())
