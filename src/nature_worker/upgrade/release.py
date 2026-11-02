# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Release identity: versions, trusted keys, signed manifests, bundle checks.

A release bundle is a directory (or the same files under an https:// URL)::

    release.json        the manifest (schema 1, below)
    release.json.sig    Ed25519 signature of the exact bytes of release.json
    RELEASE_NOTES.md    hashed in the manifest
    wheels/*.whl        the Worker wheel and every dependency wheel, hashed
    runtime/*.tar.gz    optional (Windows Standalone): the pinned Python runtime, hashed

Nothing in a bundle is used until the manifest signature verifies against a
trusted key and each file matches its recorded size and SHA-256. Bundles are
copied into a private staging directory and verified there, so a file swapped
in the source afterwards is never installed.

Trusted keys: ``release_keys.json`` in this package lists the official NATURE
AI release keys (scope "official"). An installation may also list keys in its
own ``trusted-keys.json`` (scope "local", e.g. a company's internal builds);
such releases are always reported as NOT official.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PRODUCT = "nature-ai-worker"
MANIFEST = "release.json"
SIGNATURE = "release.json.sig"
BUNDLED_KEYS = Path(__file__).with_name("release_keys.json")
MAX_MANIFEST_BYTES = 1 << 20
MAX_ARTIFACT_BYTES = 200 << 20
EDITIONS = ("standalone", "platform_client")

_VERSION = re.compile(r"^(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})"
                      r"(?:\+([a-z0-9]{1,32}(?:\.[a-z0-9]{1,32}){0,3}))?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[0-9a-f]{16}$")
_FILE = re.compile(r"^(?:wheels/|runtime/)?[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_PYTHON_VERSION = re.compile(r"^3\.\d{1,2}\.\d{1,3}$")
_WHEEL = re.compile(r"^([A-Za-z0-9_.]+)-([A-Za-z0-9_.!+]+)(?:-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")


class ReleaseError(ValueError):
    """A release, bundle or key that must not be trusted or installed."""


@dataclass(frozen=True, order=True)
class Version:
    """``MAJOR.MINOR.PATCH`` with an optional ``+local`` label (local builds).

    Ordering compares the release numbers; a local label never makes a build
    newer than the release it was built from.
    """
    release: tuple[int, int, int]
    local: str | None = field(default=None, compare=False)

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _VERSION.match(text or "")
        if not match:
            raise ReleaseError(f"Invalid version {text!r}: expected MAJOR.MINOR.PATCH.")
        return cls((int(match[1]), int(match[2]), int(match[3])), match[4])

    def __str__(self) -> str:
        base = ".".join(map(str, self.release))
        return f"{base}+{self.local}" if self.local else base


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    public_key: Ed25519PublicKey = field(repr=False)
    scope: str  # "official" | "local"
    comment: str = ""


def key_id_for(raw_public_key: bytes) -> str:
    return hashlib.sha256(raw_public_key).hexdigest()[:16]


def load_keys(path: Path, scope: str) -> list[TrustedKey]:
    """Read a key list. A missing file is an empty list; a malformed one is an error."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"Unreadable key list {path}.") from exc
    if not isinstance(record, dict) or record.get("schema") != 1 or not isinstance(record.get("keys"), list):
        raise ReleaseError(f"Unrecognized key list {path}.")
    keys = []
    for entry in record["keys"]:
        try:
            raw = base64.b64decode(entry["public_key"], validate=True)
            public = Ed25519PublicKey.from_public_bytes(raw)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ReleaseError(f"Invalid public key in {path}.") from exc
        if entry.get("key_id") != key_id_for(raw):
            raise ReleaseError(f"Key id does not match its public key in {path}.")
        keys.append(TrustedKey(entry["key_id"], public, scope, str(entry.get("comment", ""))))
    return keys


def trusted_keys(local_file: Path | None = None) -> list[TrustedKey]:
    keys = load_keys(BUNDLED_KEYS, "official")
    if local_file is not None:
        official = {k.key_id for k in keys}
        keys += [k for k in load_keys(local_file, "local") if k.key_id not in official]
    return keys


def public_key_entry(private_key: Ed25519PrivateKey, comment: str) -> dict:
    from cryptography.hazmat.primitives import serialization
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"key_id": key_id_for(raw), "public_key": base64.b64encode(raw).decode(), "comment": comment}


def sign_manifest(raw: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """The content of ``release.json.sig`` for manifest bytes ``raw``."""
    entry = public_key_entry(private_key, "")
    return (json.dumps({"schema": 1, "algorithm": "ed25519", "key_id": entry["key_id"],
                        "signature": base64.b64encode(private_key.sign(raw)).decode()}) + "\n").encode()


def verify_signature(raw: bytes, signature_file: bytes, keys: list[TrustedKey]) -> TrustedKey:
    try:
        record = json.loads(signature_file)
        if record.get("schema") != 1 or record.get("algorithm") != "ed25519":
            raise ReleaseError("Unrecognized signature format.")
        key_id, signature = record["key_id"], base64.b64decode(record["signature"], validate=True)
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error) as exc:
        raise ReleaseError("Unreadable release signature.") from exc
    key = next((k for k in keys if k.key_id == key_id), None)
    if key is None:
        raise ReleaseError(f"The release is signed by key {key_id!r}, which this installation does "
                           "not trust. It is not an official release for this installation.")
    try:
        key.public_key.verify(signature, raw)
    except InvalidSignature:
        raise ReleaseError("The release signature is invalid: the manifest was altered or "
                           "not signed by the key it names.") from None
    return key


@dataclass(frozen=True)
class Artifact:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Runtime:
    """A Python runtime shipped in the bundle (python-build-standalone ``install_only``
    archive, top folder ``python/``), so the installation needs no Python of its own."""
    artifact: Artifact
    python: str         # exact version, e.g. "3.12.15"; its major.minor must equal target.python
    origin: dict        # upstream URL and SHA-256, recorded for provenance (informational)


@dataclass(frozen=True)
class Manifest:
    raw: bytes = field(repr=False)
    version: Version
    released_at: str
    channel: str
    target: dict
    editions: tuple[str, ...]
    min_upgrade_from: Version
    data_schema: dict
    client_interface: str | None
    notes: Artifact
    product_wheel: str
    artifacts: tuple[Artifact, ...]
    source: dict
    runtime: Runtime | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @classmethod
    def parse(cls, raw: bytes) -> "Manifest":
        try:
            m = json.loads(raw)
        except ValueError as exc:
            raise ReleaseError("The manifest is not valid JSON.") from exc
        if not isinstance(m, dict) or m.get("schema") != 1:
            raise ReleaseError("Unrecognized manifest schema.")
        if m.get("product") != PRODUCT:
            raise ReleaseError(f"The bundle is not a {PRODUCT} release.")
        try:
            artifacts = tuple(_artifact(a) for a in m["artifacts"])
            notes = _artifact(m["notes"])
            runtime = None
            if m.get("runtime") is not None:
                r = m["runtime"]
                runtime = Runtime(_artifact(r), str(r["python"]), dict(r.get("origin") or {}))
            target = m["target"]
            editions = tuple(m["editions"])
            data_schema = {str(k): int(v) for k, v in m["data_schema"].items()}
            manifest = cls(
                raw=raw, version=Version.parse(m["version"]), released_at=str(m["released_at"]),
                channel=str(m.get("channel", "stable")), target=target, editions=editions,
                min_upgrade_from=Version.parse(m["upgrade_from"]["min"]), data_schema=data_schema,
                client_interface=m.get("client_interface"), notes=notes,
                product_wheel=str(m["product_wheel"]), artifacts=artifacts,
                source=dict(m.get("source") or {}), runtime=runtime,
            )
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            if isinstance(exc, ReleaseError):
                raise
            raise ReleaseError(f"Incomplete or malformed manifest ({exc}).") from exc
        if not isinstance(target, dict) or not all(isinstance(target.get(k), str) for k in ("os", "arch", "python")):
            raise ReleaseError("The manifest target must name os, arch and python.")
        if not editions or any(e not in EDITIONS for e in editions):
            raise ReleaseError("The manifest lists an unknown edition.")
        paths = [a.path for a in artifacts]
        if len(set(paths)) != len(paths) or notes.path in paths:
            raise ReleaseError("The manifest lists a file twice.")
        if any(not p.startswith("wheels/") or not p.endswith(".whl") for p in paths):
            raise ReleaseError("Every artifact must be a wheel under wheels/.")
        if runtime is not None:
            if not runtime.artifact.path.startswith("runtime/") or not runtime.artifact.path.endswith(".tar.gz") \
                    or runtime.artifact.path in paths or runtime.artifact.path == notes.path:
                raise ReleaseError("The runtime must be one .tar.gz archive under runtime/.")
            if not _PYTHON_VERSION.match(runtime.python) \
                    or runtime.python.rsplit(".", 1)[0] != target.get("python"):
                raise ReleaseError("The runtime's Python version does not match the release target.")
        elif any(p.startswith("runtime/") for p in paths):
            raise ReleaseError("Every artifact must be a wheel under wheels/.")
        if manifest.product_wheel not in paths:
            raise ReleaseError("The Worker wheel is not among the hashed artifacts.")
        if manifest.min_upgrade_from > manifest.version:
            raise ReleaseError("The manifest's minimum upgrade version is newer than the release.")
        return manifest


def _artifact(entry: dict) -> Artifact:
    path, digest, size = entry["path"], entry["sha256"], entry["size"]
    if not isinstance(path, str) or not _FILE.match(path) or ".." in path:
        raise ReleaseError(f"Unsafe file name in manifest: {path!r}.")
    if not isinstance(digest, str) or not _SHA256.match(digest):
        raise ReleaseError(f"Invalid SHA-256 for {path}.")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_ARTIFACT_BYTES:
        raise ReleaseError(f"Invalid size for {path}.")
    return Artifact(path, digest, size)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wheel_requirement(path: str) -> str:
    """``name==version`` for a wheel file name (used with ``--require-hashes``)."""
    match = _WHEEL.match(Path(path).name)
    if not match:
        raise ReleaseError(f"Not a wheel file name: {path}.")
    return f"{match[1]}=={match[2]}"


@dataclass(frozen=True)
class VerifiedRelease:
    manifest: Manifest
    signer: TrustedKey
    bundle: Path
    notes: str

    @property
    def official(self) -> bool:
        return self.signer.scope == "official"

    def wheels(self) -> list[tuple[Path, Artifact]]:
        return [(self.bundle / a.path, a) for a in self.manifest.artifacts]


def _check_file(path: Path, artifact: Artifact, root: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"{artifact.path} is missing from the bundle (or is a link).")
    if root.resolve() not in path.resolve().parents:
        raise ReleaseError(f"{artifact.path} lies outside the bundle.")
    if path.stat().st_size != artifact.size:
        raise ReleaseError(f"{artifact.path} has the wrong size: the bundle is damaged or altered.")
    if sha256_file(path) != artifact.sha256:
        raise ReleaseError(f"{artifact.path} does not match its SHA-256: the bundle is damaged or altered.")


def verify_bundle(bundle: Path, keys: list[TrustedKey]) -> VerifiedRelease:
    """Verify a bundle directory in place: signature first, then every file."""
    raw = _read_limited(bundle / MANIFEST, MAX_MANIFEST_BYTES)
    signer = verify_signature(raw, _read_limited(bundle / SIGNATURE, 4096), keys)
    manifest = Manifest.parse(raw)
    for artifact in _files(manifest):
        _check_file(bundle / artifact.path, artifact, bundle)
    notes = (bundle / manifest.notes.path).read_text(encoding="utf-8", errors="replace")
    return VerifiedRelease(manifest, signer, bundle, notes)


def _files(manifest: Manifest) -> tuple[Artifact, ...]:
    runtime = (manifest.runtime.artifact,) if manifest.runtime else ()
    return (manifest.notes, *manifest.artifacts, *runtime)


def _read_limited(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"{path.name} is missing from the bundle.")
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ReleaseError(f"{path.name} is too large.")
    return data


def stage_bundle(source: str, dest: Path, keys: list[TrustedKey]) -> VerifiedRelease:
    """Copy (or download) a bundle into the new private directory ``dest`` and verify it there.

    Only files the signed manifest names are copied. ``source`` is a local
    directory or an ``https://`` URL of the directory.
    """
    dest.mkdir(mode=0o700, parents=True)
    remote = source.startswith("https://")
    if not remote and "://" in source:
        raise ReleaseError("Release sources must be a local directory or an https:// URL.")

    def fetch(name: str, limit: int) -> None:
        target = dest / name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if remote:
            _download(source.rstrip("/") + "/" + name, target, limit)
        else:
            origin = Path(source) / name
            if origin.is_symlink() or not origin.is_file():
                raise ReleaseError(f"{name} is missing from the bundle (or is a link).")
            shutil.copyfile(origin, target)

    fetch(MANIFEST, MAX_MANIFEST_BYTES)
    fetch(SIGNATURE, 4096)
    raw = _read_limited(dest / MANIFEST, MAX_MANIFEST_BYTES)
    verify_signature(raw, _read_limited(dest / SIGNATURE, 4096), keys)
    manifest = Manifest.parse(raw)
    for artifact in _files(manifest):
        fetch(artifact.path, artifact.size)
    return verify_bundle(dest, keys)


def _download(url: str, target: Path, limit: int) -> None:
    import httpx

    def https_only(request: httpx.Request) -> None:
        if request.url.scheme != "https":
            raise ReleaseError("Release downloads must stay on https://.")

    try:
        with httpx.Client(follow_redirects=True, timeout=60, trust_env=False,
                          event_hooks={"request": [https_only]}) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ReleaseError(f"Download failed ({response.status_code}): {urlsplit(url).path}")
                size = 0
                with open(target, "wb") as stream:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ReleaseError(f"{target.name} is larger than the manifest allows.")
                        stream.write(chunk)
    except httpx.HTTPError as exc:
        raise ReleaseError(f"Download failed: {type(exc).__name__}.") from exc
