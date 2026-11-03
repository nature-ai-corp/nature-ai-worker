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
NEW_DIR\\e2e-journal.log records, flushed to disk line by line, every scenario and
child process (PID, command, timeout, exit), Python stacks when a step stalls or the
interpreter crashes, and console/shutdown events: if the runner ends without the
line "runner finished", it was stopped from outside (or crashed) and the journal's
last lines say where.

    python scripts\\platform_client_windows_e2e.py --cleanup-leftovers --workdir EARLIER_DIR

removes what a run that ended without its own cleanup left in the e2e namespace
(the same e2e uninstall), refusing while any installer, upgrader or e2e process is
still active; it writes EARLIER_DIR\\windows-platform-client-e2e-cleanup.md.
"""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import hashlib
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from nature_worker.build_identity import build_mismatch  # noqa: E402
from nature_worker.platform_client.windows_setup import (  # noqa: E402
    UNINSTALL_KEY, Setup, WindowsSystem, default_data, default_prefix, start_menu,
)
from nature_worker.platform_client.winservice import SERVICE_NAME  # noqa: E402
from nature_worker.upgrade.release import public_key_entry  # noqa: E402
from nature_worker.windows_security import (  # noqa: E402
    admin_only_problems, current_token, lookup_account_sid, private_problems, read_acl, service_sid, system_tool,
)

USERS = "S-1-5-32-545"
# This test runs elevated: system programs by absolute path only (never a bare name).
POWERSHELL = system_tool("WindowsPowerShell", "v1.0", "powershell.exe")
ICACLS, SC, CMDKEY = system_tool("icacls.exe"), system_tool("sc.exe"), system_tool("cmdkey.exe")
# Everything this test creates lives in its own namespace, never the production one.
E2E_SERVICE = SERVICE_NAME + "E2E"
E2E_PREFIX = default_prefix().parent.parent / "NatureAIWorkerE2E" / "PlatformClient"
E2E_DATA = default_data().parent.parent / "NatureAIWorkerE2E" / "PlatformClient"
# A child running longer than this gets the runner's Python stack written to the journal
# (repeated), so a stall shows exactly which call is waiting.
STALL_DUMP_SECONDS = 300
# Command lines of everything that may change the e2e namespace (cleanup waits for none).
ACTIVE_PATTERN = (r"platform_client_windows_e2e\.py|platform-client-windows\.py|nature_worker\.upgrade|"
                  r"nature-worker-upgrade|nature-worker-platform-client-setup|windows_setup|nwpc-uninstall")
# Credential Manager entries Windows maintains for the signed-in user itself (Microsoft account and
# Web Account Manager single sign-on, Office, OneDrive, Xbox): TokenBroker refreshes them in the
# background at any time (e2e-4952 cleanup: the user's credential file was rewritten mid-uninstall,
# as the Start menu re-indexed). Only a refresh is accepted: the same target before and after with
# changed metadata, recorded with both versions and its impact. Every entry added or removed, of
# these targets too, and every change to any other entry fails the run.
WINDOWS_MANAGED_TARGET = re.compile(
    r"^(?:\w+:target=)?(?:MicrosoftAccount:|SSO_POP_(?:Device|User)|virtualapp/didlogical|WindowsLive:|"
    r"MicrosoftOffice\d*_Data:|OneDrive Cached Credential|XblGrts|XboxLive|Xbl\|)", re.IGNORECASE)
# Outcomes the upgrade engine records in upgrade\history.jsonl (nature_worker/upgrade/engine.py,
# Outcome.outcome): the e2e checks these exact values, never its own words for them.
SUCCEEDED, ROLLED_BACK, ROLLED_BACK_BY_USER = "succeeded", "rolled_back", "rolled_back_by_user"
CONSOLE_EVENTS = {0: "CTRL_C_EVENT (Ctrl+C)", 1: "CTRL_BREAK_EVENT (Ctrl+Break)",
                  2: "CTRL_CLOSE_EVENT (console window closed)", 5: "CTRL_LOGOFF_EVENT (user signs out)",
                  6: "CTRL_SHUTDOWN_EVENT (Windows shuts down or restarts)"}


class Check(Exception):
    pass


def expect(condition, message: str) -> None:
    if not condition:
        raise Check(message)


def credential_entries(listing: str) -> frozenset[tuple[str, str, str, str]]:
    """``cmdkey /list`` as metadata: (target, type, persistence, short hash of the user name).
    cmdkey never prints a secret; the user name is not kept either. Order does not matter.
    Output that cannot be parsed (another display language) is compared as a whole."""
    entries: list[list[str]] = []
    for raw in listing.splitlines():
        line = raw.strip()
        key, colon, value = line.partition(":")
        if colon and key.lower() == "target":
            entries.append([value.strip(), "", "", ""])
        elif entries and colon and key.lower() == "type":
            entries[-1][1] = value.strip()
        elif entries and colon and key.lower() == "user":
            entries[-1][3] = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:8]
        elif entries and line and not colon:
            entries[-1][2] = line  # "Local machine persistence", "Saved for this logon only", ...
    if not entries and listing.strip() and "* NONE *" not in listing:
        return frozenset({("(unparsed cmdkey output)", "", "", hashlib.sha256(listing.encode()).hexdigest()[:8])})
    return frozenset(tuple(e) for e in entries)


def redact(target: str) -> str:
    """Target names as reported: e-mail addresses (account names) left out."""
    return re.sub(r"[^\s:=/]+@[^\s:=/]+", "<account>", target)


def credential_changes(before: str, after: str) -> tuple[list[str], list[str]]:
    """Targets that differ between two listings: (Windows-maintained entries refreshed in place,
    every other change: any entry added or removed, and any other entry changed)."""
    old, new = credential_entries(before), credential_entries(after)
    changed = sorted({e[0] for e in old ^ new})
    kept = {e[0] for e in old} & {e[0] for e in new}
    refreshed = [t for t in changed if t in kept and WINDOWS_MANAGED_TARGET.search(t)]
    return refreshed, [t for t in changed if t not in refreshed]


def describe_change(target: str, before: str, after: str) -> str:
    """One changed target with its metadata before and after (no secret, no user name)."""
    def versions(listing: str) -> str:
        found = sorted(f"[{kind or '?'}, {persist or '?'}, user {user or '-'}]"
                       for t, kind, persist, user in credential_entries(listing) if t == target)
        return " ".join(found) or "absent"
    return f"{redact(target)}: before {versions(before)}, after {versions(after)}"


def service_profile(system, sid: str, service: str) -> Path | None:
    """The profile Windows created for a service's virtual account (on its first start), if any."""
    registered = system.get_registry(rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}",
                                     "ProfileImagePath")
    if registered:
        return Path(os.path.expandvars(registered))
    fallback = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "ServiceProfiles" / service
    return fallback if fallback.is_dir() else None


def profile_credential_files(profile: Path) -> list[str]:
    """Names (never contents) of Credential Manager files stored in a profile."""
    found = []
    for scope in ("Local", "Roaming"):
        folder = profile / "AppData" / scope / "Microsoft" / "Credentials"
        if folder.is_dir():
            found += [str(p) for p in folder.iterdir()]
    return found


class Journal:
    """Append-only log, flushed and synced line by line: it survives a killed runner and a
    power loss, so its last lines say what the runner was doing when it ended."""

    def __init__(self, path: Path):
        self.file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - open for the whole run
        self.lock = threading.Lock()

    def __call__(self, text: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S %z')} [{os.getpid()}] {text}\n"
        # Console events arrive on a thread of their own: never wait long for the main thread.
        locked = self.lock.acquire(timeout=2)
        try:
            self.file.write(line)
            self.file.flush()
            os.fsync(self.file.fileno())
        except (OSError, ValueError):
            pass
        finally:
            if locked:
                self.lock.release()

    def close(self) -> None:
        self.file.close()


class E2E:
    def __init__(self, workdir: Path, archive: Path | None, origin: str,
                 report_name: str = "windows-platform-client-e2e.md"):
        self.w = workdir.resolve()
        self.archive = archive.resolve() if archive else None
        self.origin = origin
        self.prefix, self.data = E2E_PREFIX, E2E_DATA
        self.system = WindowsSystem()
        self.setup = Setup(self.prefix, self.data, service=E2E_SERVICE, say=self.note)
        self.account = self.setup.account
        self.port = self.free_port()
        self.lines: list[str] = []
        self.results: list[tuple[str, bool]] = []
        self.report_path = self.w / report_name
        self.journal = Journal(self.w / "e2e-journal.log")
        self.outcome: dict[str, str] = {}
        self.running: str | None = None
        self.ended_how: str | None = None
        # Set only once the preconditions proved the e2e namespace empty: from then on,
        # anything in it was created by this run and cleanup may remove it.
        self.namespace_ours = False
        self.sid = service_sid(E2E_SERVICE)

    # -- helpers ------------------------------------------------------------------------

    def note(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def run(self, cmd, *, timeout=1800, env=None) -> subprocess.CompletedProcess:
        """subprocess.run, with the child's PID, command, timeout and outcome in the journal.
        Children share the runner's console and process group on purpose: Ctrl+C reaches them
        too, so cleanup never races an installer that is still running."""
        args = [str(c) for c in cmd]
        started = time.monotonic()
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, env=env)
        self.journal(f"child {proc.pid} started (timeout {timeout} s): {subprocess.list2cmdline(args)}")
        faulthandler.dump_traceback_later(STALL_DUMP_SECONDS, repeat=True, file=self.journal.file)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.journal(f"child {proc.pid} killed: still running after {timeout} s")
            raise subprocess.TimeoutExpired(args, timeout, out, err) from None
        except BaseException as exc:
            proc.kill()
            proc.wait()
            self.journal(f"child {proc.pid} killed: the runner was interrupted ({type(exc).__name__})")
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()
        self.journal(f"child {proc.pid} exit {proc.returncode} after {time.monotonic() - started:.1f} s")
        return subprocess.CompletedProcess(args, proc.returncode, out, err)

    def setup_cmd(self, *args) -> subprocess.CompletedProcess:
        """The installer as a user runs it from the checkout (never from inside the installation)."""
        return self.run([sys.executable, "-I", REPO / "scripts" / "platform-client-windows.py", "--prefix",
                         self.prefix, "--data", self.data, "--service", E2E_SERVICE, *args])

    def inspect_service_profile(self) -> dict:
        """The installer's read-only inspection of the e2e service account's Windows profile, which
        Windows keeps after the service is deleted (removed only by an explicit
        ``uninstall --remove-service-profile``, which the e2e never runs)."""
        result = self.setup_cmd("service-profile")
        expect(result.returncode == 0, f"service-profile failed: {result.stdout[-800:]}{result.stderr[-800:]}")
        summary = json.loads(result.stdout)
        self.journal(f"service account profile: {json.dumps(summary)}")
        expect(not summary["credential_files"],
               f"the service account stored Credential Manager files: {summary['credential_files']}")
        expect(not summary["other_data"], f"the service account profile holds files Windows does not create: "
               f"{summary['other_data']}")
        expect(not summary.get("bad_links"), f"the service account profile holds links that are not Windows' own "
               f"junctions: {summary.get('bad_links')}")
        self.note(f"service account profile {summary['path']}: "
                  + (f"kept by Windows ({summary['file_count']} entries, names in e2e-journal.log), no credential "
                     f"files, only Windows-standard files and junctions; DPAPI master keys: {len(summary['dpapi_master_keys'])}; "
                     f"removable on request: {summary['removable']}" if summary["exists"] else "not created"))
        return summary

    def upgrader(self, *args) -> subprocess.CompletedProcess:
        return self.run([self.prefix / "bin" / "nature-worker-upgrade.cmd", *args])

    def status(self) -> dict | None:
        return self.system.http_status(self.port)

    def current(self) -> str:
        return (self.prefix / "current").read_text(encoding="utf-8").strip()

    def history(self) -> list[dict]:
        path = self.prefix / "upgrade" / "history.jsonl"
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def last_upgrade(self, outcome: str, *, to: str | None = None) -> dict:
        """The newest history entry, which must record ``outcome`` (and the version it went to)."""
        history = self.history()
        expect(history, "the upgrade history is empty")
        last = history[-1]
        expect(last.get("outcome") == outcome and (to is None or last.get("to") == to),
               f"history: expected outcome {outcome!r}" + (f" to {to}" if to else "") + f", got {last}")
        return last

    def service_process(self) -> dict:
        script = ("$ProgressPreference = 'SilentlyContinue'; "
                  f"$s = Get-CimInstance Win32_Service -Filter \"Name='{E2E_SERVICE}'\"; "
                  "$p = Get-CimInstance Win32_Process -Filter (\"ProcessId=\" + $s.ProcessId); "
                  "$o = Invoke-CimMethod -InputObject $p -MethodName GetOwner; "
                  "@{state=$s.State; start_name=$s.StartName; pid=$s.ProcessId; "
                  "owner=($o.Domain + '\\' + $o.User); command=$p.CommandLine} | ConvertTo-Json")
        out = self.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=120)
        expect(out.returncode == 0, f"cannot inspect the service process: {out.stderr[-300:]}")
        return json.loads(out.stdout)

    def wait_version(self, version: str, timeout: float = 90) -> dict:
        """The service answers and runs exactly the installed build ``version``."""
        deadline = time.monotonic() + timeout
        answer, why = {}, "no answer"
        while time.monotonic() < deadline:
            answer = self.system.http_probe(self.port)
            if answer.get("json"):
                why = build_mismatch(answer["json"], version)
                if why is None:
                    return answer["json"]
            time.sleep(1)
        raise Check(f"the service did not answer as build {version}: {why}; last answer {answer}")

    def assert_isolated(self) -> None:
        problems = private_problems(read_acl(self.data), self.sid)
        expect(not problems, f"data root not private: {problems}")
        problems = admin_only_problems(read_acl(self.prefix))
        expect(not problems, f"code folder not admin-only: {problems}")
        proc = self.service_process()
        expect(proc["start_name"] == self.account, f"service account is {proc['start_name']}")
        expect(proc["owner"].lower() == self.account.lower(), f"service process runs as {proc['owner']}")
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

    def scenario(self, name: str, fn, needs: tuple[str, ...] = ()) -> None:
        """Run one scenario, unless a scenario it depends on did not pass: then it is recorded as
        SKIPPED (still a failed run), so one broken step does not produce a cascade of noise."""
        missing = [n for n in needs if self.outcome.get(n) != "PASS"]
        if missing:
            self.lines.append(f"\n## SKIPPED: {name}")
            self.note(f"not run: needs {', '.join(missing)}")
            self.results.append((name, False))
            self.outcome[name] = "SKIPPED"
            print(f"[SKIPPED] {name} (needs {', '.join(missing)})", flush=True)
            self.write_report()
            return
        # On disk before the scenario starts: a run that ends inside it shows where it was.
        header = f"\n## RUNNING (not finished): {name}"
        self.lines.append(header)
        self.note(f"started {time.strftime('%H:%M:%S')}; if this is the last section, the runner ended "
                  "inside this scenario: see e2e-journal.log")
        hint = len(self.lines) - 1
        self.running = name
        self.journal(f"scenario started: {name}")
        self.write_report()
        started = time.monotonic()
        try:
            fn()
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            self.failure = f"{type(exc).__name__}: {exc}"
            self.note(f"FAILED: {exc}")
            self.lines.append("```\n" + traceback.format_exc()[-2000:] + "```")
        self.running = None
        self.lines[self.lines.index(header)] = f"\n## {'PASS' if ok else 'FAIL'}: {name}"
        del self.lines[hint]
        self.journal(f"scenario {'PASS' if ok else 'FAIL'}: {name}" + ("" if ok else f": {self.failure}"[:300]))
        self.note(f"({time.monotonic() - started:.1f} s)")
        self.results.append((name, ok))
        self.outcome[name] = "PASS" if ok else "FAIL"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f": {self.failure}"), flush=True)
        self.write_report()

    def production_state(self) -> dict:
        """What a real installation consists of: recorded before and compared after."""
        def acl(path: Path):
            try:
                a = read_acl(path)
                return [a.owner, a.protected, sorted((x.sid, x.mask, x.type, x.flags) for x in a.aces)]
            except Exception:  # noqa: BLE001 - absent or unreadable is recorded as such
                return None
        status = self.system.service_status(SERVICE_NAME)
        return {
            "service": status.name if status else None,
            "uninstall_key": self.system.get_registry(UNINSTALL_KEY, "DisplayVersion"),
            "paths": {str(p): [p.exists(), acl(p)] for p in (
                default_prefix(), default_prefix().parent, default_data(), default_data().parent,
                start_menu() / "Nature AI Worker Platform Client.url",
                Path(os.environ["LOCALAPPDATA"]) / "NatureAIWorker")},
            "credential_manager": self.credential_snapshot(),
        }

    def credential_snapshot(self) -> str:
        """This user's Credential Manager listing; its metadata (never secrets) goes to the journal.
        Taken in the elevated runner: the same user and profile as without elevation, so the same
        persisted entries; entries "saved for this logon only" belong to the elevated logon session."""
        listing = self.run([CMDKEY, "/list"], timeout=60).stdout
        entries = sorted(credential_entries(listing))
        context = ""
        if sys.platform == "win32":
            token = current_token()
            context = f" as {token.user} (elevated {token.elevated})"
        self.journal(f"Credential Manager{context}: {len(entries)} entries: "
                     + "; ".join(f"{redact(t)} [{kind or '?'}, {persist or '?'}, user {user or '-'}]"
                                 for t, kind, persist, user in entries))
        return listing

    def check_credentials(self, after: str) -> None:
        """Nature AI Worker added, removed or changed no Credential Manager entry of this user, and
        its service account stored none in its own profile (which this user's listing cannot show)."""
        before = self.before["credential_manager"]
        refreshed, other = credential_changes(before, after)
        for target in refreshed:
            self.note("Credential Manager: Windows refreshed an entry it maintains for the signed-in account "
                      f"(TokenBroker; Nature AI Worker uses no Credential Manager API): "
                      f"{describe_change(target, before, after)}. Impact: none on the Worker's isolation; the "
                      "entry is in this user's vault, encrypted for this user, which the service account "
                      "cannot open; only this user's sign-in to Microsoft services uses it.")
        expect(not other, "Credential Manager entries of this user were added, removed or changed: "
               + "; ".join(describe_change(t, before, after) for t in other)
               + " (metadata of both listings: e2e-journal.log)")
        profile = service_profile(self.system, self.sid, E2E_SERVICE)
        stored = profile_credential_files(profile) if profile else []
        expect(not stored, f"the service account stored Credential Manager files: {stored}")
        self.note(f"service account profile {profile or '(none created)'}: no Credential Manager files")

    def s_preconditions(self) -> None:
        token = current_token()
        expect(token.administrator and token.elevated, "run this in an elevated (administrator) prompt")
        expect(E2E_SERVICE != SERVICE_NAME and E2E_PREFIX != default_prefix() and E2E_DATA != default_data(),
               "the e2e namespace must differ from the production one")
        expect(self.system.service_status(E2E_SERVICE) is None, f"{E2E_SERVICE} already exists (an earlier "
               "run did not finish): remove it with the e2e uninstall before running again")
        for path in (E2E_PREFIX.parent, E2E_DATA.parent, self.setup.shortcut):
            expect(not path.exists(), f"{path} already exists; refusing to touch it")
        expect(self.system.get_registry(self.setup.uninstall_key, "DisplayName") is None,
               f"HKLM\\{self.setup.uninstall_key} already exists; refusing")
        self.before = self.production_state()
        self.note(f"production state recorded: service {self.before['service'] or 'not installed'}; "
                  + ", ".join(f"{Path(k).name} {'exists' if v[0] else 'absent'}" for k, v in self.before["paths"].items()))
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
        self.namespace_ours = True

    def s_install(self) -> None:
        result = self.setup_cmd("install", "--origin", self.origin, "--standalone-data", self.standalone,
                                "--port", self.port, "--archive", self.archive)
        expect(result.returncode == 0, f"install failed: {result.stdout[-1500:]}{result.stderr[-1500:]}")
        self.label = self.current()
        status = self.wait_version(self.label)
        expect(status["platform"] == self.origin and status["signed_in"] is False, f"status {status}")
        self.assert_isolated()
        expect(self.system.get_registry(self.setup.uninstall_key, "DisplayVersion") == self.label, "Apps & Features entry")
        # In-process, not Invoke-WebRequest: a console-attached PowerShell (progress bar,
        # interactive host) is the step e2e-4952 was still waiting in when Windows shut down.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}/", timeout=15) as page:
            expect(page.status == 200, f"the UI page answers HTTP {page.status}")
        expect((self.standalone / "worker.db").read_bytes() == b"standalone data that S-C must never touch",
               "the Standalone data changed")
        showsid = self.run([SC, "showsid", E2E_SERVICE], timeout=60).stdout
        shown = re.search(r"S-1-5-80(?:-\d+){5}", showsid)
        resolved = lookup_account_sid(self.account)
        expect(shown and shown.group(0) == self.sid == resolved,
               f"service SID: derived {self.sid}, sc showsid {shown and shown.group(0)}, Windows {resolved}")
        expect(self.system.get_registry(self.setup.service_key, "ServiceSidType") == 1, "service SID type")
        expect((self.system.get_registry(self.setup.service_key, "ObjectName") or "").lower() == self.account.lower(),
               "service account registration")
        self.note(f"service SID {self.sid}: derived = sc showsid = LookupAccountName; ServiceSidType unrestricted; "
                  f"ObjectName {self.account}")
        runtime = self.prefix / "runtime"
        version = self.run([runtime / "python.exe", "-I", "-c", "import sys; print(sys.version.split()[0])"],
                           timeout=120)
        expect(version.stdout.strip() == sys.version.split()[0], f"runtime is {version.stdout.strip()}")
        expect(not admin_only_problems(read_acl(runtime)), f"runtime not admin-only: {admin_only_problems(read_acl(runtime))}")
        self.note(f"bundled runtime: {runtime}\\python.exe {version.stdout.strip()}, admin-only")
        stored = [p.name for p in self.data.rglob("*") if p.suffix == ".dpapi" or p.name in ("credentials", "sessions")]
        expect(not stored, f"credential storage found in the data root: {stored}")
        self.check_credentials(self.credential_snapshot())
        self.note("no DPAPI file or credentials folder in the data root; no Credential Manager entry of this "
                  "user or of the service account (the service keeps Platform sessions in memory only)")
        self.note(f"installed {self.label}; service runs as {self.account} (process owner checked); data root "
                  "private to the service account; code admin-only; UI answers on loopback; Standalone untouched")

    def s_repair(self) -> None:
        self.run([ICACLS, self.data, "/grant", f"*{USERS}:(OI)(CI)R"], timeout=120)
        self.run([SC, "privs", E2E_SERVICE, "SeChangeNotifyPrivilege/SeImpersonatePrivilege"], timeout=60)
        self.system.stop_service(E2E_SERVICE, 60)
        expect(private_problems(read_acl(self.data), self.sid), "damage was not applied")
        result = self.setup_cmd("repair")
        expect(result.returncode == 0, f"repair failed: {result.stdout[-1500:]}{result.stderr[-1500:]}")
        self.wait_version(self.label)
        self.assert_isolated()
        privs = self.run([SC, "qprivs", E2E_SERVICE], timeout=60).stdout
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
        expect(result.returncode == 0, f"upgrade failed: {result.stdout[-1500:]}{result.stderr[-1500:]}")
        self.wait_version("9.1.0")
        self.assert_isolated()
        last = self.last_upgrade(SUCCEEDED, to="9.1.0")
        expect(last.get("from") == self.label, f"history: upgraded from {last.get('from')}, not {self.label}")
        self.note(f"{self.label} -> 9.1.0 (signed by a locally trusted key): service restarted on 9.1.0, same "
                  f"account, data still private, history '{SUCCEEDED}' from {self.label}")

    def s_failed_upgrade_rolls_back(self) -> None:
        result = self.upgrader("upgrade", "--bundle", self.bundles["9.2.0"], "--confirm-version", "9.2.0")
        expect(result.returncode == 1, f"expected exit 1, got {result.returncode}: {result.stdout[-800:]}")
        expect(self.current() == "9.1.0", f"current is {self.current()}")
        self.wait_version("9.1.0")
        last = self.last_upgrade(ROLLED_BACK, to="9.2.0")
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
        self.journal(f"child {proc.pid} started (killed once it reaches verifying): upgrade to 9.3.0")
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
            self.journal(f"child {proc.pid} killed on purpose, exit {proc.returncode}")
        status = self.upgrader("status")
        expect("UNFINISHED" in status.stdout, "status does not report the unfinished upgrade")
        # Killed while verifying, the data is already migrated: recovery verifies 9.3.0 again and
        # completes the upgrade if it works (engine.recover, as scripts/upgrade_e2e.py checks on Linux).
        recovered = self.upgrader("recover", "--confirm")
        expect(recovered.returncode == 0, f"recover failed: {recovered.stdout[-800:]}")
        expect(self.current() == "9.3.0", f"after recover: {self.current()}")
        expect(not state_file.exists(), "recovery left upgrade\\state.json behind")
        self.wait_version("9.3.0")
        self.assert_isolated()
        self.last_upgrade(SUCCEEDED, to="9.3.0")
        self.note("upgrader killed during 'verifying' -> status UNFINISHED -> recover --confirm re-verified 9.3.0 "
                  f"and completed the upgrade (history '{SUCCEEDED}'): 9.3.0 serving, permissions intact")

    def s_user_rollback(self) -> None:
        result = self.run([self.prefix / "bin" / "nature-worker-upgrade.cmd", "rollback", "--confirm"])
        expect(result.returncode == 0, f"rollback failed: {result.stdout[-800:]}")
        expect(self.current() == "9.1.0", f"current {self.current()}")
        self.wait_version("9.1.0")
        self.assert_isolated()
        self.last_upgrade(ROLLED_BACK_BY_USER, to="9.1.0")
        self.note(f"user rollback 9.3.0 -> 9.1.0 (history '{ROLLED_BACK_BY_USER}'): service serving 9.1.0, "
                  "permissions intact")

    def s_uninstall_keeps_data_then_reinstall(self) -> None:
        marker = self.data / "e2e-kept.txt"
        marker.write_text("kept across uninstall", encoding="utf-8")
        result = self.setup_cmd("uninstall")
        expect(result.returncode == 0, f"uninstall failed: {result.stdout[-1500:]}{result.stderr[-800:]}")
        expect(self.system.service_status(E2E_SERVICE) is None and not self.prefix.exists(), "service or code left behind")
        expect(self.system.get_registry(self.setup.uninstall_key, "DisplayName") is None, "Apps & Features entry left")
        expect(marker.read_text(encoding="utf-8") == "kept across uninstall", "data was not kept")
        expect("NOT authorized" in result.stdout, "clean validation did not block while data remains")
        self.note("uninstall: service, code and registration removed; data kept; clean validation blocks a "
                  "Standalone reinstall while data remains")
        result = self.setup_cmd("install", "--origin", self.origin, "--standalone-data", self.standalone,
                                "--port", self.port, "--archive", self.archive)
        expect(result.returncode == 0, f"reinstall failed: {result.stdout[-1500:]}{result.stderr[-1500:]}")
        self.wait_version(self.current())
        self.assert_isolated()
        expect(marker.exists(), "reinstall lost the kept data")
        self.note("reinstall over the kept data root: running, isolated, data still there")

    def s_uninstall_with_data(self) -> None:
        result = self.setup_cmd("uninstall", "--delete-local-data")
        expect(result.returncode == 0, f"uninstall failed: {result.stdout[-1500:]}{result.stderr[-800:]}")
        expect(not self.data.exists() and not self.prefix.exists(), "files left behind")
        expect(self.system.get_registry(self.setup.service_key, "Environment") is None, "service registry left behind")
        for check in ("os_credentials", "running_processes", "installed_packages", "other_app_locations"):
            expect(f"{check}: verified clean" in result.stdout,
                   f"{check} not verified clean; uninstall output:\n{result.stdout[-2500:]}")
        expect("device_policy: unverified" in result.stdout, "device policy should be reported unverified")
        expect((self.standalone / "worker.db").exists(), "the Standalone data was removed")
        self.inspect_service_profile()
        self.note("uninstall --delete-local-data: everything removed; credentials, processes, packages and "
                  "locations verified clean; device policy unverified (by design it blocks an automatic "
                  "Standalone reinstall); Standalone data untouched")

    def s_production_untouched(self) -> None:
        after = self.production_state()
        for key in ("service", "uninstall_key"):
            expect(after[key] == self.before[key], f"production {key} changed")
        self.check_credentials(after["credential_manager"])
        for path, state in self.before["paths"].items():
            expect(after["paths"][path] == state, f"{path} changed (existence or permissions)")
        leftovers = self.e2e_leftovers()
        expect(not leftovers, f"e2e leftovers: {leftovers}")
        self.note("production service, registry entry, folders and their permissions, the Standalone folder and "
                  "Credential Manager are exactly as before; no e2e leftovers")

    def e2e_leftovers(self) -> list[str]:
        found = [str(p) for p in (E2E_PREFIX.parent, E2E_DATA.parent, self.setup.shortcut) if p.exists()]
        if self.system.service_status(E2E_SERVICE) is not None:
            found.append(f"service {E2E_SERVICE}")
        for key in (self.setup.uninstall_key, self.setup.service_key):
            if self.system.get_registry(key, "DisplayName") is not None or \
                    self.system.get_registry(key, "Environment") is not None:
                found.append(f"HKLM\\{key}")
        return found

    def uninstall_namespace(self) -> tuple[list[str], subprocess.CompletedProcess, list[str]]:
        """The e2e uninstall: the checkout's installer, in the e2e namespace, deleting its data."""
        leftovers = self.e2e_leftovers()
        self.journal(f"e2e uninstall of {leftovers}")
        result = self.setup_cmd("uninstall", "--delete-local-data")
        for path in (E2E_PREFIX.parent, E2E_DATA.parent):
            if path.exists() and not any(path.iterdir()):
                path.rmdir()
        remaining = self.e2e_leftovers()
        self.journal(f"e2e uninstall exit {result.returncode}; remaining {remaining or 'none'}")
        return leftovers, result, remaining

    def cleanup(self) -> None:
        """Remove everything of the e2e namespace, also after a failed or partial install, and
        never anything that existed before this run (the preconditions refused that case)."""
        if not self.namespace_ours:
            return
        if not self.e2e_leftovers():
            return
        leftovers, result, remaining = self.uninstall_namespace()
        self.lines.append("\n## Cleanup")
        self.note(f"found {leftovers}; e2e uninstall exit {result.returncode}; remaining {remaining or 'none'}")
        if remaining:
            self.lines.append("```\n" + (result.stdout + result.stderr)[-2000:] + "```")
        self.write_report()

    # -- leftovers of a run that ended without its cleanup --------------------------------

    def active_processes(self) -> list[str]:
        """Processes that could still change the e2e namespace: an installer, upgrader or e2e
        runner, or anything running from the e2e code folder. Not counted: this runner with its
        ancestors (the venv launcher, the console) and the e2e service, which the uninstall stops."""
        script = ("$ProgressPreference = 'SilentlyContinue'; "
                  f"$svc = Get-CimInstance Win32_Service -Filter \"Name='{E2E_SERVICE}'\"; "
                  "$procs = @(Get-CimInstance Win32_Process | ForEach-Object { @{pid=[int]$_.ProcessId; "
                  "parent=[int]$_.ParentProcessId; exe=[string]$_.ExecutablePath; cmd=[string]$_.CommandLine} }); "
                  "@{self=$PID; service=[int]$svc.ProcessId; processes=$procs} | ConvertTo-Json -Compress -Depth 4")
        out = self.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=120)
        expect(out.returncode == 0 and out.stdout.strip(), f"cannot list the processes: {out.stderr[-300:]}")
        listing = json.loads(out.stdout)
        procs = {p["pid"]: p for p in listing["processes"]}
        ours, pid = {os.getpid(), listing["self"]}, os.getpid()
        while pid in procs and procs[pid]["parent"] not in ours and procs[pid]["parent"] in procs:
            pid = procs[pid]["parent"]
            ours.add(pid)
        prefix = str(E2E_PREFIX).lower()
        active = []
        for p in procs.values():
            if p["pid"] in ours or (listing["service"] and p["pid"] == listing["service"]):
                continue
            cmd, exe = p["cmd"] or "", (p["exe"] or "").lower()
            if re.search(ACTIVE_PATTERN, cmd, re.IGNORECASE) or exe.startswith(prefix) or prefix in cmd.lower():
                active.append(f"{p['pid']} {cmd}")
        self.journal(f"active installer/upgrader/e2e processes: {active or 'none'} "
                     f"(not counted: this runner {sorted(ours)}, e2e service {listing['service'] or 'not running'})")
        return active

    def s_cleanup_preconditions(self) -> None:
        """Only an e2e installation is there, and nothing is still working on it."""
        token = current_token()
        expect(token.administrator and token.elevated, "run this in an elevated (administrator) prompt")
        self.found = self.e2e_leftovers()
        self.note(f"e2e leftovers: {self.found or 'none'}")
        record = self.prefix / "installation.json"
        if record.is_file():
            recorded = json.loads(record.read_text(encoding="utf-8"))
            expect(recorded.get("service") == E2E_SERVICE and Path(recorded.get("data_dir") or "") == E2E_DATA,
                   f"{record} does not describe the e2e installation: {recorded}")
        expect(not (self.prefix / "upgrade" / "state.json").exists(),
               "an upgrade of the e2e installation is unfinished (upgrade\\state.json): leaving it alone")
        active = self.active_processes()
        expect(not active, "still active (wait until they end, then run this again): " + "; ".join(active))
        self.note("no installer, upgrader or e2e process is active; no unfinished upgrade")
        self.before = self.production_state()
        self.namespace_ours = bool(self.found)

    def s_cleanup_leftovers(self) -> None:
        leftovers, result, remaining = self.uninstall_namespace()
        output = (result.stdout + result.stderr)[-1500:]
        expect(result.returncode == 0 and not remaining,
               f"e2e uninstall exit {result.returncode}; still present: {remaining or 'none'}\n{output}")
        self.note(f"removed {leftovers} with the e2e uninstall (exit 0); nothing of the e2e namespace remains")
        self.inspect_service_profile()

    # -- abnormal termination -----------------------------------------------------------

    def ended(self, how: str) -> None:
        """Record why the runner is ending outside a scenario's own result, with its stacks."""
        self.ended_how = how
        self.journal(f"runner ending: {how}" + (f" (during scenario: {self.running})" if self.running else ""))
        faulthandler.dump_traceback(file=self.journal.file, all_threads=True)
        self.lines.append(f"\n## ENDED ABNORMALLY: {how}")
        if self.running:
            self.note(f"while running: {self.running}; its result was not recorded")
        self.note("stacks of every thread are in e2e-journal.log")
        self.write_report()

    def watch_console(self) -> None:
        """Windows: record Ctrl+C, a closed window, sign-out and shutdown before the runner ends
        (the process is terminated after the handler; nothing else would run). Best effort:
        Windows skips the sign-out and shutdown events for processes that loaded user32."""
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        def handler(event):
            how = f"Windows sent {CONSOLE_EVENTS.get(event, f'console event {event}')}"
            try:
                if event == 0:  # Python turns Ctrl+C into KeyboardInterrupt, recorded where it lands
                    self.journal(how)
                else:
                    self.ended(how)
            except Exception:  # noqa: BLE001 - never block the default handling
                pass
            return False  # continue with the default handling (Python's Ctrl+C, termination)
        self._console_handler = handler  # keep the callback alive
        if not ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleCtrlHandler(handler, True):
            self.journal(f"SetConsoleCtrlHandler failed ({ctypes.get_last_error()})")

    def describe_process(self) -> None:
        """Which processes share the runner's console, and whether it runs inside a job."""
        self.journal(f"runner started: pid {os.getpid()}, parent {os.getppid()}, Python {sys.version.split()[0]}, "
                     f"{sys.executable}; args {sys.argv[1:]}")
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        in_job = wintypes.BOOL()
        if kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
            self.journal(f"runner in a job object: {bool(in_job.value)}")
        pids = (wintypes.DWORD * 64)()
        count = kernel32.GetConsoleProcessList(pids, 64)
        self.journal(f"processes on this console: {list(pids[:min(count, 64)]) if count else 'none (no console)'}")

    def write_report(self) -> None:
        passed = sum(ok for _, ok in self.results)
        head = ["# Windows Platform Client e2e report", "",
                f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}", f"- Result: {passed}/{len(self.results)} passed"]
        # Replaced in one step: a runner killed while writing never leaves half a report.
        tmp = self.report_path.with_name(f"{self.report_path.name}.{threading.get_ident()}.tmp")
        tmp.write_text("\n".join(head + list(self.lines)) + "\n", encoding="utf-8")
        os.replace(tmp, self.report_path)

    def report(self) -> int:
        self.write_report()
        passed = sum(ok for _, ok in self.results)
        print(f"{passed}/{len(self.results)} passed; report: {self.report_path}")
        self.journal(f"result {passed}/{len(self.results)} passed")
        return 0 if self.results and passed == len(self.results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--origin", default="https://platform.example.invalid")
    parser.add_argument("--cleanup-leftovers", action="store_true",
                        help="remove what an earlier run (its --workdir) left in the e2e namespace")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Windows only.", file=sys.stderr)
        return 2
    if args.cleanup_leftovers:
        return cleanup_leftovers(args.workdir, args.origin)
    if args.archive is None:
        parser.error("--archive is required")
    return run(args.workdir, args.archive, args.origin)


@contextlib.contextmanager
def supervised(e2e: E2E):
    """Everything that ends the runner leaves a trace: a native crash (faulthandler), a console
    or shutdown event, an exception escaping the scenarios; and a normal end says so."""
    restore = faulthandler.is_enabled()
    faulthandler.enable(file=e2e.journal.file, all_threads=True)
    e2e.describe_process()
    e2e.watch_console()
    try:
        yield
    except BaseException as exc:
        if not e2e.ended_how:
            e2e.ended(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        e2e.journal("runner finished" if not e2e.ended_how else f"runner ended: {e2e.ended_how}")
        faulthandler.disable()
        if restore and sys.__stderr__ is not None:
            faulthandler.enable(file=sys.__stderr__)
        e2e.journal.close()


def run(workdir: Path, archive: Path, origin: str) -> int:
    if workdir.exists():
        print(f"{workdir} exists; give a new directory.", file=sys.stderr)
        return 2
    # The report directory exists before the first check, so every outcome is recorded.
    workdir.mkdir(parents=True)
    e2e = E2E(workdir, archive, origin)
    with supervised(e2e):
        e2e.scenario("Preconditions and test material", e2e.s_preconditions)
        if not e2e.namespace_ours:
            return e2e.report()
        install = "Install: service account, private data, admin-only code, Standalone untouched"
        upgrade = "Signed upgrade"
        try:
            for name, fn, needs in (
                (install, e2e.s_install, ()),
                ("Repair after damaged permissions and service configuration", e2e.s_repair, (install,)),
                (upgrade, e2e.s_signed_upgrade, (install,)),
                ("Failed upgrade rolls back automatically", e2e.s_failed_upgrade_rolls_back, (upgrade,)),
                ("Tampered bundle refused", e2e.s_refused_tampered, (upgrade,)),
                ("Upgrader killed during verification, recovered", e2e.s_killed_during_verification, (upgrade,)),
                ("User rollback", e2e.s_user_rollback, (upgrade,)),
                ("Uninstall keeps data; reinstall over it", e2e.s_uninstall_keeps_data_then_reinstall, (install,)),
                ("Uninstall with data deletion and clean validation", e2e.s_uninstall_with_data, (install,)),
            ):
                e2e.scenario(name, fn, needs)
        except BaseException as exc:
            e2e.ended(f"{type(exc).__name__}: {exc}")  # recorded before cleanup runs
            raise
        finally:
            e2e.cleanup()
        e2e.scenario("Production installation and Standalone untouched", e2e.s_production_untouched)
        return e2e.report()


def cleanup_leftovers(workdir: Path, origin: str) -> int:
    """After a run that ended without its own cleanup (e2e-4952: Windows shut down): the same
    e2e uninstall, once nothing is still running; the production installation is checked."""
    if not workdir.is_dir():
        print(f"{workdir} is not the folder of an earlier run.", file=sys.stderr)
        return 2
    e2e = E2E(workdir, None, origin, report_name="windows-platform-client-e2e-cleanup.md")
    with supervised(e2e):
        first = "Cleanup preconditions: only e2e leftovers, nothing still active"
        e2e.scenario(first, e2e.s_cleanup_preconditions)
        if e2e.namespace_ours:
            e2e.scenario("E2E uninstall of the leftovers", e2e.s_cleanup_leftovers)
        if e2e.outcome[first] == "PASS":
            e2e.scenario("Production installation and Standalone untouched", e2e.s_production_untouched)
        return e2e.report()


if __name__ == "__main__":
    sys.exit(main())
