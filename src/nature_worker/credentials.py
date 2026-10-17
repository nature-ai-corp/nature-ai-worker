"""Local storage for provider API keys.

Keys are resolved in this order:

1. Environment variable for the provider (e.g. ``OPENAI_API_KEY``).
2. ``credentials.enc`` in the data directory, encrypted with Fernet
   (AES-128-CBC + HMAC-SHA256 from the ``cryptography`` package).

The encryption key comes from ``NATURE_WORKER_SECRET_KEY`` if set, otherwise
from ``master.key`` created in the data directory with 0600 permissions.
Keeping the master key outside the data directory (via the environment
variable) means a copy of the data directory alone does not reveal API keys.

Keys are never written to SQLite, logs, or conversation history.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class CredentialStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "credentials.enc"
        self._lock = threading.Lock()
        self._fernet = Fernet(self._master_key(data_dir))

    @staticmethod
    def _master_key(data_dir: Path) -> bytes:
        secret = os.environ.get("NATURE_WORKER_SECRET_KEY", "")
        if secret:
            return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        key_path = data_dir / "master.key"
        if not key_path.exists():
            _write_private(key_path, Fernet.generate_key())
        return key_path.read_bytes().strip()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except InvalidToken:
            log.error(
                "Stored API keys could not be decrypted (master key changed?). "
                "Re-enter your API key in Settings."
            )
            return {}

    def get(self, provider: str, endpoint: str | None = None) -> tuple[str | None, str]:
        """Return (key, source) where source is 'env', 'stored', 'other_endpoint' or 'none'.

        ``endpoint`` is the normalized endpoint the key would be sent to (None for
        providers with a fixed official endpoint). A stored key is only returned
        for the endpoint it was saved with, so changing a configurable endpoint
        never sends an existing key to a different server.
        """
        env_name = ENV_VARS.get(provider)
        if env_name and os.environ.get(env_name):
            return os.environ[env_name], "env"
        with self._lock:
            entry = self._load().get(provider)
        if not isinstance(entry, dict) or not entry.get("key"):
            return None, "none"
        if entry.get("endpoint") != endpoint:
            return None, "other_endpoint"
        return entry["key"], "stored"

    def stored_endpoint(self, provider: str) -> str | None:
        with self._lock:
            entry = self._load().get(provider)
        return entry.get("endpoint") if isinstance(entry, dict) else None

    def set(self, provider: str, key: str | None, endpoint: str | None = None) -> None:
        with self._lock:
            data = self._load()
            if key:
                data[provider] = {"key": key, "endpoint": endpoint}
            else:
                data.pop(provider, None)
            _write_private(self.path, self._fernet.encrypt(json.dumps(data).encode()))

    def all_values(self) -> list[str]:
        """Every known secret value, used by the log redaction filter."""
        values = [os.environ[v] for v in ENV_VARS.values() if os.environ.get(v)]
        with self._lock:
            values.extend(e["key"] for e in self._load().values() if isinstance(e, dict) and e.get("key"))
        return values


class RedactSecretsFilter(logging.Filter):
    """Replaces any known API key in log records with ``[REDACTED]``."""

    def __init__(self, store: CredentialStore):
        super().__init__()
        self.store = store

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = self.store.all_values()
        msg = record.getMessage()
        redacted = redact(msg, secrets)
        if redacted != msg:
            record.msg, record.args = redacted, None
        # Tracebacks are formatted separately from the message; redact them too.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, secrets)
        return True


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    return text
