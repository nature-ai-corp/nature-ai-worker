"""Start the server: ``python -m nature_worker`` or ``nature-worker``."""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .app import create_app
from .config import Config
from .credentials import CredentialStore, RedactSecretsFilter
from .providers import SDK_ENV_OVERRIDES


def main() -> None:
    argparse.ArgumentParser(
        prog="nature-worker",
        description="Start the Nature AI Worker web app (default http://127.0.0.1:8080). "
        "Configure with NATURE_WORKER_* environment variables; see README.md and .env.example.",
    ).parse_args()
    config = Config.from_env()
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
