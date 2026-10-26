# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Build and sign a Nature AI Worker release bundle (see docs/RELEASES.md).

  python scripts/release_bundle.py keygen --private-key FILE --comment TEXT
      New Ed25519 signing key (PEM, mode 0600). Prints the public key entry
      for src/nature_worker/upgrade/release_keys.json (official keys) or for
      an installation's trusted-keys.json (locally trusted keys).

  python scripts/release_bundle.py build --private-key FILE --notes FILE --out DIR
      [--min-upgrade-from X.Y.Z] [--channel stable] [--src DIR]

      Builds the Worker wheel from the committed HEAD (``git archive``; never
      the working tree) unless --src names an explicit source tree, downloads
      every dependency wheel pinned by requirements.lock for THIS computer's
      OS, architecture and Python, hashes everything and signs the manifest.
      Build each platform's bundle on that platform: pip cannot evaluate
      platform markers for another OS.

Run with the dev environment's Python (it needs `cryptography` and pip).
Never commit or upload the private key.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from nature_worker.upgrade.release import (  # noqa: E402
    MANIFEST, PRODUCT, SIGNATURE, Manifest, public_key_entry, sha256_file, sign_manifest,
)
from nature_worker.upgrade.toolkit import host_target  # noqa: E402


def _passphrase() -> bytes | None:
    value = os.environ.get("NATURE_RELEASE_KEY_PASSPHRASE")
    return value.encode() if value else None


def keygen(args) -> int:
    key = Ed25519PrivateKey.generate()
    secret = _passphrase()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(secret) if secret
                            else serialization.NoEncryption())
    fd = os.open(args.private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(pem)
    print(json.dumps(public_key_entry(key, args.comment), indent=2))
    return 0


def _load_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=_passphrase())
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("The signing key must be an Ed25519 key.")
    return key


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kwargs)


def _committed_source(dest: Path) -> dict:
    commit = _run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
    archive = dest.with_suffix(".tar")
    with open(archive, "wb") as stream:
        subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "HEAD"], check=True, stdout=stream)
    dest.mkdir()
    _run(["tar", "-xf", str(archive), "-C", str(dest)])
    tag = subprocess.run(["git", "-C", str(REPO), "describe", "--tags", "--exact-match", "HEAD"],
                         text=True, capture_output=True).stdout.strip()
    return {"repository": "https://github.com/nature-ai-corp/nature-ai-worker", "commit": commit,
            "tag": tag or None}


def _code_facts(src: Path) -> dict:
    probe = ("import json, sys; sys.path.insert(0, sys.argv[1]);"
             "import nature_worker as n; from nature_worker import migrations as m;"
             "from nature_worker.platform_contract import SUPPORTED_VERSION as v;"
             "print(json.dumps({'version': n.__version__, 'standalone': m.DATA_SCHEMA,"
             "'platform_client': m.PLATFORM_CLIENT_SCHEMA, 'client_interface': v}))")
    return json.loads(_run([sys.executable, "-I", "-c", probe, str(src / "src")]).stdout)


def build(args) -> int:
    key = _load_key(args.private_key)
    out: Path = args.out
    if out.exists():
        raise SystemExit(f"{out} already exists.")
    with tempfile.TemporaryDirectory() as tmp:
        if args.src:
            src, source = args.src.resolve(), {"repository": "local source tree (not a committed revision)",
                                               "commit": None, "tag": None}
        else:
            src = Path(tmp) / "src"
            source = _committed_source(src)
        facts = _code_facts(src)
        wheels = out / "wheels"
        wheels.mkdir(parents=True)
        pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
        _run([*pip, "wheel", "--no-deps", "-q", "-w", str(wheels), str(src)])
        product = next(wheels.glob("nature_ai_worker-*.whl"))
        _run([*pip, "download", "-q", "--only-binary=:all:", "-d", str(wheels),
              "-r", str(src / "requirements.lock"), str(product)])
    notes = out / "RELEASE_NOTES.md"
    notes.write_bytes(args.notes.read_bytes())

    def entry(path: Path) -> dict:
        return {"path": path.relative_to(out).as_posix(), "sha256": sha256_file(path), "size": path.stat().st_size}

    manifest = {
        "schema": 1, "product": PRODUCT, "version": facts["version"],
        "released_at": time.strftime("%Y-%m-%d"), "channel": args.channel,
        "target": host_target(), "editions": ["standalone", "platform_client"],
        "upgrade_from": {"min": args.min_upgrade_from or facts["version"]},
        "data_schema": {"standalone": facts["standalone"], "platform_client": facts["platform_client"]},
        "client_interface": facts["client_interface"],
        "notes": entry(notes),
        "product_wheel": product.relative_to(out).as_posix(),
        "artifacts": [entry(w) for w in sorted(wheels.glob("*.whl"))],
        "source": source,
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    Manifest.parse(raw)  # the same validation an installation applies
    (out / MANIFEST).write_bytes(raw)
    (out / SIGNATURE).write_bytes(sign_manifest(raw, key))
    print(f"Built {out}: {PRODUCT} {facts['version']} for {manifest['target']}, "
          f"{len(manifest['artifacts'])} wheels, manifest sha256 {Manifest.parse(raw).digest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("keygen")
    p.add_argument("--private-key", type=Path, required=True)
    p.add_argument("--comment", required=True)
    p = sub.add_parser("build")
    p.add_argument("--private-key", type=Path, required=True)
    p.add_argument("--notes", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--min-upgrade-from")
    p.add_argument("--channel", default="stable")
    p.add_argument("--src", type=Path, help="explicit source tree (tests); default: committed HEAD")
    args = parser.parse_args(argv)
    return {"keygen": keygen, "build": build}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
