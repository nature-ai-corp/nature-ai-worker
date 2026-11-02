# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Build and sign a Nature AI Worker release bundle (see docs/RELEASES.md).

  python scripts/release_bundle.py keygen --private-key FILE --comment TEXT
      New Ed25519 signing key (PEM, mode 0600). Prints the public key entry
      for src/nature_worker/upgrade/release_keys.json (official keys) or for
      an installation's trusted-keys.json (locally trusted keys).

  python scripts/release_bundle.py build --private-key FILE --notes FILE --out DIR
      [--min-upgrade-from X.Y.Z] [--channel stable] [--src DIR]
      [--target OS-ARCH-PYTHON --wheel-hashes FILE] [--runtime ARCHIVE --runtime-pin JSON]

      Builds the Worker wheel from the committed HEAD (``git archive``; never
      the working tree) unless --src names an explicit source tree, downloads
      every dependency wheel pinned by requirements.lock for THIS computer's
      OS, architecture and Python, hashes everything and signs the manifest.
      Build each platform's bundle on that platform: pip cannot evaluate
      platform markers for another OS.

      Exception: a bundle for another target (``--target windows-x86_64-3.12``) is
      built from an explicit, hashed wheel list (``--wheel-hashes``, e.g.
      packaging/windows/wheels-win_amd64-cp312.sha256): exactly those wheels are
      downloaded without dependency resolution and each must match its SHA-256.
      ``--runtime`` adds the pinned Python runtime archive (checked against
      ``--runtime-pin``) so the installation needs no Python of its own.

  python scripts/release_bundle.py verify --bundle DIR [--repo CLONE] [--trust-key FILE] [--rebuild]

      Verifies a bundle's provenance (docs/RELEASES.md, "What makes a release
      official"): signature by an official key, every file's SHA-256, the
      official repository, tag v<version>, and - with --repo, a clone that has
      the tags - that the tag points at the recorded commit and whether the tag
      itself is signed. --rebuild also rebuilds the Worker wheel from that
      commit and reports whether it is byte-identical. Exit 0 only for an
      official release whose provenance checks all pass.

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
    MANIFEST, PRODUCT, SIGNATURE, Manifest, ReleaseError, load_keys, public_key_entry, sha256_file,
    sign_manifest, trusted_keys, verify_bundle,
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


OFFICIAL_REPOSITORY = "https://github.com/nature-ai-corp/nature-ai-worker"


def _commit_epoch(repo: Path, commit: str) -> str:
    """The commit's own time: wheel timestamps then depend only on the source."""
    return _run(["git", "-C", str(repo), "log", "-1", "--format=%ct", commit]).stdout.strip()


BUILD_REQUIREMENTS = REPO / "packaging" / "build-requirements.txt"


def _build_wheel(src: Path, out: Path, epoch: str | None) -> Path:
    """The Worker wheel, built with the hash-pinned build backend only (no build isolation, so pip
    never fetches a build tool of its own choosing)."""
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    with tempfile.TemporaryDirectory() as tools:
        _run([*pip, "install", "-q", "--no-deps", "--require-hashes", "--only-binary=:all:", "--target", tools,
              "-r", str(BUILD_REQUIREMENTS)])
        env = {**os.environ, "PYTHONPATH": tools, **({"SOURCE_DATE_EPOCH": epoch} if epoch else {})}
        _run([*pip, "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(out), str(src)], env=env)
    return next(out.glob("nature_ai_worker-*.whl"))


def _committed_source(dest: Path, ref: str = "HEAD", repo: Path = REPO) -> dict:
    commit = _run(["git", "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"]).stdout.strip()
    archive = dest.with_suffix(".tar")
    with open(archive, "wb") as stream:
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], check=True, stdout=stream)
    dest.mkdir()
    _run(["tar", "-xf", str(archive), "-C", str(dest)])
    tag = subprocess.run(["git", "-C", str(repo), "describe", "--tags", "--exact-match", commit],
                         text=True, capture_output=True).stdout.strip()
    return {"repository": OFFICIAL_REPOSITORY, "commit": commit, "tag": tag or None}


def _code_facts(src: Path) -> dict:
    probe = ("import json, sys; sys.path.insert(0, sys.argv[1]);"
             "import nature_worker as n; from nature_worker import migrations as m;"
             "from nature_worker.platform_contract import SUPPORTED_VERSION as v;"
             "print(json.dumps({'version': n.__version__, 'standalone': m.DATA_SCHEMA,"
             "'platform_client': m.PLATFORM_CLIENT_SCHEMA, 'client_interface': v}))")
    return json.loads(_run([sys.executable, "-I", "-c", probe, str(src / "src")]).stdout)


PIP_PLATFORMS = {("windows", "x86_64"): "win_amd64"}


def _target(text: str | None) -> dict:
    if not text:
        return host_target()
    try:
        system, arch, python = text.split("-")
    except ValueError:
        raise SystemExit("--target must be OS-ARCH-PYTHON, e.g. windows-x86_64-3.12") from None
    if (system, arch) not in PIP_PLATFORMS:
        raise SystemExit(f"No pip platform known for {system}-{arch}.")
    return {"os": system, "arch": arch, "python": python}


def read_wheel_hashes(path: Path) -> dict[str, str]:
    """``sha256  file name`` per line (the output of sha256sum); # comments allowed."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            digest, name = line.split(maxsplit=1)
            pins[name.lstrip("*")] = digest.lower()
    return pins


def _download_pinned(pins: dict[str, str], target: dict, wheels: Path, product: Path) -> None:
    """Exactly the pinned wheels for ``target``, each checked against its SHA-256."""
    py = target["python"].replace(".", "")
    for name in sorted(pins):
        dist, version = name.split("-")[:2]
        _run([sys.executable, "-m", "pip", "--disable-pip-version-check", "download", "-q", "--no-deps",
              "--only-binary=:all:", "--platform", PIP_PLATFORMS[(target["os"], target["arch"])],
              "--python-version", target["python"], "--implementation", "cp", "--abi", f"cp{py}",
              "--abi", "abi3", "--abi", "none", "-d", str(wheels), f"{dist}=={version}"])
    got = {w.name for w in wheels.glob("*.whl")} - {product.name}
    if got != set(pins):
        raise SystemExit(f"Downloaded wheels differ from the pinned list: missing {sorted(set(pins) - got)}, "
                         f"unexpected {sorted(got - set(pins))}")
    for name, digest in pins.items():
        if sha256_file(wheels / name) != digest:
            raise SystemExit(f"{name} does not match its pinned SHA-256.")


def _runtime(args, out: Path, target: dict) -> dict | None:
    if not args.runtime:
        return None
    pin = json.loads(args.runtime_pin.read_text(encoding="utf-8"))
    if sha256_file(args.runtime) != pin["sha256"]:
        raise SystemExit(f"{args.runtime} does not match the pinned SHA-256 in {args.runtime_pin}.")
    if pin["python"].rsplit(".", 1)[0] != target["python"]:
        raise SystemExit(f"The runtime is Python {pin['python']}, the target {target['python']}.")
    dest = out / "runtime" / pin["file"].replace("+", "-")
    dest.parent.mkdir()
    dest.write_bytes(args.runtime.read_bytes())
    return {"path": dest.relative_to(out).as_posix(), "sha256": pin["sha256"], "size": dest.stat().st_size,
            "python": pin["python"], "origin": {"url": pin["url"], "sha256": pin["sha256"],
                                                "release": pin["release"]}}


def build(args) -> int:
    key = _load_key(args.private_key)
    target = _target(args.target)
    if args.target and not args.wheel_hashes:
        raise SystemExit("--target needs --wheel-hashes: another platform's wheels are only taken from a pinned list.")
    if bool(args.runtime) != bool(args.runtime_pin):
        raise SystemExit("--runtime and --runtime-pin go together.")
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
        epoch = _commit_epoch(REPO, source["commit"]) if source["commit"] else None
        product = _build_wheel(src, wheels, epoch)
        if args.wheel_hashes:
            _download_pinned(read_wheel_hashes(args.wheel_hashes), target, wheels, product)
        else:
            _run([*pip, "download", "-q", "--only-binary=:all:", "-d", str(wheels),
                  "-r", str(src / "requirements.lock"), str(product)])
    runtime = _runtime(args, out, target)
    notes = out / "RELEASE_NOTES.md"
    notes.write_bytes(args.notes.read_bytes())

    def entry(path: Path) -> dict:
        return {"path": path.relative_to(out).as_posix(), "sha256": sha256_file(path), "size": path.stat().st_size}

    manifest = {
        "schema": 1, "product": PRODUCT, "version": facts["version"],
        "released_at": time.strftime("%Y-%m-%d"), "channel": args.channel,
        "target": target, "editions": ["standalone"] if runtime else ["standalone", "platform_client"],
        "upgrade_from": {"min": args.min_upgrade_from or facts["version"]},
        "data_schema": {"standalone": facts["standalone"], "platform_client": facts["platform_client"]},
        "client_interface": facts["client_interface"],
        "notes": entry(notes),
        "product_wheel": product.relative_to(out).as_posix(),
        "artifacts": [entry(w) for w in sorted(wheels.glob("*.whl"))],
        "source": source,
        **({"runtime": runtime} if runtime else {}),
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    Manifest.parse(raw)  # the same validation an installation applies
    (out / MANIFEST).write_bytes(raw)
    (out / SIGNATURE).write_bytes(sign_manifest(raw, key))
    print(f"Built {out}: {PRODUCT} {facts['version']} for {manifest['target']}, "
          f"{len(manifest['artifacts'])} wheels, manifest sha256 {Manifest.parse(raw).digest}")
    return 0


def verify(args) -> int:
    """Provenance report for a bundle; exit 0 only for a fully verified official release."""
    keys = trusted_keys()
    if args.trust_key:
        official = {k.key_id for k in keys}
        keys += [k for k in load_keys(args.trust_key, "local") if k.key_id not in official]
    report: dict = {"bundle": str(args.bundle), "checks": []}

    def check(name: str, ok: bool | None, detail: str) -> None:
        report["checks"].append({"check": name, "ok": ok, "detail": detail})

    try:
        release = verify_bundle(args.bundle, keys)
    except ReleaseError as exc:
        check("signature_and_integrity", False, str(exc))
        print(json.dumps(report, indent=2))
        return 1
    m = release.manifest
    report.update(version=str(m.version), manifest_sha256=m.digest, source=m.source,
                  signer={"key_id": release.signer.key_id, "scope": release.signer.scope})
    check("signature_and_integrity", True, f"signed by {release.signer.key_id}; "
          f"{len(m.artifacts) + 1 + bool(m.runtime)} files match their SHA-256")
    check("official_signer", release.official, "an official NATURE AI release key" if release.official else
          "NOT an official release key (no official key is published yet, see docs/RELEASES.md)")
    source = m.source or {}
    check("official_repository", source.get("repository") == OFFICIAL_REPOSITORY,
          f"source repository {source.get('repository')!r}")
    tag, commit = source.get("tag"), source.get("commit")
    check("tag_matches_version", tag == f"v{m.version}", f"tag {tag!r}, version {m.version}")
    if args.repo:
        resolved = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "--verify", "-q",
                                   f"refs/tags/{tag}^{{commit}}"], capture_output=True, text=True).stdout.strip()
        check("tag_points_at_commit", bool(commit) and resolved == commit,
              f"{tag} -> {resolved or 'missing in this clone'}; manifest commit {commit}")
        kind = subprocess.run(["git", "-C", str(args.repo), "cat-file", "-t", f"refs/tags/{tag}"],
                              capture_output=True, text=True).stdout.strip()
        signed = kind == "tag" and subprocess.run(["git", "-C", str(args.repo), "verify-tag", f"refs/tags/{tag}"],
                                                  capture_output=True).returncode == 0
        check("signed_tag", signed or None, "the tag's signature verifies" if signed else
              f"{'annotated' if kind == 'tag' else 'lightweight or missing'} tag without a verifiable "
              "signature (recommended, not required)")
        if args.rebuild and commit and resolved == commit:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "src"
                _committed_source(src, commit, args.repo)
                rebuilt = _build_wheel(src, Path(tmp) / "wheel", _commit_epoch(args.repo, commit))
                same = sha256_file(rebuilt) == sha256_file(args.bundle / m.product_wheel)
                check("reproducible_wheel", same or None, "the Worker wheel rebuilt from the tagged commit is "
                      "byte-identical" if same else "the rebuilt Worker wheel differs (build tools or "
                      "environment differ); informational")
    elif tag:
        check("tag_points_at_commit", False, "not checked: pass --repo with a clone that has the release tags")
    report["verified_official_release"] = all(c["ok"] is not False for c in report["checks"])
    print(json.dumps(report, indent=2))
    return 0 if report["verified_official_release"] else 1


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
    p.add_argument("--target", help="another platform, e.g. windows-x86_64-3.12 (needs --wheel-hashes)")
    p.add_argument("--wheel-hashes", type=Path, help="pinned dependency wheels: 'sha256  file name' per line")
    p.add_argument("--runtime", type=Path, help="Python runtime archive to ship (Windows Standalone installer)")
    p.add_argument("--runtime-pin", type=Path, help="JSON pin for --runtime (packaging/windows/runtime.json)")
    p = sub.add_parser("verify")
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--repo", type=Path, help="a git clone that has the release tags")
    p.add_argument("--trust-key", type=Path, help="also accept these locally trusted keys (never official)")
    p.add_argument("--rebuild", action="store_true", help="also check that the Worker wheel is reproducible")
    args = parser.parse_args(argv)
    return {"keygen": keygen, "build": build, "verify": verify}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
