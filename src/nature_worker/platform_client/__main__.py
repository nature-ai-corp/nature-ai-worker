"""``python -m nature_worker.platform_client [serve|init|preflight]``."""
from __future__ import annotations

import argparse
import json
import sys

from ..installation_mode import ModeError, initialize_platform_client
from .server import ClientSettings, create_app, startup_findings


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

    if settings.host not in ("127.0.0.1", "::1", "localhost"):
        print("The Platform Client UI listens on loopback only.", file=sys.stderr)
        return 1
    try:
        app = create_app(settings)
    except ModeError as exc:
        print(exc, file=sys.stderr)
        return 1
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
