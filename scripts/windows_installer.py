# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Stage the Windows Standalone installer (Inno Setup) from the committed HEAD.

    python scripts/windows_installer.py stage --private-key KEY.pem --out DIR
        [--trust-key KEYS.json] [--runtime ARCHIVE] [--notes FILE]
        [--test-version X.Y.Z+label [--test-migration] [--test-broken]]
        [--worktree [--exclude PATH ...]]

Runs on any OS (Linux is fine): the Windows wheels come from the hashed list
packaging/windows/wheels-win_amd64-cp312.sha256 and the runtime from the pin in
packaging/windows/runtime.json, both checked byte for byte. DIR then holds:

    NatureAIWorker.iss, defines.iss   the Inno Setup script and this build's values
    payload.tar                       payload/bundle (the signed release bundle, runtime
                                      included), payload/bootstrap.py, and with --trust-key
                                      payload/trusted-dev-key.json (public key only)
    licenses/                         LICENSE.txt, NOTICE.txt, THIRD_PARTY.md, THIRD_PARTY_LICENSES.txt
    build-info.json                   commit, version, every pinned hash used

    python scripts/windows_installer.py verify-stage --stage DIR [--repo CLONE]

``verify-stage`` checks a staged build by the payload Setup will install: payload.tar against
build-info.json and the PayloadSha256 compiled into defines.iss, then a safe extraction into a
new temporary folder (plain files and folders under payload/ only; no links, devices, absolute
or '..' paths), the manifest, runtime and bootstrap.py against the build records, and
``release_bundle.py verify`` on the extracted bundle. The folder is always removed.

    python scripts/windows_installer.py kit --private-key KEY.pem --trust-key KEYS.json --out DIR
        [--runtime ARCHIVE] [--innosetup EXE] [--worktree [--exclude PATH ...]]

``kit`` builds the native Windows acceptance kit: three staged installers (the
product version, a newer test version with one data migration, and a test
version that fails to start), the pinned Inno Setup and runtime downloads, the
acceptance driver and numbered .cmd steps (see the kit's README.txt).

Compile it on Windows with Inno Setup's ISCC.exe (the acceptance kit does this):
``ISCC.exe DIR\\NatureAIWorker.iss`` writes DIR\\output\\NatureAIWorker-Setup-<version>.exe.

The signing key signs the bundle's manifest and is never copied into DIR. A
development key makes a development build: the installer then trusts that key
locally (its public part), and every check reports the release as NOT official.

``--worktree`` stages the working tree instead (tracked and new files, minus
``--exclude``) and records its git tree id: committing exactly those files later
gives a commit whose tree equals it, which ties tested installers to the commit.

``--test-version`` builds an acceptance-test variant of the same source with a
different version, optionally with one extra data migration or a Worker that
fails to start; such builds say so in their release notes and are never releases.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGING = REPO / "packaging" / "windows"
TARGET = "windows-x86_64-3.12"
BASELINE = '    (1, "Record the v0.1.0 tables as data schema 1", lambda conn: None),\n]'
LICENSE_NAMES = ("license", "licence", "copying", "notice", "authors")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if result.returncode != 0:
        raise SystemExit(f"Failed: {' '.join(map(str, cmd[:4]))}\n{result.stdout[-2000:]}{result.stderr[-2000:]}")
    return result


def runtime_archive(given: Path | None, pin: dict, cache: Path) -> Path:
    path = given or cache / pin["file"]
    if not path.exists():
        print(f"Downloading {pin['url']}")
        with urllib.request.urlopen(pin["url"], timeout=120) as response, open(path, "wb") as out:
            shutil.copyfileobj(response, out)
    if sha256(path) != pin["sha256"]:
        raise SystemExit(f"{path} does not match the pinned SHA-256 {pin['sha256']}.")
    return path


def committed_source(dest: Path) -> str:
    commit = run(["git", "-C", REPO, "rev-parse", "HEAD"]).stdout.strip()
    archive = dest.with_suffix(".tar")
    with open(archive, "wb") as stream:
        subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", commit], check=True, stdout=stream)
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    return commit


def worktree_source(dest: Path, exclude: list[str]) -> str:
    """Copy the tracked and new (not ignored) files, minus ``exclude``; return their git tree id."""
    listed = run(["git", "-C", REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).stdout
    files = sorted({f for f in listed.split("\0") if f and f not in exclude and (REPO / f).is_file()})
    for f in files:
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, dest / f)
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run(["git", "-C", REPO, "add", "--", *files], env=env)
        return run(["git", "-C", REPO, "write-tree"], env=env).stdout.strip()


def make_variant(src: Path, version: str, migration: bool, broken: bool) -> None:
    init = src / "src" / "nature_worker" / "__init__.py"
    old = init.read_text().split('__version__ = "')[1].split('"')[0]
    init.write_text(init.read_text().replace(f'__version__ = "{old}"', f'__version__ = "{version}"'))
    pyproject = src / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace(f'version = "{old}"', f'version = "{version}"', 1))
    if migration:
        path = src / "src" / "nature_worker" / "migrations.py"
        text = path.read_text()
        if BASELINE not in text:
            raise SystemExit("migrations.py baseline entry not found")
        extra = '    (2, "Acceptance test migration", lambda conn: conn.execute("CREATE TABLE acceptance_2 (v INTEGER)")),\n'
        path.write_text(text.replace(BASELINE, BASELINE[:-1] + extra + "]"))
    if broken:
        path = src / "src" / "nature_worker" / "__main__.py"
        text = path.read_text()
        path.write_text(text.replace("def main() -> None:\n",
                                     "def main() -> None:\n    raise SystemExit('acceptance: broken on purpose')\n", 1))


def license_texts(bundle: Path, runtime: Path) -> str:
    """Every license file of what the installer ships: the runtime (CPython's Windows build notice,
    pip inside it and the pip wheel venvs are made with) and every wheel."""
    parts = []

    def wheel_licenses(name: str, data: bytes) -> list[tuple[str, str]]:
        found = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for entry in sorted(z.namelist()):
                head, _, base = entry.rpartition("/")
                if ".dist-info" in head and base.lower().startswith(LICENSE_NAMES):
                    found.append((entry, z.read(entry).decode("utf-8", "replace")))
            if not found:
                meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
                lines = z.read(meta).decode("utf-8", "replace").splitlines()
                declared = [ln for ln in lines if ln.startswith(("License:", "License-Expression:", "Classifier: License"))]
                if not declared:
                    raise SystemExit(f"{name} has no license file or declared license.")
                found.append((meta, "\n".join(declared) + "\n(No license file in the wheel; declared license above.)"))
        return found

    with tarfile.open(runtime, "r:gz") as tar:
        cpython = tar.extractfile("python/LICENSE.txt").read().decode("utf-8", "replace")
        parts.append(("Python runtime: CPython (python-build-standalone build), python/LICENSE.txt", cpython))
        for path in sorted((PACKAGING / "runtime-licenses").glob("LICENSE.*.txt")):
            parts.append((f"Python runtime component: {path.name} (see packaging/windows/runtime-licenses/README.md)",
                          path.read_text(encoding="utf-8", errors="replace")))
        for member in tar.getmembers():
            if member.isfile() and member.name.startswith("python/Lib/ensurepip/_bundled/") and member.name.endswith(".whl"):
                for entry, text in wheel_licenses(member.name, tar.extractfile(member).read()):
                    parts.append((f"{Path(member.name).name} (bundled in the runtime): {entry}", text))
            elif member.isfile() and "/site-packages/" in member.name and ".dist-info/" in member.name \
                    and Path(member.name).name.lower().startswith(LICENSE_NAMES):
                parts.append((f"Runtime site-packages: {member.name}",
                              tar.extractfile(member).read().decode("utf-8", "replace")))
    for wheel in sorted((bundle / "wheels").glob("*.whl")):
        if wheel.name.startswith("nature_ai_worker-"):
            continue
        for entry, text in wheel_licenses(wheel.name, wheel.read_bytes()):
            parts.append((f"{wheel.name}: {entry}", text))
    head = ("Third-party licenses for the Nature AI Worker Windows installer\n"
            "=============================================================\n\n"
            "The installer contains the following third-party software, unmodified: a CPython runtime\n"
            "built by python-build-standalone (https://github.com/astral-sh/python-build-standalone),\n"
            "with the libraries listed in its license file below, and the Python packages listed in\n"
            "THIRD_PARTY.md. Their license texts follow, as shipped by each component.\n\n")
    return head + "".join(f"\n\n{'-' * 78}\n{title}\n{'-' * 78}\n\n{text.strip()}\n" for title, text in parts)


def write_payload_tar(out: Path) -> Path:
    """out/payload as out/payload.tar: sorted, with no owner names or times (reproducible)."""
    tar_path = out / "payload.tar"
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted((out / "payload").rglob("*")):
            info = tar.gettarinfo(path, path.relative_to(out).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if path.is_file():
                with open(path, "rb") as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
    return tar_path


# -- verifying a staged build: exactly the payload Setup will install --------------------------

class StageError(Exception):
    """The staged build is inconsistent or its payload archive is unsafe."""


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def _member_problem(member: tarfile.TarInfo) -> str | None:
    """Why a payload.tar member may not be extracted, or None. Only plain files and folders
    under payload/ with ordinary relative names are allowed: no links, devices or FIFOs, no
    absolute, drive-qualified, backslash, '.' or '..' paths, no Windows device names or
    alternate data streams."""
    if not (member.isreg() or member.isdir()):
        return "is not a regular file or folder (links, devices and other special members are refused)"
    name = member.name
    if not name or "\\" in name or ":" in name or name.startswith("/") or "\0" in name:
        return "has an absolute, drive-qualified, backslash or stream path"
    parts = name.rstrip("/").split("/")
    if parts[0] != "payload":
        return "is outside payload/"
    for part in parts:
        if part in ("", ".", "..") or part != part.rstrip(". ") or part.split(".")[0].lower() in _RESERVED:
            return "has an empty, '.', '..', trailing-dot/space or Windows device-name component"
    return None


def extract_payload(tar_path: Path, dest: Path) -> int:
    """Extract payload.tar into the empty folder ``dest``, refusing the whole archive if any
    member is unsafe. Members are written one by one (never ``extractall``): files are created
    exclusively, so nothing that already exists - including a link - is ever written through,
    and every resolved path must stay inside ``dest``. Returns the number of files written."""
    root = dest.resolve(strict=True)
    if any(root.iterdir()):
        raise StageError(f"{dest} is not empty.")
    with tarfile.open(tar_path, "r:") as tar:
        members = tar.getmembers()
        seen: set[str] = set()
        for member in members:
            problem = _member_problem(member)
            if problem:
                raise StageError(f"payload.tar member {member.name!r} {problem}.")
            key = member.name.rstrip("/").casefold()  # Windows names are case-insensitive
            if key in seen:
                raise StageError(f"payload.tar has the member {member.name!r} more than once.")
            seen.add(key)
        files = 0
        for member in members:
            target = root.joinpath(*member.name.rstrip("/").split("/"))
            if not target.resolve().is_relative_to(root):
                raise StageError(f"payload.tar member {member.name!r} resolves outside the folder.")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.parent.resolve(strict=True).is_relative_to(root):
                    raise StageError(f"payload.tar member {member.name!r} resolves outside the folder.")
                source = tar.extractfile(member)
                with source, open(target, "xb") as stream:
                    shutil.copyfileobj(source, stream)
                files += 1
    return files


def _defines(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[0] == "#define":
            values[parts[1]] = parts[2].strip('"')
    return values


def _remove_tree(path: Path) -> bool:
    """Delete ``path``; a few retries for Windows scanners that briefly hold new files open."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.3)
    return False


def verify_stage(args) -> int:
    """Verify a staged build by the payload Setup will install: payload.tar must match the
    SHA-256 recorded in build-info.json and compiled into defines.iss; it is extracted safely
    into a new temporary folder; its manifest, runtime, bootstrap.py and trust-key file must
    match the build records; and its bundle goes through ``release_bundle.py verify``. The
    temporary folder is always removed. Prints one JSON report.

    Exit 0: consistent and the bundle's signature and every file hash verify (whether the
    signer is official and the tag matches is in ``verify``, for the caller to judge).
    1: the bundle's signature or integrity fails. 2: inconsistent build or unsafe archive.
    3: the temporary folder could not be removed."""
    stage_dir: Path = args.stage
    report: dict = {"stage": str(stage_dir), "checks": []}

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            raise StageError(f"{name}: {detail}")

    work: Path | None = None
    code = 0
    try:
        try:
            info = json.loads((stage_dir / "build-info.json").read_text(encoding="utf-8"))
            defines = _defines(stage_dir / "defines.iss")
        except (OSError, ValueError) as exc:
            raise StageError(f"unreadable build records: {exc}") from exc
        tar_path = stage_dir / "payload.tar"
        if not tar_path.is_file():
            raise StageError("payload.tar is missing.")
        actual = sha256(tar_path)
        report["payload_sha256"] = actual
        check("payload_matches_build_info", actual == info.get("payload_sha256"),
              f"payload.tar {actual}, build-info.json {info.get('payload_sha256')}")
        check("payload_matches_installer", actual == defines.get("PayloadSha256"),
              f"payload.tar {actual}, defines.iss PayloadSha256 {defines.get('PayloadSha256')}")
        work = Path(tempfile.mkdtemp(prefix="nw-stage-verify-", dir=args.work_dir))
        report["work_dir"] = str(work)
        files = extract_payload(tar_path, work)
        check("payload_extracted_safely", True, f"{files} regular files, all inside the temporary folder")
        payload = work / "payload"
        bundle = payload / "bundle"
        manifest_raw = (bundle / "release.json").read_bytes() if (bundle / "release.json").is_file() else b""
        digest = hashlib.sha256(manifest_raw).hexdigest()
        check("manifest_matches_build_info", bool(manifest_raw) and digest == info.get("manifest_sha256"),
              f"release.json {digest if manifest_raw else 'missing'}, build-info.json {info.get('manifest_sha256')}")
        manifest = json.loads(manifest_raw)
        report["manifest"] = manifest
        runtime = manifest.get("runtime") or {}
        runtime_path = bundle.joinpath(*str(runtime.get("path", "")).split("/")) if runtime else None
        check("runtime_matches_installer",
              bool(runtime) and runtime["path"].replace("/", "\\") == defines.get("RuntimePath")
              and runtime["sha256"] == defines.get("RuntimeSha256") and runtime_path.is_file()
              and sha256(runtime_path) == runtime["sha256"],
              f"manifest runtime {runtime.get('path')} {runtime.get('sha256')}, defines.iss "
              f"{defines.get('RuntimePath')} {defines.get('RuntimeSha256')}")
        boot = payload / "bootstrap.py"
        check("bootstrap_matches_source", boot.is_file() and sha256(boot) == sha256(PACKAGING / "bootstrap.py"),
              "payload/bootstrap.py equals packaging/windows/bootstrap.py")
        has_key = (payload / "trusted-dev-key.json").exists()
        check("trust_key_matches_build_info", has_key == bool(info.get("trust_key")),
              f"trusted-dev-key.json {'present' if has_key else 'absent'}, build-info.json trust_key "
              f"{bool(info.get('trust_key'))}")
        leaked = [p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file() and
                  (p.suffix in (".pem", ".key") or b"PRIVATE KEY" in p.read_bytes()[:1 << 20])]
        check("no_private_key", not leaked, f"files that look like private keys: {leaked}" if leaked else "none")
        cmd = [sys.executable, str(REPO / "scripts" / "release_bundle.py"), "verify", "--bundle", str(bundle)]
        if args.repo:
            cmd += ["--repo", str(args.repo)]
        if args.trust_key:
            cmd += ["--trust-key", str(args.trust_key)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            verified = json.loads(result.stdout)
        except ValueError:
            raise StageError(f"release_bundle.py verify gave no report (exit {result.returncode}): "
                             f"{result.stderr.strip()[-500:]}") from None
        report.update(verify=verified, verify_exit=result.returncode)
        integrity = [c for c in verified.get("checks", []) if c.get("check") == "signature_and_integrity"]
        if not (integrity and integrity[0].get("ok") is True):
            report["error"] = "signature_and_integrity: " + (integrity[0]["detail"] if integrity else "not checked")
            code = 1
        else:
            # The signed notes' text (the manifest records only their path and hash, now verified),
            # e.g. for the stage scripts' release-candidate banner check.
            report["notes_text"] = bundle.joinpath(*manifest["notes"]["path"].split("/")).read_text(
                encoding="utf-8", errors="replace")
    except (StageError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        report["error"] = str(exc)
        code = 2
    finally:
        if work is not None:
            report["cleaned_up"] = _remove_tree(work)
            if not report["cleaned_up"]:
                report["error"] = (report.get("error", "") + f" Could not remove {work}.").strip()
                code = code or 3
    print(json.dumps(report, indent=2))
    return code


def stage(args) -> int:
    out: Path = args.out
    if out.exists():
        raise SystemExit(f"{out} already exists.")
    pin = json.loads((PACKAGING / "runtime.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        runtime = runtime_archive(args.runtime, pin, tmp)
        src = tmp / "src"
        if args.worktree:
            base = run(["git", "-C", REPO, "rev-parse", "HEAD"]).stdout.strip()
            commit = f"worktree tree {worktree_source(src, args.exclude)} (on {base[:12]})"
        else:
            commit = committed_source(src)
        version = args.test_version
        notes = tmp / "notes.md"
        if version:
            make_variant(src, version, args.test_migration, args.test_broken)
            notes.write_text(f"# Nature AI Worker {version} (ACCEPTANCE TEST BUILD)\n\nBuilt from {commit} with "
                             f"the version changed{'; one extra data migration' if args.test_migration else ''}"
                             f"{'; the Worker fails to start on purpose' if args.test_broken else ''}. "
                             "Not a release; for installer acceptance tests only.\n")
        else:
            notes.write_bytes(args.notes.read_bytes() if args.notes else
                              f"# Nature AI Worker (development build of {commit})\n\nNot an official release.\n".encode())
        payload = out / "payload"
        bundle = payload / "bundle"
        payload.mkdir(parents=True)
        cmd = [sys.executable, REPO / "scripts" / "release_bundle.py", "build", "--private-key", args.private_key,
               "--notes", notes, "--out", bundle, "--target", TARGET,
               "--wheel-hashes", PACKAGING / "wheels-win_amd64-cp312.sha256",
               "--runtime", runtime, "--runtime-pin", PACKAGING / "runtime.json"]
        if version or args.worktree:
            cmd += ["--src", src]
        if args.min_upgrade_from:
            cmd += ["--min-upgrade-from", args.min_upgrade_from]
        elif version:
            cmd += ["--min-upgrade-from", "0.1.0"]
        print(run(cmd).stdout.strip())
        licenses = out / "licenses"
        licenses.mkdir()
        (licenses / "THIRD_PARTY_LICENSES.txt").write_text(license_texts(bundle, runtime), encoding="utf-8")
    shutil.copyfile(PACKAGING / "bootstrap.py", payload / "bootstrap.py")
    if args.trust_key:
        shutil.copyfile(args.trust_key, payload / "trusted-dev-key.json")
    for name, target in (("LICENSE", "LICENSE.txt"), ("NOTICE", "NOTICE.txt"), ("THIRD_PARTY.md", "THIRD_PARTY.md")):
        shutil.copyfile(REPO / name, licenses / target)
    manifest_raw = (bundle / "release.json").read_bytes()
    manifest = json.loads(manifest_raw)
    for path in (p for p in out.rglob("*") if p.is_file()):
        if path.suffix in (".pem", ".key") or b"PRIVATE KEY" in path.read_bytes()[:1 << 20]:
            raise SystemExit(f"Refusing: {path} looks like a private key; nothing may ship one.")
    tar_path = write_payload_tar(out)
    shutil.rmtree(payload)
    v = manifest["version"]
    numeric = ".".join(v.split("+")[0].split(".") + ["0"])
    defines = {
        "AppVersion": v, "AppVersionNumeric": numeric, "FileLabel": v.replace("+", "-"),
        "PayloadSha256": sha256(tar_path), "RuntimePath": manifest["runtime"]["path"].replace("/", "\\"),
        "RuntimeSha256": manifest["runtime"]["sha256"],
    }
    (out / "defines.iss").write_text("".join(f'#define {k} "{val}"\n' for k, val in defines.items())
                                     + f"#define TrustKey {1 if args.trust_key else 0}\n", encoding="utf-8")
    shutil.copyfile(PACKAGING / "NatureAIWorker.iss", out / "NatureAIWorker.iss")
    info = {"version": v, "commit": commit, "test_build": bool(version), "payload_sha256": defines["PayloadSha256"],
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "min_upgrade_from": manifest["upgrade_from"]["min"],
            "runtime": pin, "wheels": len(manifest["artifacts"]), "trust_key": bool(args.trust_key)}
    (out / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")
    print(f"Staged {out}: Nature AI Worker {v} from {commit}, payload sha256 {defines['PayloadSha256']}")
    return 0


KIT_README = r"""Nature AI Worker - Windows Standalone installer acceptance kit
=============================================================

Built from: {source}
Nothing here needs administrator rights. Everything is installed for the current user only.

1-build-installers.cmd   Checks the pinned Inno Setup download (SHA-256), installs it for this
                         user into tools\inno (build tool only) and compiles the three installers
                         into installers\, with installers\SHA256SUMS.txt.
2-acceptance.cmd         Automated native test (about 20-40 minutes). Installs into
                         %LOCALAPPDATA%\NatureAIWorkerAcceptance and uses the real Start-menu
                         folder and Apps & Features entry, so it refuses to run if Nature AI
                         Worker Standalone is already installed for this user. Uses the Ollama
                         host {ollama}. Leaves 0.1.0 installed for step 3.
3-UI-CHECKLIST.txt       Manual UI acceptance in the browser (Chat, Files, Tools, Approvals, Stop,
                         replace_text, and DeepSeek with your own key). Write the results into
                         results\ui-checklist-results.txt.
4-finish.cmd             DeepSeek check (if you configured it in step 3; the key is never read),
                         explicit data removal, final uninstall and the residue check.
5-remove-build-tools.cmd Uninstalls the per-user Inno Setup from tools\inno.

Results: results\acceptance-*.md and .json, results\logs-*. Send the results folder back.
Installers: installers\*.exe - DEVELOPMENT builds, unsigned (no Authenticode), signed release
manifest by a development key that is NOT an official NATURE AI key. Windows SmartScreen may warn.
Test builds 0.1.1+acceptance and 0.1.2+broken exist only for upgrade and rollback tests.
"""

KIT_BUILD = r"""@echo off
rem Compile the three installers with the pinned Inno Setup (per-user, build tool only).
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "if ((Get-FileHash -Algorithm SHA256 'tools\{inno_file}').Hash.ToLower() -ne '{inno_sha}') {{ exit 1 }}"
if errorlevel 1 (echo tools\{inno_file} does not match its pinned SHA-256. & exit /b 1)
if not exist "tools\inno\ISCC.exe" (
  echo Installing Inno Setup {inno_version} for this user into tools\inno ...
  start "" /wait "tools\{inno_file}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /NOICONS /DIR="%~dp0tools\inno" /LOG="%~dp0tools\inno-setup.log"
)
if not exist "tools\inno\ISCC.exe" (echo ISCC.exe not found; see tools\inno-setup.log & exit /b 1)
rem Never mix installers from different builds: start from an empty folder.
if exist installers rmdir /s /q installers
mkdir installers
for %%V in (A B C) do (
  echo Compiling stage\%%V ...
  "tools\inno\ISCC.exe" /Q "stage\%%V\NatureAIWorker.iss" > "stage\%%V\iscc.log" 2>&1
  if errorlevel 1 (
    type "stage\%%V\iscc.log"
    echo BUILD FAILED compiling stage\%%V. No installers were produced; send stage\%%V\iscc.log back.
    rmdir /s /q installers
    exit /b 1
  )
  copy /y "stage\%%V\output\*.exe" installers\ >nul
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem installers\*.exe | Sort-Object Name | ForEach-Object {{ (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower() + '  ' + $_.Name }} | Set-Content -Encoding ascii installers\SHA256SUMS.txt"
if errorlevel 1 (echo BUILD FAILED writing installers\SHA256SUMS.txt. & exit /b 1)
type installers\SHA256SUMS.txt
echo Done. Next: 2-acceptance.cmd
"""

KIT_PYTHON = r"""if not exist "tools\py\python\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "if ((Get-FileHash -Algorithm SHA256 'tools\{runtime_file}').Hash.ToLower() -ne '{runtime_sha}') {{ exit 1 }}"
  if errorlevel 1 (echo tools\{runtime_file} does not match its pinned SHA-256. & exit /b 1)
  mkdir tools\py
  "%SystemRoot%\System32\tar.exe" -xzf "tools\{runtime_file}" -C "tools\py" || exit /b 1
)
"""

KIT_ACCEPT = r"""@echo off
rem Automated native acceptance. No administrator rights; see README.txt.
setlocal
cd /d "%~dp0"
{python}"tools\py\python\python.exe" -I windows_standalone_acceptance.py auto --kit "%~dp0." --ollama {ollama} --model {model}
if errorlevel 1 (
  echo.
  echo ACCEPTANCE DID NOT PASS. See the newest report in results\. Do not continue with the
  echo UI checklist; send the results folder back instead.
  exit /b 1
)
echo.
echo Automated acceptance passed. Next: 3-UI-CHECKLIST.txt, then 4-finish.cmd
"""

KIT_FINISH = r"""@echo off
rem Cloud check (if configured in the UI), data removal, final uninstall, residue check.
setlocal
cd /d "%~dp0"
{python}"tools\py\python\python.exe" -I windows_standalone_acceptance.py finish --kit "%~dp0."
if errorlevel 1 (
  echo.
  echo FINISH DID NOT PASS. See the newest report in results\ and send the results folder back.
  exit /b 1
)
echo.
echo Finished. Send the results folder back, then run 5-remove-build-tools.cmd.
"""

KIT_TOOLS = r"""@echo off
rem Remove the per-user Inno Setup used to compile the installers.
cd /d "%~dp0"
if exist "tools\inno\unins000.exe" start "" /wait "tools\inno\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
echo Done.
"""

KIT_UI = r"""Nature AI Worker - manual UI acceptance (after 2-acceptance.cmd, before 4-finish.cmd)
===================================================================================

2-acceptance.cmd left Nature AI Worker 0.1.0 installed for you, configured for Ollama, with a few
test conversations and files. Copy this list to results\ui-checklist-results.txt and mark each
line PASS or FAIL, with a note for anything unexpected. Screenshots welcome (results\).

[ ] 1  Start menu > Nature AI Worker > Nature AI Worker: a console window opens and the browser
       opens the app by itself (note the address).
[ ] 2  Start it again from the Start menu while it runs: the browser opens the same address; no
       second Worker starts.
[ ] 3  Settings: provider selection shows Ollama as configured. Switch to DeepSeek, paste YOUR key
       (it is stored encrypted; never send it to anyone), Test connection, Save.
[ ] 4  Chat: ask a question; the answer appears.
[ ] 5  Files and tools: "List my workspace files and summarize sales.csv". The tool calls show in
       the conversation and the answer has the right totals (North 170, South 90).
[ ] 6  replace_text: "Create notes.md with the line 'Meeting on Tuesday at 10'", then "In notes.md
       change Tuesday to Wednesday". Open the replace_text tool details: they show Replaced/With;
       no approval was asked; the file has only that change (open it in the workspace folder).
[ ] 7  Approvals: "In plan.md replace 'Plan for Q2' with 'Plan for Q3'". An approval card shows the
       old and new text. Deny: the file is unchanged. Ask again and Approve: it changes.
[ ] 8  Stop: "Write a long handbook about our products in ten sections, one file per section."
       Press Stop after the first file appears: the run stops; files already written are kept.
[ ] 9  Close the console window: the app stops (the page no longer answers). Start it again from
       the Start menu: your conversations are still there.
[ ] 10 Start menu > Nature AI Worker > Licenses: the folder opens with LICENSE.txt, NOTICE.txt,
       THIRD_PARTY.md and THIRD_PARTY_LICENSES.txt.
[ ] 11 Settings > Apps > Installed apps: "Nature AI Worker" 0.1.0 is listed (do not uninstall it
       here; 4-finish.cmd does).

Leave the app closed, then run 4-finish.cmd.
"""


# The acceptance upgrade chain A -> B -> C. B migrates the data to schema 2, so C carries the same
# migration: a release that does not know the data's schema is (rightly) not eligible, and C must
# install and then fail to start, to test the automatic rollback.
KIT_VARIANTS = (("A", ["--test-version", "0.1.0"]),
                ("B", ["--test-version", "0.1.1+acceptance", "--test-migration"]),
                ("C", ["--test-version", "0.1.2+broken", "--test-migration", "--test-broken"]))

KIT_FILES = ("README.txt", "1-build-installers.cmd", "2-acceptance.cmd", "3-UI-CHECKLIST.txt", "4-finish.cmd",
             "5-remove-build-tools.cmd", "windows_standalone_acceptance.py", "acceptance.py")


def kit(args) -> int:
    out: Path = args.out
    if out.exists():
        if not args.refresh:
            raise SystemExit(f"{out} already exists (--refresh rebuilds a kit in place).")
        if not (out / "windows_standalone_acceptance.py").is_file():
            raise SystemExit(f"{out} is not an acceptance kit; refusing to refresh it.")
        # Replaced: the staged installers, compiled installers and scripts. Kept: tools\ (the
        # per-user Inno Setup installation, registered in Apps & Features, and the unpacked runtime;
        # the pinned downloads are checked again below) and results\ (evidence of earlier runs).
        for name in KIT_FILES:
            (out / name).unlink(missing_ok=True)
        for folder in ("stage", "installers"):
            shutil.rmtree(out / folder, ignore_errors=True)
    pin = json.loads((PACKAGING / "runtime.json").read_text())
    inno = json.loads((PACKAGING / "innosetup.json").read_text())
    (out / "tools").mkdir(parents=True, exist_ok=True)
    runtime = runtime_archive(args.runtime, pin, out / "tools")
    if runtime.parent != out / "tools":
        shutil.copyfile(runtime, out / "tools" / pin["file"])
    installer = args.innosetup or out / "tools" / inno["file"]
    if not installer.exists():
        with urllib.request.urlopen(inno["url"], timeout=120) as response, open(installer, "wb") as f:
            shutil.copyfileobj(response, f)
    if sha256(installer) != inno["sha256"]:
        raise SystemExit(f"{installer} does not match the pinned Inno Setup SHA-256.")
    if installer.parent != out / "tools":
        shutil.copyfile(installer, out / "tools" / inno["file"])
    common = ["--private-key", str(args.private_key), "--trust-key", str(args.trust_key),
              "--runtime", str(out / "tools" / pin["file"])]
    if args.worktree:
        common += ["--worktree", *sum((["--exclude", e] for e in args.exclude), [])]
    for label, extra in KIT_VARIANTS:
        main(["stage", *common, "--out", str(out / "stage" / label), *extra])
    shutil.copyfile(REPO / "scripts" / "windows_standalone_acceptance.py", out / "windows_standalone_acceptance.py")
    shutil.copyfile(REPO / "scripts" / "acceptance.py", out / "acceptance.py")
    source = json.loads((out / "stage" / "A" / "build-info.json").read_text())["commit"]
    python = KIT_PYTHON.format(runtime_file=pin["file"], runtime_sha=pin["sha256"])
    files = {
        "README.txt": KIT_README.format(source=source, ollama=args.ollama),
        "1-build-installers.cmd": KIT_BUILD.format(inno_file=inno["file"], inno_sha=inno["sha256"],
                                                   inno_version=inno["version"]),
        "2-acceptance.cmd": KIT_ACCEPT.format(python=python, ollama=args.ollama, model=args.model),
        "3-UI-CHECKLIST.txt": KIT_UI,
        "4-finish.cmd": KIT_FINISH.format(python=python),
        "5-remove-build-tools.cmd": KIT_TOOLS,
    }
    assert set(files) | {"windows_standalone_acceptance.py", "acceptance.py"} == set(KIT_FILES)
    for name, text in files.items():
        (out / name).write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    print(f"Kit ready in {out} (source {source}). On Windows: 1-build-installers.cmd, 2-acceptance.cmd, ...")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("stage")
    p.add_argument("--private-key", type=Path, required=True, help="Ed25519 key that signs the bundle (never copied)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--trust-key", type=Path, help="public key list the installed Worker trusts locally (dev builds)")
    p.add_argument("--runtime", type=Path, help="local copy of the pinned runtime archive (else downloaded)")
    p.add_argument("--notes", type=Path, help="release notes (default: a development-build note)")
    p.add_argument("--min-upgrade-from", help="oldest installed version eligible for this staged upgrade")
    p.add_argument("--test-version", help="acceptance-test variant with this version")
    p.add_argument("--test-migration", action="store_true", help="test variant: one extra data migration")
    p.add_argument("--test-broken", action="store_true", help="test variant: the Worker fails to start")
    p.add_argument("--worktree", action="store_true", help="stage the working tree, recording its git tree id")
    p.add_argument("--exclude", action="append", default=[], help="with --worktree: leave out this path")
    p = sub.add_parser("kit")
    p.add_argument("--private-key", type=Path, required=True)
    p.add_argument("--trust-key", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--runtime", type=Path)
    p.add_argument("--innosetup", type=Path, help="local copy of the pinned Inno Setup installer")
    p.add_argument("--ollama", required=True, help="Ollama URL the Windows computer reaches for real-model checks")
    p.add_argument("--model", default="qwen3.8:27b")
    p.add_argument("--worktree", action="store_true")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--refresh", action="store_true", help="rebuild an existing kit, keeping tools\\ and results\\")
    p = sub.add_parser("verify-stage", help="verify a staged build by extracting payload.tar safely")
    p.add_argument("--stage", type=Path, required=True, help="the folder written by 'stage'")
    p.add_argument("--repo", type=Path, help="a git clone with the release tags (for the tag checks)")
    p.add_argument("--trust-key", type=Path, help="tests only: also accept these keys (never official)")
    p.add_argument("--work-dir", type=Path, help="parent of the temporary folder (default: system temp)")
    args = parser.parse_args(argv)
    return {"stage": stage, "kit": kit, "verify-stage": verify_stage}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
