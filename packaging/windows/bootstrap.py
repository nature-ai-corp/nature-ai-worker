# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Run by the Windows installer, with the runtime it just unpacked into its temporary folder.

The installer itself carries no install logic: this script only makes the release's own
upgrader importable and hands over to it, so a new installation and an upgrade go through the
same verified, transactional engine as ``nature-worker-upgrade`` (docs/UPGRADES.md):

    python -I bootstrap.py --bundle DIR --prefix DIR --confirm-version X.Y.Z --log FILE
        [--data-dir DIR --workspace DIR] [--trust-key FILE]

No installation at --prefix: ``install`` (verify the signature and every file, build the
version from the bundled runtime and wheels, check that it starts, then record it). An
installation there: ``upgrade`` (the release must be newer; running work finishes first; data
backed up; rolls back on failure). Only the wheels named in the manifest are used, offline,
each checked against its SHA-256.

Exit codes are the upgrader's: 0 done, 1 failed (rolled back), 2 refused / not eligible,
3 installation error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def upgrader_site(bundle: Path, work: Path) -> Path:
    """The release's own code and dependencies, installed into a private folder (not a venv)."""
    manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    lines = []
    for artifact in manifest["artifacts"]:
        name = Path(artifact["path"]).name
        dist, version = name.split("-")[:2]
        lines.append(f"{dist}=={version} --hash=sha256:{artifact['sha256']}\n")
    requirements = work / "requirements.txt"
    requirements.write_text("".join(lines), encoding="utf-8")
    site = work / "site"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PYTHON"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pip", "install", "--no-index", "--find-links", str(bundle / "wheels"),
         "--only-binary=:all:", "--require-hashes", "--no-cache-dir", "--disable-pip-version-check",
         "--no-input", "-q", "--target", str(site), "-r", str(requirements)],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        raise SystemExit(3)
    return site


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--prefix", type=Path, required=True)
    p.add_argument("--confirm-version", required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--data-dir", type=Path)
    p.add_argument("--workspace", type=Path)
    p.add_argument("--trust-key", type=Path)
    args = p.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    log = open(args.log, "a", encoding="utf-8")
    sys.stdout = sys.stderr = Tee(sys.__stdout__, log) if sys.__stdout__ else log
    upgrade = (args.prefix / "installation.json").exists()
    print(f"Nature AI Worker {args.confirm_version}: {'upgrade' if upgrade else 'new installation'} "
          f"at {args.prefix}")
    # Next to the bundle in the installer's temporary folder, which Setup removes afterwards
    # (the loaded extension modules cannot be deleted while this process runs).
    work = args.bundle.parent / "upgrader"
    work.mkdir(exist_ok=True)
    sys.path.insert(0, str(upgrader_site(args.bundle, work)))
    from nature_worker.upgrade.__main__ import main as upgrader
    if upgrade:
        argv = ["upgrade", "--prefix", str(args.prefix), "--bundle", str(args.bundle),
                "--confirm-version", args.confirm_version]
    else:
        argv = ["install", "--prefix", str(args.prefix), "--bundle", str(args.bundle),
                "--confirm-version", args.confirm_version]
        if args.data_dir:
            argv += ["--data-dir", str(args.data_dir)]
        if args.workspace:
            argv += ["--workspace", str(args.workspace)]
        if args.trust_key:
            argv += ["--trust-key", str(args.trust_key)]
    code = upgrader(argv)
    print(f"Result: exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
