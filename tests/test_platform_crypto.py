"""Encrypted Platform Client store tests; no real enterprise data."""
import pytest
from cryptography.fernet import Fernet
from nature_worker.platform_crypto import EncryptedPlatformStore, PlatformStorageError


def test_roundtrip_and_no_plaintext_on_disk(tmp_path):
    key = Fernet.generate_key()
    store = EncryptedPlatformStore(tmp_path / "enterprise", key)
    assert store.read("session") is None
    store.write("session", b"private-enterprise-value")
    assert store.read("session") == b"private-enterprise-value"
    assert b"private-enterprise-value" not in (tmp_path / "enterprise" / "session.enc").read_bytes()


def test_standalone_key_cannot_decrypt(tmp_path):
    root = tmp_path / "enterprise"
    EncryptedPlatformStore(root, Fernet.generate_key()).write("task", b"company secret")
    with pytest.raises(PlatformStorageError, match="decrypt"):
        EncryptedPlatformStore(root, Fernet.generate_key()).read("task")


@pytest.mark.parametrize("name", ["../outside", "/absolute", ".hidden", "", "x/y", ".."])
def test_rejects_unsafe_names(tmp_path, name):
    store = EncryptedPlatformStore(tmp_path / "enterprise", Fernet.generate_key())
    with pytest.raises(PlatformStorageError):
        store.write(name, b"secret")


def test_rejects_corrupted_ciphertext(tmp_path):
    root = tmp_path / "enterprise"
    store = EncryptedPlatformStore(root, Fernet.generate_key())
    store.write("session", b"secret")
    (root / "session.enc").write_bytes(b"invalid")
    with pytest.raises(PlatformStorageError):
        store.read("session")


def test_rejects_symlinked_file(tmp_path):
    root = tmp_path / "enterprise"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    (root / "cache.enc").symlink_to(outside)
    store = EncryptedPlatformStore(root, Fernet.generate_key())
    with pytest.raises(PlatformStorageError):
        store.write("cache", b"secret")
    assert outside.read_bytes() == b"untouched"


def test_does_not_store_key_alongside_data(tmp_path):
    root = tmp_path / "enterprise"
    key = Fernet.generate_key()
    store = EncryptedPlatformStore(root, key)
    store.write("history", b"data")
    assert not (root / "master.key").exists()
    assert key not in (root / "history.enc").read_bytes()
