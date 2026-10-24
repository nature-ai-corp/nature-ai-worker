"""Windows S-C session lifecycle boundary.

Only authenticated Platform client adapters may create a session. This module
does not implement or guess Platform endpoints. It intentionally never exposes
a local Agent, local task database or provider credentials.
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

    def sign_in(self) -> None:
        if self.session is not None:
            raise PlatformSessionError("Already signed in")
        token = self.transport.authenticate()
        if not isinstance(token, str) or not token:
            raise PlatformSessionError("Platform authentication did not provide a session")
        try:
            valid = self.transport.validate_session(token) is True
        except BaseException:
            try:
                self.transport.revoke_session(token)
            finally:
                raise
        if not valid:
            try:
                self.transport.revoke_session(token)
            finally:
                raise PlatformSessionError("Platform session validation failed")
        self.session = token

    def sign_out(self) -> None:
        if self.session is None:
            return
        token = self.session
        if self.transport.revoke_session(token) is not True:
            raise PlatformSessionError("Server session revocation unconfirmed")
        self.session = None
