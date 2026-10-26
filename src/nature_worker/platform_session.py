# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Platform session state; real transport must use the existing S-C contract.

In-memory only. Durable revocation recovery and OS-protected credential
persistence are mandatory before connecting to real enterprise sessions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PlatformSessionError(RuntimeError):
    pass


class PlatformTransport(Protocol):
    def authenticate(self) -> str: ...
    def validate_session(self, session: str) -> bool: ...
    def revoke_session(self, session: str) -> bool: ...


@dataclass
class PlatformSession:
    transport: PlatformTransport
    session: str | None = None
    pending_revoke: str | None = None

    def _revoke_or_mark_pending(self, token: str) -> bool:
        self.pending_revoke = token
        try:
            if self.transport.revoke_session(token) is not True:
                return False
        except Exception:
            return False
        self.pending_revoke = None
        return True

    def sign_in(self) -> None:
        if self.session is not None or self.pending_revoke is not None:
            raise PlatformSessionError("Existing or unrevoked Platform session")
        token = self.transport.authenticate()
        if not isinstance(token, str) or not token:
            raise PlatformSessionError("Platform authentication did not provide a session")
        try:
            valid = self.transport.validate_session(token) is True
        except Exception as exc:
            if not self._revoke_or_mark_pending(token):
                raise PlatformSessionError("Validation failed; session revocation pending") from exc
            raise
        if not valid:
            if not self._revoke_or_mark_pending(token):
                raise PlatformSessionError("Invalid session; server revocation pending")
            raise PlatformSessionError("Platform session validation failed")
        self.session = token

    def sign_out(self) -> None:
        if self.session is None:
            if self.pending_revoke is not None:
                self.retry_revocation()
            return
        token = self.session
        if not self._revoke_or_mark_pending(token):
            raise PlatformSessionError("Server session revocation unconfirmed")
        self.session = None

    def retry_revocation(self) -> None:
        if self.pending_revoke is None:
            return
        token = self.pending_revoke
        if not self._revoke_or_mark_pending(token):
            raise PlatformSessionError("Server session revocation still unconfirmed")
        if self.session == token:
            self.session = None
