"""Windows S-C session recovery journal backed by the separate DPAPI vault.

The journal is conservative: it records a token *before* validation and treats
every recovered token as requiring server confirmation/revocation. It must be
accessed only by an OS-isolated Platform Client, never the Standalone Agent.
"""
from __future__ import annotations

from .platform_session import PlatformSessionError, PlatformTransport
from .windows_credentials import WindowsPlatformCredentialStore

_JOURNAL = "pending_server_session"


class WindowsSessionRecovery:
    def __init__(self, vault: WindowsPlatformCredentialStore, transport: PlatformTransport):
        self.vault = vault
        self.transport = transport

    def blocked(self) -> bool:
        return self.vault.get(_JOURNAL) is not None

    def authenticate_and_validate(self) -> str:
        if self.blocked():
            raise PlatformSessionError("Unresolved prior server session; recovery required")
        token = self.transport.authenticate()
        if not isinstance(token, str) or not token:
            raise PlatformSessionError("Platform authentication returned no session")
        # Durable journal before any further API use. If this write fails, do
        # not validate or adopt the token; request server-side cleanup.
        try:
            self.vault.put(_JOURNAL, token.encode("utf-8"))
        except Exception as exc:
            try:
                revoked = self.transport.revoke_session(token) is True
            except Exception:
                revoked = False
            if not revoked:
                raise PlatformSessionError(
                    "Session journal failed and revocation is unverified; administrative recovery required"
                ) from exc
            raise PlatformSessionError("Could not persist server session; token revoked") from exc
        try:
            valid = self.transport.validate_session(token) is True
        except Exception as exc:
            raise PlatformSessionError("Validation interrupted; recovery journal retained") from exc
        if not valid:
            raise PlatformSessionError("Session validation rejected; recovery journal retained")
        return token

    def revoke_and_clear(self) -> None:
        raw = self.vault.get(_JOURNAL)
        if raw is None:
            return
        try:
            token = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlatformSessionError("Corrupt recovery journal; manual recovery required") from exc
        try:
            revoked = self.transport.revoke_session(token) is True
        except Exception as exc:
            raise PlatformSessionError("Server revocation unavailable; journal retained") from exc
        if not revoked:
            raise PlatformSessionError("Server revocation unconfirmed; journal retained")
        self.vault.delete(_JOURNAL)
