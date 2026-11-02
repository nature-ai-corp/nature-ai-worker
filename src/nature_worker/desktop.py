# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Start-menu entry of an installed Standalone Worker: ``python -m nature_worker.desktop``.

If this installation's Worker is already running, its page is opened in the browser and
nothing else happens. Otherwise the Worker starts in this window, on NATURE_WORKER_PORT
(default 8080) or, when that port is taken by another program, on a free one, and the page
opens as soon as it answers. Closing the window stops the Worker.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .maintenance import RUNTIME_INFO, worker_running


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _any_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_when_ready(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.3)


def main() -> None:
    data_dir = Path(os.environ.get("NATURE_WORKER_DATA_DIR", "./data")).resolve()
    if worker_running(data_dir):
        try:
            port = int(json.loads((data_dir / RUNTIME_INFO).read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError):
            print(f"Nature AI Worker is already running for {data_dir}, but its address is unknown.")
            sys.exit(1)
        url = f"http://127.0.0.1:{port}/"
        print(f"Nature AI Worker is already running: opening {url}")
        webbrowser.open(url)
        return
    wanted = int(os.environ.get("NATURE_WORKER_PORT", "").strip() or 8080)
    port = wanted if _free(wanted) else _any_free_port()
    if port != wanted:
        print(f"Port {wanted} is used by another program; using port {port} instead.")
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    print("Close this window to stop Nature AI Worker.")
    from .__main__ import main as serve
    sys.argv = [sys.argv[0], "--port", str(port)]
    serve()


if __name__ == "__main__":
    main()
