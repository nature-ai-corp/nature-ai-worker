"""Runtime configuration read from environment variables.

Only deployment settings live here (paths, bind address). User-facing
settings such as the selected provider are stored in SQLite, and API keys
are stored by ``credentials.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass
class Config:
    data_dir: Path
    workspace_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        allowed = _env("NATURE_WORKER_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
        return cls(
            data_dir=Path(_env("NATURE_WORKER_DATA_DIR", "./data")).resolve(),
            workspace_dir=Path(_env("NATURE_WORKER_WORKSPACE", "./workspace")).resolve(),
            host=_env("NATURE_WORKER_HOST", "127.0.0.1"),
            port=int(_env("NATURE_WORKER_PORT", "8080")),
            allowed_hosts=[h.strip() for h in allowed.split(",") if h.strip()],
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
