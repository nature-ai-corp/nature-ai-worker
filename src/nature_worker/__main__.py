"""Start the server: ``python -m nature_worker`` or ``nature-worker``."""

from __future__ import annotations

import argparse
import errno
import logging
import os
import socket
import sys

import uvicorn

from .app import create_app
from .config import Config
from .credentials import CredentialStore, RedactSecretsFilter
from .providers import SDK_ENV_OVERRIDES


def _port(value: str) -> int:
    if not value.strip().isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError(value)
    return int(value)


def _check_port_free(host: str, port: int) -> str | None:
    """Return an error message if the address cannot be bound. Nothing else is touched."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except socket.gaierror as e:
        return f"Cannot listen on {host}: {e.strerror}. Check NATURE_WORKER_HOST."
    family, kind, proto, _, addr = infos[0]
    with socket.socket(family, kind, proto) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does
        try:
            s.bind(addr)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return (
                    f"Port {port} is already in use on {host}. Another program (or another copy of "
                    f"Nature AI Worker) is using it.\n"
                    f"  Start on a different port:  nature-worker --port {port + 1 if port < 65535 else 8081}\n"
                    f"  Or find what is using it:   ss -ltnp 'sport = :{port}'"
                )
            if e.errno == errno.EACCES:
                return f"Not allowed to listen on port {port}. Ports below 1024 need extra privileges; use --port 8080 or higher."
            return f"Cannot listen on {host}:{port}: {e.strerror}."
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nature-worker",
        description="Start the Nature AI Worker web app (default http://127.0.0.1:8080). "
        "Configure with NATURE_WORKER_* environment variables; see README.md and .env.example.",
    )
    parser.add_argument(
        "--port", metavar="PORT",
        help="HTTP port to listen on (default: NATURE_WORKER_PORT, or 8080). The address stays 127.0.0.1 unless "
        "NATURE_WORKER_HOST is set.",
    )
    args = parser.parse_args()
    raw_port = args.port if args.port is not None else os.environ.get("NATURE_WORKER_PORT", "").strip() or "8080"
    try:
        port = _port(raw_port)
    except ValueError:
        parser.error(f"invalid port {raw_port!r}: use a number from 1 to 65535")
    os.environ["NATURE_WORKER_PORT"] = str(port)
    config = Config.from_env()

    problem = _check_port_free(config.host, config.port)
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)
    config.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP client libraries can log request details; keep them quiet.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "openai", "anthropic", "google_genai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    redact = RedactSecretsFilter(CredentialStore(config.data_dir))
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)

    ignored = [name for name in SDK_ENV_OVERRIDES if os.environ.get(name)]
    if ignored:
        logging.getLogger("nature_worker").warning(
            "Ignoring %s: model endpoints and keys come from Settings, not SDK environment overrides.",
            ", ".join(ignored),
        )
    app = create_app(config)
    print(f"Nature AI Worker is running at http://{config.host}:{config.port}")
    print(f"Workspace folder: {config.workspace_dir}")
    uvicorn.run(app, host=config.host, port=config.port, log_config=None)


if __name__ == "__main__":
    main()
