# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The Windows service process of the Platform Client.

The installer registers ``NatureWorkerPlatformClient`` to run as its own
virtual account. The service runs exactly what ``serve`` runs on Linux: the
same preflight, the same loopback UI and API, the same upgrade watcher. Its
settings come from the service's registry ``Environment`` (written by the
installer, administrators only). A Windows service has no console, so output
goes to ``<data root>\\logs\\service.log``, inside the private data root.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from .. import __version__
from ..installation_mode import ModeError
from ..windows_service import ServiceError, ServiceHost

SERVICE_NAME = "NatureWorkerPlatformClient"
LOG_LIMIT = 5 * 1024 * 1024
EXIT_BLOCKED, EXIT_NOT_SERVING = 2, 3


def service_log(data_dir: Path) -> Path:
    return data_dir / "logs" / "service.log"


def _redirect_output(data_dir: Path) -> None:
    try:
        path = service_log(data_dir)
        path.parent.mkdir(exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_name("service.log.1"))
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        import os
        stream = open(os.devnull, "w")
    sys.stdout = sys.stderr = stream


def run_service(stop: threading.Event, ready) -> int:
    from .__main__ import build_server
    from .server import ClientSettings
    settings = ClientSettings.from_env()
    _redirect_output(settings.data_dir)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Platform Client {__version__} starting "
          f"(127.0.0.1:{settings.port}, Platform {settings.platform_origin})")
    try:
        server, watcher = build_server(settings)
    except ModeError as exc:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {exc}")
        return EXIT_BLOCKED
    thread = threading.Thread(target=server.run, name="platform-client-ui", daemon=True)
    watcher.start()
    thread.start()
    serving = False
    try:
        while thread.is_alive():
            if not serving and server.started:
                serving = True
                ready()
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} serving on 127.0.0.1:{settings.port}")
            if stop.is_set():
                server.should_exit = True  # graceful: the shutdown signs out of the Platform
            thread.join(0.5)
    finally:
        watcher.stop()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} stopped")
    return 0 if serving else EXIT_NOT_SERVING


def main() -> int:
    try:
        return ServiceHost(SERVICE_NAME, run_service).dispatch()
    except ServiceError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
