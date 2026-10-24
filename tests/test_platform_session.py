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
