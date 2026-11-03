#!/usr/bin/env python3
"""Disposable local FastLab server used by Playwright tests."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastlab.bootstrap import main  # noqa: E402


if __name__ == "__main__":
    os.environ["FASTLAB_AUTH_MODE"] = "local"
    os.environ["FASTLAB_CODEX_BIN"] = str(ROOT / "tests" / "fake_codex.py")
    os.environ["FASTLAB_CLAUDE_BIN"] = str(ROOT / "tests" / "fake_claude.py")
    with tempfile.TemporaryDirectory(prefix="fastlab-playwright-") as data_dir:
        main(["--data-dir", data_dir, "--port", "4199"])
