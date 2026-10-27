# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""``python -m nature_worker.platform_client [serve|init|preflight]``."""
from __future__ import annotations

import argparse
import json
import sys

from ..installation_mode import ModeError, initialize_platform_client
from ..maintenance import MaintenanceWatcher
from .server import ClientSettings, create_app, running_submissions, startup_findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nature-worker-platform-client")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "init", "preflight"))
    args = parser.parse_args(argv)
    settings = ClientSettings.from_env()

    if args.command == "init":
        initialize_platform_client(settings.data_dir)
        print(f"Platform Client installation initialized at {settings.data_dir}")
        return 0

    if args.command == "preflight":
        preflight, findings = startup_findings(settings, True)
        print(json.dumps({
            "allowed": preflight.allowed,
            "blockers": list(preflight.blockers),
            "findings": [{"check": f.check, "verified": f.clean, "detail": f.detail} for f in findings],
            "credential_storage": "memory-only (nothing persisted)",
        }, indent=2))
        return 0 if preflight.allowed else 1

    try:
        server, watcher = build_server(settings)
    except ModeError as exc:
        print(exc, file=sys.stderr)
        return 1
    watcher.start()
    try:
        server.run()
    finally:
        watcher.stop()
    return 0


def build_server(settings: ClientSettings):
    """The loopback UI server and its upgrade watcher (shared by the console and the
    Windows service host). Raises ModeError when the preflight blocks startup."""
    if settings.host not in ("127.0.0.1", "::1", "localhost"):
        raise ModeError("The Platform Client UI listens on loopback only.")
    app = create_app(settings)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port,
                                           access_log=False, log_level="warning"))
    # An upgrade stop: no new submissions; running ones finish; then a clean exit,
    # whose shutdown signs out of the Platform (revokes the session).
    watcher = MaintenanceWatcher(settings.data_dir, busy=lambda: running_submissions(app),
                                 on_drain=lambda on: setattr(app.state, "draining", on),
                                 on_ready=lambda: setattr(server, "should_exit", True))
    return server, watcher


if __name__ == "__main__":
    sys.exit(main())
