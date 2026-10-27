# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Real Windows Platform Client (S-C) tests: installed service, real processes.

    python scripts\\platform_client_windows_e2e.py --workdir NEW_DIR --archive SRC.tar [--origin URL]

Run in an elevated prompt (administrator) on a Windows computer that has no
Platform Client installed yet. It installs the service from the archive (a
``git archive --format=tar HEAD``), then checks with real processes:

  install (service account, process identity, private data root, admin-only code,
  loopback UI, Standalone folder untouched); repair after permissions and the
  service were damaged; signed upgrade; failed upgrade with automatic rollback;
  refused tampered bundle; upgrader killed during verification plus recover;
  user rollback; uninstall keeping data; reinstall over kept data; uninstall with
  data deletion and clean validation.

The Platform is NOT contacted (default origin https://platform.example.invalid):
these tests cover installation, startup, upgrade, repair and uninstall. Sign-in
and Tasks against the real Platform are a separate, manual check.

Writes NEW_DIR\\windows-platform-client-e2e.md; exits 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from nature_worker.platform_client.windows_setup import (  # noqa: E402
    ACCOUNT, SERVICE_KEY, UNINSTALL_KEY, Setup, WindowsSystem, default_data, default_prefix,
)
from nature_worker.platform_client.winservice import SERVICE_NAME  # noqa: E402
from nature_worker.upgrade.release import public_key_entry  # noqa: E402
from nature_worker.windows_security import (  # noqa: E402
    admin_only_problems, current_token, private_problems, read_acl, service_sid,
)

USERS = "S-1-5-32-545"


class Check(Exception):
    pass


def expect(condition, message: str) -> None:
    if not condition:
        raise Check(message)


class E2E:
    def __init__(self, workdir: Path, archive: Path, origin: str):
        self.w = workdir.resolve()
        self.archive = archive.resolve()
        self.origin = origin
        self.prefix, self.data = default_prefix(), default_data()
        self.system = WindowsSystem()
        self.setup = Setup(self.prefix, self.data, say=self.note)
        self.port = self.free_port()
        self.lines: list[str] = []
        self.results: list[tuple[str, bool]] = []
        self.sid = service_sid(SERVICE_NAME)

    # -- helpers ------------------------------------------------------------------------

    def note(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def run(self, cmd, *, timeout=1800, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)

    def setup_cmd(self, *args) -> subprocess.CompletedProcess:
        """The installer as a user runs it from the checkout (never from inside the installation)."""
        return self.run([sys.executable, "-I", REPO / "scripts" / "platform-client-windows.py", *args])

    def upgrader(self, *args) -> subprocess.CompletedProcess:
        return self.run([self.prefix / "bin" / "nature-worker-upgrade.cmd", *args])

    def status(self) -> dict | None:
        return self.system.http_status(self.port)

    def current(self) -> str:
        return (self.prefix / "current").read_text(encoding="utf-8").strip()

    def history(self) -> list[dict]:
        path = self.prefix / "upgrade" / "history.jsonl"
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def service_process(self) -> dict:
        script = (f"$s = Get-CimInstance Win32_Service -Filter \"Name='{SERVICE_NAME}'\"; "
                  "$p = Get-CimInstance Win32_Process -Filter (\"ProcessId=\" + $s.ProcessId); "
                  "$o = Invoke-CimMethod -InputObject $p -MethodName GetOwner; "
                  "@{state=$s.State; start_name=$s.StartName; pid=$s.ProcessId; "
                  "owner=($o.Domain + '\\' + $o.User); command=$p.CommandLine} | ConvertTo-Json")
        out = self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=120)
        expect(out.returncode == 0, f"cannot inspect the service process: {out.stderr[-300:]}")
        return json.loads(out.stdout)

    def wait_version(self, version: str, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status and status.get("version") == version:
                return status
            time.sleep(1)
        raise Check(f"the service did not answer as {version} (last: {self.status()})")

    def assert_isolated(self) -> None:
        problems = private_problems(read_acl(self.data), self.sid)
        expect(not problems, f"data root not private: {problems}")
        problems = admin_only_problems(read_acl(self.prefix))
        expect(not problems, f"code folder not admin-only: {problems}")
        proc = self.service_process()
        expect(proc["start_name"] == ACCOUNT, f"service account is {proc['start_name']}")
        expect(proc["owner"].lower() == ACCOUNT.lower(), f"service process runs as {proc['owner']}")
        expect("runtime\\python.exe" in proc["command"], f"unexpected service command line {proc['command']}")

    def bundle(self, version: str, *, broken: bool = False) -> Path:
        src = self.w / f"src-{version}"
        shutil.copytree(self.base_src, src)
        init = src / "src" / "nature_worker" / "__init__.py"
        text = init.read_text(encoding="utf-8")
        old = text.split('__version__ = "')[1].split('"')[0]
        init.write_text(text.replace(f'__version__ = "{old}"', f'__version__ = "{version}"'), encoding="utf-8")
        pyproject = src / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace(
            f'version = "{old}"', f'version = "{version}"', 1), encoding="utf-8")
        if broken:
            service = src / "src" / "nature_worker" / "platform_client" / "winservice.py"
            text = service.read_text(encoding="utf-8")
            service.write_text(text.replace("    settings = ClientSettings.from_env()\n",
                                            "    raise SystemExit('e2e: broken on purpose')\n", 1), encoding="utf-8")
        notes = self.w / f"notes-{version}.md"
        notes.write_text(f"# {version} (Windows S-C e2e test build)\n", encoding="utf-8")
        out = self.w / "bundles" / version
        result = self.run([sys.executable, REPO / "scripts" / "release_bundle.py", "build", "--private-key",
                           self.key_file, "--notes", notes, "--out", out, "--src", src,
                           "--min-upgrade-from", "0.0.1"])
        expect(result.returncode == 0, f"bundle {version} failed: {result.stderr[-600:]}")
        return out

    # -- scenarios ----------------------------------------------------------------------

    def scenario(self, name: str, fn) -> None:
        self.lines.append(f"\n## {name}")
        started = time.monotonic()
        try:
            fn()
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            self.note(f"FAILED: {exc}")
            self.lines.append("```\n" + traceback.format_exc()[-2000:] + "```")
        self.lines[self.lines.index(f"\n## {name}")] = f"\n## {'PASS' if ok else 'FAIL'}: {name}"
        self.note(f"({time.monotonic() - started:.1f} s)")
        self.results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)

    def s_preconditions(self) -> None:
        token = current_token()
        expect(token.administrator and token.elevated, "run this in an elevated (administrator) prompt")
        expect(self.system.service_status() is None, f"{SERVICE_NAME} is already installed; use a computer "
               "without a Platform Client")
        expect(not self.prefix.exists() and not self.data.exists(),
               f"{self.prefix} or {self.data} already exists; refusing to touch it")
        self.w.mkdir(parents=True)
        self.base_src = self.w / "source"
        with tarfile.open(self.archive) as t:
            self.commit = t.pax_headers.get("comment", "")
            t.extractall(self.base_src, filter="data")
        expect(len(self.commit) == 40, "the archive does not name its commit")
        key = Ed25519PrivateKey.generate()
        self.key_file = self.w / "test-release-key.pem"
        self.key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        self.key = key
        self.standalone = self.w / "standalone-data"
        self.standalone.mkdir()
        (self.standalone / "worker.db").write_bytes(b"standalone data that S-C must never touch")
        self.note(f"commit {self.commit}; Python {sys.version.split()[0]}; port {self.port}; "
                  f"origin {self.origin}")

    def s_install(self) -> None:
        result = self.setup_cmd("install", "--origin", self.origin, "--standalone-data", self.standalone,
                                "--port", self.port, "--archive", self.archive)
        expect(result.returncode == 0, f"install failed: {result.stdout[-1500:]}{result.stderr[-1500:]}")
        self.label = self.current()
        status = self.wait_version(self.label)
        expect(status["platform"] == self.origin and status["signed_in"] is False, f"status {status}")
        self.assert_isolated()
        expect(self.system.get_registry(UNINSTALL_KEY, "DisplayVersion") == self.label, "Apps & Features entry")
        page = self.run(["powershell", "-NoProfile", "-Command",
                         f"(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:{self.port}/).StatusCode"])
        expect(page.stdout.strip() == "200", "the UI page is not served")
        expect((self.standalone / "worker.db").read_bytes() == b"standalone data that S-C must never touch",
               "the Standalone data changed")
        self.note(f"installed {self.label}; service runs as {ACCOUNT} (process owner checked); data root "
                  "private to the service account; code admin-only; UI answers on loopback; Standalone untouched")

    def s_repair(self) -> None:
        self.run(["icacls", self.data, "/grant", f"*{USERS}:(OI)(CI)R"])
        self.run(["sc.exe", "privs", SERVICE_NAME, "SeChangeNotifyPrivilege/SeImpersonatePrivilege"])
        self.system.stop_service(60)
        expect(private_problems(read_acl(self.data), self.sid), "damage was not applied")
        result = self.setup_cmd("repair")
        expect(result.returncode == 0, f"repair failed: {result.stdout[-1500:]}{result.stderr[-1500:]}")
        self.wait_version(self.label)
        self.assert_isolated()
        privs = self.run(["sc.exe", "qprivs", SERVICE_NAME]).stdout
        expect("SeImpersonatePrivilege" not in privs, "repair did not reset the service privileges")
        self.note("Users read access added, extra privilege granted, service stopped -> repair: data root private "
                  f"again, privileges reset, {self.label} running")

    def s_signed_upgrade(self) -> None:
        trust = {"schema": 1, "keys": [public_key_entry(self.key, "windows e2e test key")]}
        (self.prefix / "trusted-keys.json").write_text(json.dumps(trust), encoding="utf-8")
        self.bundles = {"9.1.0": self.bundle("9.1.0"), "9.2.0": self.bundle("9.2.0", broken=True),
                        "9.3.0": self.bundle("9.3.0")}
        tampered = self.w / "bundles" / "tampered"
        shutil.copytree(self.bundles["9.3.0"], tampered)
        wheel = next((tampered / "wheels").glob("nature_ai_worker-*.whl"))
        content = bytearray(wheel.read_bytes())
        content[len(content) // 2] ^= 0xFF
        wheel.write_bytes(bytes(content))
        self.bundles["tampered"] = tampered
        result = self.upgrader("upgrade", "--bundle", self.bundles["9.1.0"], "--confirm-version", "9.1.0")
        expect(result.returncode == 0, f"upgrade failed: {result.stdout[-1500:]}")
        self.wait_version("9.1.0")
        self.assert_isolated()
        expect(self.history()[-1].get("outcome") == "success", f"history {self.history()[-1]}")
        self.note(f"{self.label} -> 9.1.0 (signed by a locally trusted key): service restarted on 9.1.0, same "
                  "account, data still private, history 'success'")

    def s_failed_upgrade_rolls_back(self) -> None:
        result = self.upgrader("upgrade", "--bundle", self.bundles["9.2.0"], "--confirm-version", "9.2.0")
        expect(result.returncode == 1, f"expected exit 1, got {result.returncode}: {result.stdout[-800:]}")
        expect(self.current() == "9.1.0", f"current is {self.current()}")
        self.wait_version("9.1.0")
        last = self.history()[-1]
        expect(last.get("outcome") == "rolled_back", f"history {last}")
        self.note(f"9.2.0 (service exits at start): exit 1, outcome rolled_back, failed phase "
                  f"{last.get('failed_phase')}; 9.1.0 serving again")

    def s_refused_tampered(self) -> None:
        result = self.upgrader("upgrade", "--bundle", self.bundles["tampered"], "--confirm-version", "9.3.0")
        expect(result.returncode == 2 and "SHA-256" in result.stdout, f"tampered: {result.returncode} {result.stdout[-400:]}")
        expect(self.current() == "9.1.0", "current changed")
        self.wait_version("9.1.0")
        self.note("tampered wheel: exit 2 'does not match its SHA-256'; 9.1.0 kept running")

    def s_killed_during_verification(self) -> None:
        python = self.prefix / "versions" / self.current() / "venv" / "Scripts" / "python.exe"
        proc = subprocess.Popen([str(python), "-I", "-m", "nature_worker.upgrade", "upgrade", "--prefix",
                                 str(self.prefix), "--bundle", str(self.bundles["9.3.0"]), "--confirm-version", "9.3.0"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        state_file = self.prefix / "upgrade" / "state.json"
        try:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                try:
                    if json.loads(state_file.read_text(encoding="utf-8")).get("phase") == "verifying":
                        break
                except (OSError, ValueError):
                    pass
                expect(proc.poll() is None, "the upgrade ended before reaching verifying")
                time.sleep(0.05)
            else:
                raise Check("verifying not reached")
        finally:
            proc.kill()
            proc.wait()
        status = self.upgrader("status")
        expect("UNFINISHED" in status.stdout, "status does not report the unfinished upgrade")
        recovered = self.upgrader("recover", "--confirm")
        expect(recovered.returncode == 0, f"recover failed: {recovered.stdout[-800:]}")
        expect(self.current() == "9.1.0", f"after recover: {self.current()}")
        self.wait_version("9.1.0")
        self.assert_isolated()
        self.note("upgrader killed during 'verifying' -> status UNFINISHED -> recover --confirm: 9.1.0 active "
                  "and serving, permissions intact")
        result = self.upgrader("upgrade", "--bundle", self.bundles["9.3.0"], "--confirm-version", "9.3.0")
        expect(result.returncode == 0, f"upgrade after recovery failed: {result.stdout[-800:]}")
        self.wait_version("9.3.0")
        self.note("upgrade to 9.3.0 after recovery: success")

    def s_user_rollback(self) -> None:
        result = self.run([self.prefix / "bin" / "nature-worker-upgrade.cmd", "rollback", "--confirm"])
        expect(result.returncode == 0, f"rollback failed: {result.stdout[-800:]}")
        expect(self.current() == "9.1.0", f"current {self.current()}")
        self.wait_version("9.1.0")
        self.note("user rollback 9.3.0 -> 9.1.0: service serving 9.1.0")

    def s_uninstall_keeps_data_then_reinstall(self) -> None:
        marker = self.data / "e2e-kept.txt"
        marker.write_text("kept across uninstall", encoding="utf-8")
        result = self.setup_cmd("uninstall")
        expect(result.returncode == 0, f"uninstall failed: {result.stdout[-1500:]}{result.stderr[-800:]}")
        expect(self.system.service_status() is None and not self.prefix.exists(), "service or code left behind")
        expect(self.system.get_registry(UNINSTALL_KEY, "DisplayName") is None, "Apps & Features entry left")
        expect(marker.read_text(encoding="utf-8") == "kept across uninstall", "data was not kept")
        expect("NOT authorized" in result.stdout, "clean validation did not block while data remains")
        self.note("uninstall: service, code and registration removed; data kept; clean validation blocks a "
                  "Standalone reinstall while data remains")
        result = self.setup_cmd("install", "--origin", self.origin, "--standalone-data", self.standalone,
                                "--port", self.port, "--archive", self.archive)
        expect(result.returncode == 0, f"reinstall failed: {result.stdout[-1500:]}")
        self.wait_version(self.current())
        self.assert_isolated()
        expect(marker.exists(), "reinstall lost the kept data")
        self.note("reinstall over the kept data root: running, isolated, data still there")

    def s_uninstall_with_data(self) -> None:
        result = self.setup_cmd("uninstall", "--delete-local-data")
        expect(result.returncode == 0, f"uninstall failed: {result.stdout[-1500:]}{result.stderr[-800:]}")
        expect(not self.data.exists() and not self.prefix.exists(), "files left behind")
        expect(self.system.get_registry(SERVICE_KEY, "Environment") is None, "service registry left behind")
        for check in ("os_credentials", "running_processes", "installed_packages", "other_app_locations"):
            expect(f"{check}: verified clean" in result.stdout, f"{check} not verified clean")
        expect("device_policy: unverified" in result.stdout, "device policy should be reported unverified")
        expect((self.standalone / "worker.db").exists(), "the Standalone data was removed")
        self.note("uninstall --delete-local-data: everything removed; credentials, processes, packages and "
                  "locations verified clean; device policy unverified (by design it blocks an automatic "
                  "Standalone reinstall); Standalone data untouched")

    def cleanup(self) -> None:
        """Leave no service behind even when a scenario failed."""
        if self.system.service_status() is not None:
            self.setup_cmd("uninstall", "--delete-local-data")

    def report(self) -> int:
        passed = sum(ok for _, ok in self.results)
        head = ["# Windows Platform Client e2e report", "",
                f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}", f"- Result: {passed}/{len(self.results)} passed"]
        path = self.w / "windows-platform-client-e2e.md"
        path.write_text("\n".join(head + self.lines) + "\n", encoding="utf-8")
        print(f"{passed}/{len(self.results)} passed; report: {path}")
        return 0 if passed == len(self.results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--origin", default="https://platform.example.invalid")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Windows only.", file=sys.stderr)
        return 2
    if args.workdir.exists():
        print(f"{args.workdir} exists; give a new directory.", file=sys.stderr)
        return 2
    e2e = E2E(args.workdir, args.archive, args.origin)
    e2e.scenario("Preconditions and test material", e2e.s_preconditions)
    if not e2e.results[-1][1]:
        return e2e.report()
    try:
        for name, fn in (
            ("Install: service account, private data, admin-only code, Standalone untouched", e2e.s_install),
            ("Repair after damaged permissions and service configuration", e2e.s_repair),
            ("Signed upgrade", e2e.s_signed_upgrade),
            ("Failed upgrade rolls back automatically", e2e.s_failed_upgrade_rolls_back),
            ("Tampered bundle refused", e2e.s_refused_tampered),
            ("Upgrader killed during verification, recovered", e2e.s_killed_during_verification),
            ("User rollback", e2e.s_user_rollback),
            ("Uninstall keeps data; reinstall over it", e2e.s_uninstall_keeps_data_then_reinstall),
            ("Uninstall with data deletion and clean validation", e2e.s_uninstall_with_data),
        ):
            e2e.scenario(name, fn)
    finally:
        e2e.cleanup()
    return e2e.report()


if __name__ == "__main__":
    sys.exit(main())
