"""Session recovery uses an in-memory fake vault, no Platform or Windows secrets."""
import pytest
from nature_worker.platform_session import PlatformSessionError
from nature_worker.windows_session_recovery import WindowsSessionRecovery


class FakeVault:
    def __init__(self):
        self.value = None
    def get(self, key):
        return self.value
    def put(self, key, value):
        if self.value is not None:
            raise RuntimeError("duplicate")
        self.value = value
    def delete(self, key):
        self.value = None


class FakePlatform:
    def __init__(self):
        self.valid = True
        self.revoke_ok = True
        self.revoked = []
    def authenticate(self): return "test-token"
    def validate_session(self, session): return self.valid
    def revoke_session(self, session):
        self.revoked.append(session)
        return self.revoke_ok


def test_journal_survives_restart_and_is_revoked_before_new_login():
    vault, platform = FakeVault(), FakePlatform()
    first = WindowsSessionRecovery(vault, platform)
    assert first.authenticate_and_validate() == "test-token"
    restarted = WindowsSessionRecovery(vault, platform)
    with pytest.raises(PlatformSessionError, match="recovery required"):
        restarted.authenticate_and_validate()
    restarted.revoke_and_clear()
    assert vault.value is None
    assert platform.revoked == ["test-token"]


def test_rejected_session_is_preserved_until_revoked():
    vault, platform = FakeVault(), FakePlatform()
    platform.valid = False
    recovery = WindowsSessionRecovery(vault, platform)
    with pytest.raises(PlatformSessionError):
        recovery.authenticate_and_validate()
    assert recovery.blocked()
    platform.revoke_ok = False
    with pytest.raises(PlatformSessionError):
        recovery.revoke_and_clear()
    assert recovery.blocked()
    platform.revoke_ok = True
    recovery.revoke_and_clear()
    assert not recovery.blocked()


def test_no_secret_adoption_if_journal_write_fails():
    class BrokenVault(FakeVault):
        def put(self, key, value): raise OSError("disk error")
    vault, platform = BrokenVault(), FakePlatform()
    with pytest.raises(PlatformSessionError, match="token revoked"):
        WindowsSessionRecovery(vault, platform).authenticate_and_validate()
    assert platform.revoked == ["test-token"]
