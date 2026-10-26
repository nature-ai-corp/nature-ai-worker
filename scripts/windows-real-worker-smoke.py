# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Launch the actual installed Worker process on a Windows runner.

This is a real local Worker startup smoke test, NOT real Platform integration.
It deliberately does not use simulated agents or fake Platform responses.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nature-worker-real-smoke-") as folder:
        root = Path(folder)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = os.environ.copy()
        env["NATURE_WORKER_DATA_DIR"] = str(root / "data")
        env["NATURE_WORKER_WORKSPACE"] = str(root / "workspace")
        for key in list(env):
            if key.endswith("_API_KEY"):
                del env[key]
        proc = subprocess.Popen(
            [sys.executable, "-m", "nature_worker", "--port", str(port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(80):
                if proc.poll() is not None:
                    raise RuntimeError(f"Worker terminated on startup (exit {proc.returncode})")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=1
                    ) as response:
                        if response.status == 200:
                            print("PASS: installed real Worker started and answered /api/health")
                            return
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            raise RuntimeError("Worker did not answer local health endpoint")
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=10)


if __name__ == "__main__":
    main()
