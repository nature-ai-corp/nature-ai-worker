import sys
import pytest
from nature_worker.windows_dpapi import (
    WindowsProtectionError, protect_for_current_user, unprotect_for_current_user
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_dpapi_roundtrip_and_ciphertext_differs():
    secret = b"enterprise-session-test-value"
    encrypted = protect_for_current_user(secret)
    assert encrypted != secret
    assert unprotect_for_current_user(encrypted) == secret


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_dpapi_rejects_corrupt_ciphertext():
    with pytest.raises(WindowsProtectionError):
        unprotect_for_current_user(b"invalid-ciphertext")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows DPAPI available")
def test_dpapi_rejects_non_windows():
    with pytest.raises(WindowsProtectionError):
        protect_for_current_user(b"secret")
