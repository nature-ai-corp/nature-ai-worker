# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Native Windows acceptance of the Standalone installer (run from the acceptance kit).

    python windows_standalone_acceptance.py auto   --kit DIR [--ollama URL --model NAME]
    python windows_standalone_acceptance.py finish --kit DIR

Standard library only; run with any Python (the kit uses the pinned runtime it carries,
unpacked into its own folder). Everything is installed as the current user, without
administrator rights, into a test namespace:

    %LOCALAPPDATA%\\NatureAIWorkerAcceptance\\{app,data,workspace}

The installers are the real ones (same AppId, Start-menu folder and Apps & Features entry
as a normal installation), so the run refuses to start if Nature AI Worker Standalone is
already installed for this user. Nothing outside the namespace, the Start-menu folder and
the Apps & Features entry is changed; the run compares a snapshot taken before with one
taken after.

``auto``: fresh install A, launch, first-run configuration, real-model checks, refused and
confirmed upgrade to B (while the Worker runs), failed upgrade to C rolled back
automatically, refused reinstall and downgrade, user rollback to A, uninstall refused while
running, uninstall keeping data, reinstall adopting the data, S-C data refused. Leaves A
installed for the manual UI checklist.

``finish``: records the cloud (DeepSeek) check if it was configured in the UI, then the
explicit data removal, the final uninstall and the residue comparison.

Report: <kit>\\results\\acceptance-<time>.md (and .json). API keys are never read, printed
or written: the cloud check uses the key the user stored in the app.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = "{6F3C2B1E-8D4A-4E7B-9C52-1A0E7D3F5B94}"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
SETTINGS_KEY = r"Software\NatureAIWorker\Standalone"
LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
NS = LOCAL / "NatureAIWorkerAcceptance"
APP, DATA, WS = NS / "app", NS / "data", NS / "workspace"
START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Nature AI Worker"
PRODUCTION = [LOCAL / "Programs" / "NatureAIWorker", LOCAL / "NatureAIWorker" / "installation.json",
              LOCAL / "NatureAIWorker" / "data"]
PLATFORM_CLIENT = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NatureAIWorker" / "PlatformClient",
                   Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "NatureAIWorker" / "PlatformClient"]


REQUIRED_INSTALLERS = ("0.1.0", "0.1.1-acceptance", "0.1.2-broken")
# Requests the Worker answers at once are bounded tightly; only one that runs the model gets a whole run.
HEALTH_TIMEOUT = 5
REQUEST_TIMEOUT = 60
RUN_TIMEOUT = 3600
HARNESS_TIMEOUT = 7200   # acceptance.py C, P, F, U, S, E on the installed code


class Fail(Exception):
    pass


def load_installers(kit: Path) -> dict[str, tuple[Path, str]]:
    """The kit's compiled installers from installers\\SHA256SUMS.txt (written by 1-build-installers.cmd).
    Missing or incomplete: Fail with what to do, before anything is installed."""
    sums = kit / "installers" / "SHA256SUMS.txt"
    if not sums.is_file():
        raise Fail(f"{sums} does not exist: the installers were not built. Run 1-build-installers.cmd first "
                   "and check that it ends with 'Done'.")
    found = {}
    for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or "-Setup-" not in parts[1]:
            continue
        digest, name = parts
        found[name.split("-Setup-", 1)[1].removesuffix(".exe")] = (kit / "installers" / name, digest.lower())
    missing = [v for v in REQUIRED_INSTALLERS if v not in found or not found[v][0].is_file()]
    if missing:
        raise Fail(f"Installers missing from {kit / 'installers'}: {', '.join(missing)}. "
                   "Run 1-build-installers.cmd again and check that it ends with 'Done'.")
    return found


def expect(ok: bool, message: str) -> None:
    if not ok:
        raise Fail(message)


def kill_tree(proc: subprocess.Popen) -> None:
    """End a process this run started, with its children: only that tree, by the PID of a process
    the run still holds (so the PID cannot belong to anything else)."""
    if proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass


def run_logged(cmd: list[str], log: Path, timeout: float, poll: float = 1.0) -> tuple[int | None, list[str]]:
    """Run cmd with its output written to log as it happens (never into an unread pipe), printing its
    [START]/[PASS]/[FAIL]/[ERROR]/[NOT EXECUTED] lines as they appear. Returns (exit code, those
    lines); the exit code is None if it ran past timeout, when its process tree is ended."""
    lines: list[str] = []
    pending = b""

    def take(data: bytes, final: bool) -> bytes:
        *complete, rest = data.split(b"\n")
        if final and rest:
            complete, rest = complete + [rest], b""
        for raw in complete:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if line.startswith("["):
                lines.append(line)
                print(f"    {line[:300]}", flush=True)
        return rest

    with open(log, "wb") as out, open(log, "rb") as follow:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        deadline = time.monotonic() + timeout
        while True:
            done = proc.poll() is not None   # before reading: everything it wrote is then in the file
            pending = take(pending + follow.read(), final=done)
            if done:
                return proc.returncode, lines
            if time.monotonic() > deadline:
                kill_tree(proc)
                take(pending + follow.read(), final=True)
                return None, lines
            time.sleep(poll)


def harness_problem(code: int | None, lines: list[str], timeout: float) -> str | None:
    """None if acceptance.py passed; otherwise what failed, naming the scenario."""
    bad = [ln for ln in lines if ln.startswith(("[FAIL]", "[ERROR]"))]
    if code == 0 and lines and not bad:
        return None
    starts = [i for i, ln in enumerate(lines) if ln.startswith("[START] ")]
    last = lines[starts[-1]].split()[1:] if starts else []
    unfinished = bool(starts) and not any(ln.startswith(("[PASS]", "[FAIL]", "[ERROR]")) for ln in lines[starts[-1]:])
    where = (f"scenario {last[0]} (started {last[1] if len(last) > 1 else '?'}) has no result"
             if unfinished else f"last scenario started: {last[0]}" if last else "no scenario started")
    if code is None:
        head = f"acceptance.py gave no result within {timeout:.0f} s and was stopped; {where}"
    elif bad:
        head = "failed: " + "; ".join(ln.split(":", 1)[0].strip("[").replace("] ", " ") for ln in bad)
    else:
        head = f"acceptance.py exit {code}; {where}"
    return head + "\n" + "\n".join(lines)


class Run:
    def __init__(self, kit: Path, phase: str, installers: dict[str, tuple[Path, str]]):
        self.kit = kit
        self.results = kit / "results"
        self.results.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.report = self.results / f"acceptance-{phase}-{stamp}.md"
        self.logs = self.results / f"logs-{phase}-{stamp}"
        self.logs.mkdir()
        self.rows: list[dict] = []
        self.worker: subprocess.Popen | None = None
        self.port = 0
        self.installers = installers
        self.write(f"# Windows Standalone installer acceptance ({phase})\n\nStarted {time.strftime('%Y-%m-%d %H:%M:%S %z')} "
                   f"on {os.environ.get('COMPUTERNAME', '?')} as {os.environ.get('USERNAME', '?')}; "
                   f"Windows {sys.getwindowsversion().build if hasattr(sys, 'getwindowsversion') else '?'}; "
                   f"driver Python {sys.version.split()[0]}.\n\nInstallers (SHA-256):\n\n"
                   + "".join(f"- `{p.name}` `{d}`\n" for p, d in self.installers.values()) + "\n")

    # -- report -------------------------------------------------------------------------
    def write(self, text: str) -> None:
        with open(self.report, "a", encoding="utf-8") as f:
            f.write(text)

    def step(self, name: str, fn) -> bool:
        print(f"[START] {name} {time.strftime('%H:%M:%S')}", flush=True)
        started = time.monotonic()
        try:
            detail = fn() or ""
            status = "PASS"
        except Fail as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded, the run continues where it safely can
            status, detail = "ERROR", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
        seconds = time.monotonic() - started
        self.rows.append({"step": name, "status": status, "seconds": round(seconds), "detail": detail})
        print(f"[{status}] {name} ({seconds:.0f} s): {detail[:300]}", flush=True)
        self.write(f"## [{status}] {name} ({seconds:.0f} s)\n\n```\n{detail}\n```\n\n")
        (self.results / (self.report.stem + ".json")).write_text(json.dumps(self.rows, indent=2))
        return status == "PASS"

    def not_executed(self, name: str, reason: str) -> None:
        self.rows.append({"step": name, "status": "NOT EXECUTED", "seconds": 0, "detail": reason})
        print(f"[NOT EXECUTED] {name}: {reason}", flush=True)
        self.write(f"## [NOT EXECUTED] {name}\n\n{reason}\n\n")

    # -- helpers ------------------------------------------------------------------------
    def setup(self, key: str, *extra: str, dirs: bool = True) -> tuple[int, str]:
        exe = self.installers[key][0]
        log = self.logs / f"setup-{key}-{len(list(self.logs.glob('setup-*')))}.log"
        args = [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"]
        if dirs:
            args += [f"/DIR={APP}", f"/DATADIR={DATA}", f"/WORKSPACE={WS}"]
        result = subprocess.run(args + list(extra), timeout=1800)
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        return result.returncode, text

    def uninstall(self) -> int:
        exe = APP / "unins000.exe"
        log = self.logs / f"uninstall-{len(list(self.logs.glob('uninstall-*')))}.log"
        return subprocess.run([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"],
                              timeout=600).returncode

    def current(self) -> str | None:
        try:
            return (APP / "current").read_text().strip()
        except FileNotFoundError:
            return None

    def cli(self, *args: str, timeout: float = 900) -> subprocess.CompletedProcess:
        return subprocess.run([str(APP / "bin" / "nature-worker-upgrade.cmd"), *args], capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def history(self) -> list[dict]:
        path = APP / "upgrade" / "history.jsonl"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []

    def registry(self, key: str, name: str) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return None

    def schema(self) -> int:
        conn = sqlite3.connect(f"file:{DATA / 'worker.db'}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def tables(self) -> set[str]:
        conn = sqlite3.connect(f"file:{DATA / 'worker.db'}?mode=ro", uri=True)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def conversations(self) -> int:
        conn = sqlite3.connect(f"file:{DATA / 'worker.db'}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        finally:
            conn.close()

    def start_worker(self) -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = {**os.environ, "NATURE_WORKER_PORT": str(self.port)}
        log = open(self.logs / f"worker-{self.port}.log", "wb")
        self.worker = subprocess.Popen([str(APP / "bin" / "nature-worker.cmd")], env=env, stdout=log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            expect(self.worker.poll() is None, f"the Worker exited during startup (exit {self.worker.returncode})")
            try:
                return self.api("GET", "/api/health", timeout=HEALTH_TIMEOUT)["version"]
            except (OSError, urllib.error.URLError):
                time.sleep(0.5)
        raise Fail("the Worker did not answer within 90 s")

    def stop_worker(self) -> None:
        if self.worker:
            kill_tree(self.worker)
        self.worker = None

    def api(self, method: str, path: str, body: dict | None = None, timeout: float = REQUEST_TIMEOUT) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def task(self, text: str, conv: str | None = None) -> tuple[str, str, dict]:
        conv = conv or self.api("POST", "/api/conversations")["id"]
        status = self.api("POST", f"/api/conversations/{conv}/messages", {"text": text}, timeout=RUN_TIMEOUT)["status"]
        return conv, status, self.api("GET", f"/api/conversations/{conv}")

    @staticmethod
    def calls(view: dict, since: int = 0) -> list[str]:
        return [c["name"] for m in view["messages"][since:] if m["role"] == "assistant" for c in (m["tool_calls"] or [])]

    def snapshot(self) -> dict:
        import winreg
        keys = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as k:
                i = 0
                while True:
                    try:
                        keys.append(winreg.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        programs = START_MENU.parent
        # Platform Client folders: whether they exist, nothing more. Their contents are never read.
        pc = {}
        for root in PLATFORM_CLIENT:
            try:
                pc[str(root)] = "exists" if os.path.lexists(root) else "absent"
            except OSError as exc:
                pc[str(root)] = f"not accessible ({exc.winerror if hasattr(exc, 'winerror') else exc.errno})"
        return {"uninstall_keys": sorted(keys),
                "start_menu": sorted(p.name for p in programs.iterdir()) if programs.exists() else [],
                "localappdata": sorted(p.name for p in LOCAL.iterdir()),
                "platform_client": pc,
                "settings_key": self.registry(SETTINGS_KEY, "DataDir")}

    # -- phases -------------------------------------------------------------------------
    def preflight(self) -> str:
        expect(sys.platform == "win32", "not Windows")
        found = [str(p) for p in PRODUCTION if p.exists()]
        expect(not found, f"Nature AI Worker Standalone data or code already exists for this user: {found}. "
                          "This test installs the real product; run it on an account without it.")
        expect(self.registry(UNINSTALL_KEY, "DisplayName") is None, "Nature AI Worker is in Apps & Features already")
        expect(not NS.exists(), f"{NS} exists from an earlier run; remove it (or run `finish`) first")
        expect(Path(os.environ["SystemRoot"], "System32", "tar.exe").exists(), "tar.exe missing")
        for key, (exe, digest) in self.installers.items():
            got = hashlib.sha256(exe.read_bytes()).hexdigest()
            expect(got == digest, f"{exe.name}: SHA-256 {got} != {digest}")
        snap = self.snapshot()
        (self.results / "snapshot-before.json").write_text(json.dumps(snap, indent=2))
        python = subprocess.run(["where", "python"], capture_output=True, text=True).stdout.strip() or "none on PATH"
        return f"no existing installation; installers match SHA256SUMS; Python on PATH: {python}; snapshot saved"

    def fresh_install(self) -> str:
        code, log = self.setup("0.1.0")
        expect(code == 0, f"Setup exit {code}\n{log[-3000:]}")
        expect(self.current() == "0.1.0", f"current = {self.current()}")
        runtime = APP / "versions" / "0.1.0" / "runtime" / "python.exe"
        cfg = (APP / "versions" / "0.1.0" / "venv" / "pyvenv.cfg").read_text()
        home = [ln.split("=", 1)[1].strip() for ln in cfg.splitlines() if ln.startswith("home")][0]
        expect(runtime.exists() and Path(home) == runtime.parent, f"venv home {home}, expected {runtime.parent}")
        inst = json.loads((APP / "installation.json").read_text())
        expect(inst["edition"] == "standalone" and Path(inst["data_dir"]) == DATA and Path(inst["workspace_dir"]) == WS,
               f"installation.json {inst}")
        expect(self.registry(UNINSTALL_KEY, "DisplayVersion") == "0.1.0", "Apps & Features entry missing or wrong")
        links = sorted(p.name for p in START_MENU.glob("*.lnk"))
        expect("Nature AI Worker.lnk" in links and "Remove Nature AI Worker data.lnk" in links, f"Start menu: {links}")
        expect((APP / "licenses" / "THIRD_PARTY_LICENSES.txt").stat().st_size > 100_000, "licenses missing")
        status = self.cli("status")
        expect("NOT an official" in status.stdout or "not official" in status.stdout, status.stdout[-800:])
        h = self.history()
        expect(h and h[-1]["outcome"] == "installed", f"history {h}")
        return (f"installed 0.1.0 in {APP}; venv home {home}; Start menu {links}; Apps & Features 0.1.0; "
                f"{status.stdout.strip().splitlines()[1] if len(status.stdout.splitlines()) > 1 else ''}")

    def launch_and_configure(self, ollama: str | None, model: str) -> str:
        version = self.start_worker()
        expect(version == "0.1.0", f"health version {version}")
        page = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10).read().decode()
        expect("<html" in page.lower(), "no page")
        first = self.api("GET", "/api/settings")
        expect(first["configured"] is False, f"not a first run: {first}")
        detail = f"0.1.0 answered on port {self.port}; first run: configured=False, providers={len(first.get('providers', []))}"
        if ollama:
            view = self.api("PUT", "/api/settings", {"provider": "ollama", "model": model, "endpoint": ollama})
            expect(view["configured"], f"settings not saved: {view}")
            test = self.api("POST", "/api/settings/test", {"provider": "ollama", "model": model, "endpoint": ollama},
                            timeout=RUN_TIMEOUT)
            expect(test["ok"], f"connection test: {test}")
            detail += f"; Ollama configured, connection test: {test['message']}"
        return detail

    def real_model(self) -> str:
        conv, status, view = self.task("Reply with exactly the word: pong")
        answer = view["messages"][-1]["content"]
        expect(status == "done" and "pong" in answer.lower(), f"chat: {status} {answer[:200]}")
        (WS / "sales.csv").write_text("region,revenue\nNorth,170\nSouth,90\n")
        conv, status, view = self.task("Create report.md with exactly this content and nothing else:\n\n"
                                       "# Sales\n\nNorth: 170\nSouth: 90\nTotal: 250\n")
        expect(status == "done" and (WS / "report.md").exists(), f"write: {status} {self.calls(view)}")
        before = (WS / "report.md").read_bytes()
        since = len(view["messages"])
        conv, status, view = self.task("In report.md the total is wrong: 170 + 90 is 260. Correct only that figure.", conv)
        after = (WS / "report.md").read_bytes()
        calls = self.calls(view, since)
        expect(status == "done" and "replace_text" in calls and after == before.replace(b"Total: 250", b"Total: 260"),
               f"replace_text: {status} {calls} {after[:200]!r}")
        (WS / "plan.md").write_text("Plan for Q2\n")
        conv, status, view = self.task("In plan.md, replace 'Plan for Q2' with 'Plan for Q3'.")
        pending = [a for a in view["approvals"] if a["status"] == "pending"]
        expect(status == "needs_approval" and pending, f"approval not requested: {status} {self.calls(view)}")
        self.api("POST", f"/api/approvals/{pending[0]['id']}", {"approve": False}, timeout=RUN_TIMEOUT)
        expect((WS / "plan.md").read_text() == "Plan for Q2\n", "user file changed after denial")
        return (f"chat answered; report.md written; replace_text changed only the total ({calls}); "
                f"edit of the user's plan.md asked '{pending[0]['reason']}', denied, file unchanged; "
                f"conversations in data: {self.conversations()}")

    def acceptance_harness(self, ollama: str, model: str) -> str:
        python = APP / "versions" / self.current() / "venv" / "Scripts" / "python.exe"
        out = self.logs / "acceptance-installed-code.md"
        log = self.logs / "acceptance-installed-code.log"
        files = f"output {log}\nWorker log {out.with_name(out.stem + '-worker.log')}\nreport {out}"
        print(f"    {files.replace(chr(10), chr(10) + '    ')}", flush=True)
        code, lines = run_logged([str(python), "-u", str(self.kit / "acceptance.py"), "--provider", "ollama",
                                  "--model", model, "--endpoint", ollama, "--only", "C,P,F,U,S,E", "--out", str(out)],
                                 log, HARNESS_TIMEOUT)
        problem = harness_problem(code, lines, HARNESS_TIMEOUT)
        if problem:
            tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if not lines else ""
            raise Fail(f"{problem}\n{files}" + (f"\n{tail}" if tail else ""))
        return "installed code (its own venv Python), real model:\n" + "\n".join(lines) + f"\n{files}"

    def refused_upgrade(self) -> str:
        code, log = self.setup("0.1.1-acceptance", dirs=False)
        expect(code != 0 and self.current() == "0.1.0", f"exit {code}, current {self.current()}")
        expect("CONFIRMUPGRADE" in log, "refusal reason not in the Setup log")
        return f"silent upgrade without /CONFIRMUPGRADE refused (exit {code}); 0.1.0 still active"

    def upgrade_while_running(self) -> str:
        conversations = self.conversations()
        expect(self.worker is not None and self.worker.poll() is None, "the Worker should be running")
        code, log = self.setup("0.1.1-acceptance", "/CONFIRMUPGRADE=0.1.1+acceptance", dirs=False)
        expect(code == 0, f"Setup exit {code}\n{log[-3000:]}")
        expect(self.worker.wait(120) is not None, "the running Worker did not stop for the upgrade")
        self.worker = None
        expect(self.current() == "0.1.1+acceptance", f"current {self.current()}")
        expect(self.schema() == 2 and "acceptance_2" in self.tables(), f"schema {self.schema()}")
        expect(self.conversations() == conversations, "conversations lost")
        backups = sorted(p.name for p in (DATA / "backups").iterdir())
        expect(any(b.startswith("upgrade-") for b in backups), f"no upgrade backup: {backups}")
        expect(self.registry(UNINSTALL_KEY, "DisplayVersion") == "0.1.1+acceptance", "Apps & Features not updated")
        h = self.history()[-1]
        expect(h["outcome"] == "succeeded", f"history {h}")
        version = self.start_worker()
        settings = self.api("GET", "/api/settings")
        expect(version == "0.1.1+acceptance" and settings["configured"], f"{version} {settings.get('provider')}")
        self.stop_worker()
        return (f"running Worker drained and stopped; 0.1.1+acceptance active, data schema 2, {conversations} "
                f"conversations kept, settings kept (provider {settings['provider']}), backups {backups}")

    def failed_upgrade_rolls_back(self) -> str:
        conversations = self.conversations()
        code, log = self.setup("0.1.2-broken", "/CONFIRMUPGRADE=0.1.2+broken", dirs=False)
        expect(code != 0, "the broken upgrade reported success")
        expect(self.current() == "0.1.1+acceptance" and self.schema() == 2 and self.conversations() == conversations,
               f"not restored: current {self.current()} schema {self.schema()}")
        h = self.history()[-1]
        expect(h["outcome"] == "rolled_back", f"history {h}")
        expect(self.registry(UNINSTALL_KEY, "DisplayVersion") == "0.1.1+acceptance", "Apps & Features changed")
        return f"Setup failed (exit {code}); automatic rollback: {h.get('message', '')[:300]}"

    def refused_reinstall_and_downgrade(self) -> str:
        same, _ = self.setup("0.1.1-acceptance", "/CONFIRMUPGRADE=0.1.1+acceptance", dirs=False)
        older, _ = self.setup("0.1.0", dirs=False)
        expect(same != 0 and older != 0 and self.current() == "0.1.1+acceptance", f"{same} {older} {self.current()}")
        return f"same version refused (exit {same}); older version refused (exit {older})"

    def user_rollback(self) -> str:
        result = self.cli("rollback", "--confirm")
        expect(result.returncode == 0, result.stdout[-2000:] + result.stderr[-1000:])
        expect(self.current() == "0.1.0" and self.schema() == 1, f"current {self.current()} schema {self.schema()}")
        shown = self.registry(UNINSTALL_KEY, "DisplayVersion")
        expect(shown == "0.1.0", f"Apps & Features shows {shown} after the rollback")
        version = self.start_worker()
        expect(version == "0.1.0", f"health {version}")
        self.stop_worker()
        return (f"rollback to 0.1.0 with its data restored (schema 1); Apps & Features shows {shown}; starts and "
                f"answers\n{result.stdout[-600:]}")

    def uninstall_refused_while_running(self) -> str:
        self.start_worker()
        code = self.uninstall()
        still = (APP / "bin" / "nature-worker-upgrade.cmd").exists() and self.registry(UNINSTALL_KEY, "DisplayName")
        self.stop_worker()
        expect(code != 0 and still, f"uninstall exit {code}; program still there: {bool(still)}")
        return f"uninstaller refused while the Worker ran (exit {code}); nothing removed"

    def uninstall_keeps_data(self) -> str:
        db = hashlib.sha256((DATA / "worker.db").read_bytes()).hexdigest()
        files = sorted(p.name for p in WS.iterdir())
        code = self.uninstall()
        time.sleep(3)   # the uninstaller removes itself after it exits
        expect(code == 0, f"uninstall exit {code}")
        left = sorted(p.name for p in APP.iterdir()) if APP.exists() else []
        expect(not left, f"left in the program folder: {left}")
        expect(self.registry(UNINSTALL_KEY, "DisplayName") is None, "Apps & Features entry remains")
        expect(not START_MENU.exists(), "Start-menu folder remains")
        expect(hashlib.sha256((DATA / "worker.db").read_bytes()).hexdigest() == db, "data changed")
        expect(sorted(p.name for p in WS.iterdir()) == files, "workspace changed")
        return f"program, Start menu and Apps & Features entry removed; data (worker.db unchanged) and workspace {files} kept"

    def reinstall_adopts_data(self) -> str:
        conversations = self.conversations()
        code, log = self.setup("0.1.0")
        expect(code == 0, f"Setup exit {code}\n{log[-3000:]}")
        expect(self.registry(UNINSTALL_KEY, "DisplayVersion") == "0.1.0", "Apps & Features version")
        backups = sorted(p.name for p in (DATA / "backups").iterdir() if p.name.startswith("install-"))
        expect(backups, "no install backup of the adopted data")
        version = self.start_worker()
        settings = self.api("GET", "/api/settings")
        listed = self.api("GET", "/api/conversations")
        self.stop_worker()
        expect(settings["configured"] and len(listed) == conversations, f"{settings.get('configured')} {len(listed)}")
        return f"reinstalled 0.1.0 ({version}); adopted the kept data after backup {backups[-1]}: settings and {conversations} conversations"

    def sc_data_refused(self) -> str:
        other = NS / "sc-data"
        other.mkdir(parents=True)
        (other / "installation-mode.json").write_text('{"schema": 1, "mode": "platform_client"}\n')
        app2 = NS / "app-sc"
        log = self.logs / "setup-sc-refusal.log"
        code = subprocess.run([str(self.installers["0.1.0"][0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                               f"/LOG={log}", f"/DIR={app2}", f"/DATADIR={other}", f"/WORKSPACE={NS / 'ws-sc'}"],
                              timeout=1800).returncode
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        expect(code != 0 and not (app2 / "installation.json").exists() and not (app2 / "versions").exists(),
               f"exit {code}")
        expect("belongs to a Platform Client installation" in text, f"refused for another reason:\n{text[-2000:]}")
        expect(self.registry(UNINSTALL_KEY, "DisplayName") is None, "an Apps & Features entry was created")
        # A folder inside the Platform Client's own namespace is refused by its path alone: nothing
        # there is opened, read or created.
        inside = PLATFORM_CLIENT[1] / "standalone-acceptance-data"
        log2 = self.logs / "setup-sc-namespace-refusal.log"
        code2 = subprocess.run([str(self.installers["0.1.0"][0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                f"/LOG={log2}", f"/DIR={app2}", f"/DATADIR={inside}", f"/WORKSPACE={NS / 'ws-sc'}"],
                               timeout=1800).returncode
        text2 = log2.read_text(encoding="utf-8", errors="replace") if log2.exists() else ""
        expect(code2 != 0 and "inside the Platform Client's folders" in text2, f"exit {code2}:\n{text2[-2000:]}")
        expect(not (app2 / "installation.json").exists(), "something was installed")
        shutil.rmtree(NS)
        return (f"Setup refused a data folder marked as Platform Client data (exit {code}) and a data folder inside "
                f"the Platform Client's folders (exit {code2}); nothing installed or registered")

    def deepseek_configured(self) -> bool:
        """Whether the user configured DeepSeek in the UI (provider and a stored key; never the key)."""
        self.start_worker()
        try:
            settings = self.api("GET", "/api/settings")
            return settings.get("provider") == "deepseek" and settings.get("configured") \
                and settings.get("api_key_source") not in (None, "none")
        finally:
            self.stop_worker()

    def deepseek(self) -> str:
        self.start_worker()
        try:
            settings = self.api("GET", "/api/settings")
            conv, status, view = self.task("Use the calculate tool to compute 1234*5678, then reply with the result.")
            answer = view["messages"][-1]["content"]
            expect(status == "done" and "calculate" in self.calls(view) and "7006652" in answer.replace(",", ""),
                   f"{status} {self.calls(view)} {answer[:200]}")
            return (f"DeepSeek ({settings.get('model')}), key stored in the app ({settings.get('api_key_source')}): "
                    "tool call and answer correct")
        finally:
            self.stop_worker()

    def remove_data(self) -> str:
        wrong = self.cli("remove-data", "--confirm-path", str(NS))
        expect(wrong.returncode == 2 and DATA.exists(), f"wrong confirmation: {wrong.returncode}")
        result = self.cli("remove-data", "--include-workspace", "--confirm-path", str(DATA))
        expect(result.returncode == 0 and not DATA.exists() and not WS.exists(), result.stdout[-1500:])
        return "wrong confirmation refused; then data and workspace deleted by the explicit workflow\n" + result.stdout[-800:]

    def final_uninstall_and_residue(self) -> str:
        code = self.uninstall()
        time.sleep(3)
        expect(code == 0, f"uninstall exit {code}")
        before = json.loads((self.results / "snapshot-before.json").read_text())
        after = self.snapshot()
        (self.results / "snapshot-after.json").write_text(json.dumps(after, indent=2))
        leftovers = sorted(str(p) for p in NS.rglob("*")) if NS.exists() else []
        local_new = sorted(set(after["localappdata"]) - set(before["localappdata"]) - {"NatureAIWorkerAcceptance"})
        diffs = {k: (before[k], after[k]) for k in ("uninstall_keys", "start_menu", "platform_client", "settings_key")
                 if before[k] != after[k]}
        expect(not diffs, f"changed: {diffs}")
        expect(not local_new, f"new folders in %LOCALAPPDATA%: {local_new}")
        return (f"uninstalled; Apps & Features, Start menu, %LOCALAPPDATA% and Platform Client installations as "
                f"before; left in the test namespace: {leftovers or 'nothing'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=("auto", "finish"))
    p.add_argument("--kit", type=Path, required=True)
    p.add_argument("--ollama", help="Ollama URL for the real-model checks")
    p.add_argument("--model", default="qwen3.8:27b")
    args = p.parse_args(argv)
    kit = args.kit.resolve()
    try:
        installers = load_installers(kit)
    except Fail as exc:
        (kit / "results").mkdir(exist_ok=True)
        report = kit / "results" / f"acceptance-{args.phase}-{time.strftime('%Y%m%d-%H%M%S')}-NOT-EXECUTED.md"
        report.write_text(f"# Windows Standalone installer acceptance ({args.phase}): NOT EXECUTED\n\n{exc}\n",
                          encoding="utf-8")
        print(f"NOT EXECUTED: {exc}\nReport: {report}", file=sys.stderr)
        return 2
    run = Run(kit, args.phase, installers)
    try:
        if args.phase == "auto":
            if not run.step("Preflight", run.preflight):
                return 1
            run.step("No in-place S-C use: Platform Client data and folders are refused", run.sc_data_refused)
            if not run.step("Fresh install of 0.1.0 (silent, per user, no administrator)", run.fresh_install):
                return 1
            run.step("Launch and first-run provider configuration", lambda: run.launch_and_configure(args.ollama, args.model))
            if args.ollama:
                run.step("Real model on the installed Worker: chat, files, replace_text, approval", run.real_model)
                run.stop_worker()
                run.step("Real model on the installed code: acceptance C, P, F, U, S, E", lambda: run.acceptance_harness(args.ollama, args.model))
                run.start_worker()
            else:
                run.not_executed("Real-model checks", "no --ollama URL given")
            run.step("Silent upgrade without confirmation is refused", run.refused_upgrade)
            run.step("Upgrade to 0.1.1+acceptance while the Worker runs", run.upgrade_while_running)
            run.step("Failed upgrade to 0.1.2+broken rolls back automatically", run.failed_upgrade_rolls_back)
            run.step("Same-version reinstall and downgrade are refused", run.refused_reinstall_and_downgrade)
            run.step("User rollback to 0.1.0", run.user_rollback)
            run.step("Uninstall refused while the Worker runs", run.uninstall_refused_while_running)
            run.step("Uninstall keeps data and workspace", run.uninstall_keeps_data)
            run.step("Reinstall adopts the kept data", run.reinstall_adopts_data)
        else:
            if run.deepseek_configured():
                run.step("Cloud provider: DeepSeek configured in the UI", run.deepseek)
            else:
                run.not_executed("Cloud provider: DeepSeek", "DeepSeek was not configured in the UI (no key stored)")
            run.step("Explicit data removal (remove-data)", run.remove_data)
            run.step("Final uninstall and residue check", run.final_uninstall_and_residue)
    finally:
        run.stop_worker()
    counts = {s: sum(1 for r in run.rows if r["status"] == s) for s in ("PASS", "FAIL", "ERROR", "NOT EXECUTED")}
    run.write(f"\n**Summary:** {counts}\n")
    print(f"\nSummary: {counts}\nReport: {run.report}")
    return 0 if not counts["FAIL"] and not counts["ERROR"] else 1


if __name__ == "__main__":
    sys.exit(main())
