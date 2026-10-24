import sys
import pytest

from nature_worker.windows_credentials import (
    PlatformCredentialError, WindowsPlatformCredentialStore
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only DPAPI store")
def test_windows_enterprise_credentials_roundtrip(tmp_path):
    root = tmp_path / "sc"
    root.mkdir()
    store = WindowsPlatformCredentialStore(root)
    token = b"enterprise-test-token-only"
    store.put("session", token)
    assert token not in (root / "session.dpapi").read_bytes()
    assert store.get("session") == token
    store.delete("session")
    assert store.get("session") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only DPAPI store")
def test_refuses_unprovisioned_store(tmp_path):
    with pytest.raises(PlatformCredentialError):
        WindowsPlatformCredentialStore(tmp_path / "unprovisioned")


@pytest.mark.skipif(sys.platform == "win32", reason="requires Windows")
def test_windows_store_not_available_on_linux(tmp_path):
    with pytest.raises(PlatformCredentialError):
        WindowsPlatformCredentialStore(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_implicit_token_overwrite_is_blocked(tmp_path):
    root = tmp_path / "enterprise"
    root.mkdir()
    store = WindowsPlatformCredentialStore(root)
    store.put("session", b"original")
    with pytest.raises(PlatformCredentialError):
        store.put("session", b"replacement")
    assert store.get("session") == b"original"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_rejects_unsafe_credential_names(tmp_path):
    root = tmp_path / "enterprise"
    root.mkdir()
    store = WindowsPlatformCredentialStore(root)
    for name in ("../standalone", "sub/path", "", "a.b"):
        with pytest.raises(PlatformCredentialError):
            store.get(name)
