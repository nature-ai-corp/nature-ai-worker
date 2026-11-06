# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import pytest
from nature_worker.platform_session import PlatformSession, PlatformSessionError


class FakeTransport:
    def __init__(self, valid=True, revoke=True):
        self.valid = valid
        self.revoke = revoke
        self.revoked = []
    def authenticate(self): return "temporary-token"
    def validate_session(self, session): return self.valid
    def revoke_session(self, session):
        self.revoked.append(session)
        return self.revoke


def test_verified_login_and_logout():
    transport = FakeTransport()
    state = PlatformSession(transport)
    state.sign_in()
    assert state.session == "temporary-token"
    state.sign_out()
    assert state.session is None
    assert transport.revoked == ["temporary-token"]


def test_rejected_login_revokes_temporary_session():
    transport = FakeTransport(valid=False)
    state = PlatformSession(transport)
    with pytest.raises(PlatformSessionError):
        state.sign_in()
    assert state.session is None
    assert transport.revoked == ["temporary-token"]


def test_failed_revoke_keeps_session_for_recovery():
    state = PlatformSession(FakeTransport(revoke=False))
    state.sign_in()
    with pytest.raises(PlatformSessionError):
        state.sign_out()
    assert state.session == "temporary-token"


def test_failed_validation_and_failed_revoke_blocks_future_login():
    transport = FakeTransport(valid=False, revoke=False)
    state = PlatformSession(transport)
    with pytest.raises(PlatformSessionError, match="revocation pending"):
        state.sign_in()
    assert state.pending_revoke == "temporary-token"
    with pytest.raises(PlatformSessionError, match="unrevoked"):
        state.sign_in()
    transport.revoke = True
    state.retry_revocation()
    assert state.pending_revoke is None


def test_failed_logout_revocation_can_be_retried():
    transport = FakeTransport(revoke=False)
    state = PlatformSession(transport)
    state.sign_in()
    with pytest.raises(PlatformSessionError):
        state.sign_out()
    assert state.pending_revoke == "temporary-token"
    transport.revoke = True
    state.retry_revocation()
    assert state.pending_revoke is None
    assert state.session is None
