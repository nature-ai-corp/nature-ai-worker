# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle of scripts/platform_client_windows_e2e.py: a report is written for every
outcome (also when the preconditions fail), and cleanup removes only what the run
itself created. Scenarios and the OS are replaced; the real ``run()`` is driven."""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "platform_client_windows_e2e.py"
SCENARIOS = ("s_install", "s_repair", "s_signed_upgrade", "s_failed_upgrade_rolls_back", "s_refused_tampered",
             "s_killed_during_verification", "s_user_rollback", "s_uninstall_keeps_data_then_reinstall",
             "s_uninstall_with_data", "s_production_untouched")


PROFILE = {"path": "C:\\Windows\\ServiceProfiles\\NatureWorkerPlatformClientE2E", "exists": True,
           "credential_files": [], "dpapi_master_keys": [], "other_data": [], "file_count": 3,
           "removable": True, "problems": []}


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("platform_client_windows_e2e", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "E2E_PREFIX", tmp_path / "ProgramFiles" / "NatureAIWorkerE2E" / "PlatformClient")
    monkeypatch.setattr(module, "E2E_DATA", tmp_path / "ProgramData" / "NatureAIWorkerE2E" / "PlatformClient")
    calls = []

    def setup_cmd(self, *args):
        calls.append(args)
        if args[0] == "uninstall":  # what the real e2e uninstall does to the namespace
            import shutil
            shutil.rmtree(module.E2E_PREFIX, ignore_errors=True)
            shutil.rmtree(module.E2E_DATA, ignore_errors=True)
            self.system.service = None
        if args[0] == "service-profile":  # read-only; Windows keeps the profile after the uninstall
            return subprocess.CompletedProcess(args, 0, json.dumps(PROFILE), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(module.E2E, "setup_cmd", setup_cmd)

    class System:
        service = None

        def service_status(self, _name):
            return self.service

        def get_registry(self, _key, _name):
            return None
    original_init = module.E2E.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.system = System()
    monkeypatch.setattr(module.E2E, "__init__", init)
    for name in SCENARIOS:
        monkeypatch.setattr(module.E2E, name, lambda self: None)
    return module, calls


def test_preconditions_failing_before_any_install_still_write_the_report(e2e, tmp_path, capsys):
    module, calls = e2e

    def not_elevated(self):
        raise module.Check("run this in an elevated (administrator) prompt")
    module.E2E.s_preconditions = not_elevated
    workdir = tmp_path / "e2e-17271"
    assert module.run(workdir, tmp_path / "src.tar", "https://platform.example.invalid") == 1
    report = (workdir / "windows-platform-client-e2e.md").read_text(encoding="utf-8")
    assert "Result: 0/1 passed" in report and "## FAIL: Preconditions and test material" in report
    assert "run this in an elevated (administrator) prompt" in report
    assert "elevated (administrator) prompt" in capsys.readouterr().out  # the reason reaches the console too
    assert calls == []  # nothing was created, so nothing is cleaned up


def test_an_existing_e2e_namespace_is_never_cleaned_up(e2e, tmp_path):
    module, calls = e2e
    module.E2E_PREFIX.mkdir(parents=True)  # e.g. left by an earlier run: not ours to delete

    def refuse(self):
        self.system.service = "running"
        raise module.Check("NatureWorkerPlatformClientE2E already exists")
    module.E2E.s_preconditions = refuse
    assert module.run(tmp_path / "w", tmp_path / "src.tar", "https://x.invalid") == 1
    assert calls == [] and module.E2E_PREFIX.exists()
    assert "already exists" in (tmp_path / "w" / "windows-platform-client-e2e.md").read_text(encoding="utf-8")


def test_a_failed_scenario_is_reported_and_cleanup_removes_partial_installs(e2e, tmp_path):
    module, calls = e2e

    def ready(self):
        self.before = {}
        self.namespace_ours = True
    module.E2E.s_preconditions = ready

    def partial_install(self):
        module.E2E_PREFIX.mkdir(parents=True)  # the code folder exists, the service was never created
        module.E2E_DATA.mkdir(parents=True)
        raise module.Check("install failed: pip could not reach the index")
    module.E2E.s_install = partial_install
    workdir = tmp_path / "w"
    assert module.run(workdir, tmp_path / "src.tar", "https://x.invalid") == 1
    assert calls == [("uninstall", "--delete-local-data")]
    assert not module.E2E_PREFIX.parent.exists() and not module.E2E_DATA.parent.exists()
    report = (workdir / "windows-platform-client-e2e.md").read_text(encoding="utf-8")
    assert "## FAIL: Install" in report and "pip could not reach the index" in report
    assert "## Cleanup" in report and "remaining none" in report
    assert "## PASS: Production installation and Standalone untouched" in report


def test_report_is_rewritten_after_every_scenario(e2e, tmp_path):
    module, _ = e2e
    seen = []

    def ready(self):
        self.before = {}
        self.namespace_ours = True
    module.E2E.s_preconditions = ready
    module.E2E.s_repair = lambda self: seen.append(self.report_path.read_text(encoding="utf-8"))
    assert module.run(tmp_path / "w", tmp_path / "src.tar", "https://x.invalid") == 0
    assert "## PASS: Install" in seen[0]  # already on disk while later scenarios run


def test_an_existing_workdir_is_refused_and_left_alone(e2e, tmp_path):
    module, _ = e2e
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "keep.txt").write_text("earlier report")
    assert module.run(workdir, tmp_path / "src.tar", "https://x.invalid") == 2
    assert [p.name for p in workdir.iterdir()] == ["keep.txt"]


def test_scenarios_that_need_a_failed_install_are_skipped_not_run(e2e, tmp_path, capsys):
    module, calls = e2e
    ran = []

    def ready(self):
        self.before = {}
        self.namespace_ours = True
    module.E2E.s_preconditions = ready

    def failed_install(self):
        raise module.Check("install failed: Command failed (exit 1): python.exe -I -m venv ...")
    module.E2E.s_install = failed_install
    for name in SCENARIOS[1:-1]:
        setattr(module.E2E, name, lambda self, name=name: ran.append(name))
    workdir = tmp_path / "w"
    assert module.run(workdir, tmp_path / "src.tar", "https://x.invalid") == 1
    assert ran == []  # nothing ran against an installation that never completed
    report = (workdir / "windows-platform-client-e2e.md").read_text(encoding="utf-8")
    assert report.count("## SKIPPED:") == 8 and "not run: needs Install" in report
    assert "Result: 2/11 passed" in report and "## FAIL: Install" in report
    assert "## PASS: Production installation and Standalone untouched" in report  # always checked
    assert "[SKIPPED] Repair after damaged permissions" in capsys.readouterr().out


def test_upgrade_dependents_skip_when_only_the_upgrade_fails(e2e, tmp_path):
    module, _ = e2e
    ran = []

    def ready(self):
        self.before = {}
        self.namespace_ours = True
    module.E2E.s_preconditions = ready

    def failed_upgrade(self):
        raise module.Check("upgrade failed")
    module.E2E.s_signed_upgrade = failed_upgrade
    for name in ("s_repair", "s_failed_upgrade_rolls_back", "s_user_rollback", "s_uninstall_with_data"):
        setattr(module.E2E, name, lambda self, name=name: ran.append(name))
    module.run(tmp_path / "w", tmp_path / "src.tar", "https://x.invalid")
    assert ran == ["s_repair", "s_uninstall_with_data"]  # these need only the install


# -- abnormal termination (native e2e-4952: Windows shut down while Install was checking) ----

def ready(self):
    self.before = {}
    self.namespace_ours = True


def test_a_runner_stopped_inside_a_scenario_leaves_that_scenario_on_disk(e2e, tmp_path):
    """A kill, crash or shutdown runs no Python code: what is on disk at that moment counts."""
    module, _ = e2e
    module.E2E.s_preconditions = ready
    seen = {}

    def install(self):
        seen["report"] = self.report_path.read_text(encoding="utf-8")
        seen["journal"] = (self.w / "e2e-journal.log").read_text(encoding="utf-8")
    module.E2E.s_install = install
    workdir = tmp_path / "w"
    assert module.run(workdir, tmp_path / "src.tar", "https://x.invalid") == 0
    assert "## RUNNING (not finished): Install" in seen["report"] and "see e2e-journal.log" in seen["report"]
    assert "scenario started: Install" in seen["journal"] and "runner finished" not in seen["journal"]
    report = (workdir / "windows-platform-client-e2e.md").read_text(encoding="utf-8")
    assert "RUNNING" not in report and "see e2e-journal.log" not in report and "## PASS: Install" in report
    journal = (workdir / "e2e-journal.log").read_text(encoding="utf-8")
    assert "runner started: pid" in journal and "scenario PASS: Install" in journal
    assert journal.rstrip().endswith("runner finished")


def test_an_interrupted_runner_records_why_and_still_cleans_up(e2e, tmp_path):
    module, calls = e2e
    module.E2E.s_preconditions = ready

    def install(self):
        module.E2E_PREFIX.mkdir(parents=True)
        raise KeyboardInterrupt()
    module.E2E.s_install = install
    workdir = tmp_path / "w"
    with pytest.raises(KeyboardInterrupt):
        module.run(workdir, tmp_path / "src.tar", "https://x.invalid")
    assert calls == [("uninstall", "--delete-local-data")]
    report = (workdir / "windows-platform-client-e2e.md").read_text(encoding="utf-8")
    assert "## RUNNING (not finished): Install" in report
    assert "## ENDED ABNORMALLY: KeyboardInterrupt" in report and "while running: Install" in report
    assert report.index("## ENDED ABNORMALLY") < report.index("## Cleanup")
    journal = (workdir / "e2e-journal.log").read_text(encoding="utf-8")
    assert "runner ending: KeyboardInterrupt" in journal and "(during scenario: Install" in journal
    assert "Current thread" in journal or "Thread 0x" in journal  # the stacks
    assert "runner finished" not in journal and "runner ended: KeyboardInterrupt" in journal


def test_children_are_journaled_and_a_hung_one_is_killed_with_the_stack_recorded(e2e, tmp_path, monkeypatch):
    import sys
    module, _ = e2e
    monkeypatch.setattr(module, "STALL_DUMP_SECONDS", 1)
    workdir = tmp_path / "w"
    workdir.mkdir()
    runner = module.E2E(workdir, None, "https://x.invalid")
    ok = runner.run([sys.executable, "-c", "print('hi')"], timeout=30)
    assert ok.returncode == 0 and ok.stdout.strip() == "hi"
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=2.5)
    runner.journal.close()
    journal = (workdir / "e2e-journal.log").read_text(encoding="utf-8")
    assert re.search(r"child \d+ started \(timeout 30 s\): .*print\('hi'\)", journal)
    assert re.search(r"child \d+ exit 0 after", journal)
    assert re.search(r"child \d+ killed: still running after 2.5 s", journal)
    assert "in communicate" in journal  # the stall dump shows where the runner waited


@pytest.fixture
def leftovers(e2e, monkeypatch):
    module, calls = e2e
    monkeypatch.setattr(module, "current_token", lambda: type("T", (), {"administrator": True, "elevated": True})())
    monkeypatch.setattr(module.E2E, "production_state", lambda self: {})
    module.E2E_PREFIX.mkdir(parents=True)
    module.E2E_DATA.mkdir(parents=True)
    (module.E2E_PREFIX / "installation.json").write_text(json.dumps(
        {"service": "NatureWorkerPlatformClientE2E", "data_dir": str(module.E2E_DATA)}), encoding="utf-8")
    return module, calls


def test_cleanup_of_leftovers_waits_while_an_installer_is_still_active(leftovers, tmp_path):
    module, calls = leftovers
    module.E2E.active_processes = lambda self: ["4711 python.exe scripts\\platform-client-windows.py install"]
    workdir = tmp_path / "e2e-4952"
    workdir.mkdir()
    assert module.cleanup_leftovers(workdir, "https://x.invalid") == 1
    assert calls == [] and module.E2E_PREFIX.exists() and module.E2E_DATA.exists()
    report = (workdir / "windows-platform-client-e2e-cleanup.md").read_text(encoding="utf-8")
    assert "still active" in report and "4711" in report
    assert not (workdir / "windows-platform-client-e2e.md").exists()  # the earlier report is never rewritten


def test_cleanup_of_leftovers_refuses_an_installation_that_is_not_the_e2e_one(leftovers, tmp_path):
    module, calls = leftovers
    module.E2E.active_processes = lambda self: []
    (module.E2E_PREFIX / "installation.json").write_text(json.dumps(
        {"service": "NatureWorkerPlatformClient", "data_dir": "C:\\ProgramData\\NatureAIWorker"}), encoding="utf-8")
    workdir = tmp_path / "e2e-4952"
    workdir.mkdir()
    assert module.cleanup_leftovers(workdir, "https://x.invalid") == 1
    assert calls == [] and module.E2E_PREFIX.exists()


def test_cleanup_of_leftovers_refuses_an_unfinished_upgrade(leftovers, tmp_path):
    module, calls = leftovers
    module.E2E.active_processes = lambda self: []
    (module.E2E_PREFIX / "upgrade").mkdir()
    (module.E2E_PREFIX / "upgrade" / "state.json").write_text("{}", encoding="utf-8")
    workdir = tmp_path / "e2e-4952"
    workdir.mkdir()
    assert module.cleanup_leftovers(workdir, "https://x.invalid") == 1
    assert calls == []


def test_cleanup_of_leftovers_uses_the_e2e_uninstall_and_checks_production(leftovers, tmp_path):
    module, calls = leftovers
    module.E2E.active_processes = lambda self: []
    workdir = tmp_path / "e2e-4952"
    workdir.mkdir()
    (workdir / "windows-platform-client-e2e.md").write_text("earlier report", encoding="utf-8")
    assert module.cleanup_leftovers(workdir, "https://x.invalid") == 0
    # The service profile is inspected, never removed (no --remove-service-profile).
    assert calls == [("uninstall", "--delete-local-data"), ("service-profile",)]
    assert not module.E2E_PREFIX.parent.exists() and not module.E2E_DATA.parent.exists()
    report = (workdir / "windows-platform-client-e2e-cleanup.md").read_text(encoding="utf-8")
    assert "Result: 3/3 passed" in report and "nothing of the e2e namespace remains" in report
    assert "kept by Windows (3 entries" in report and "removable on request: True" in report
    assert (workdir / "windows-platform-client-e2e.md").read_text(encoding="utf-8") == "earlier report"


def test_active_processes_ignore_this_runner_its_launcher_and_the_e2e_service(e2e, tmp_path, monkeypatch):
    import os
    module, _ = e2e
    workdir = tmp_path / "w"
    workdir.mkdir()
    runner = module.E2E(workdir, None, "https://x.invalid")
    me, launcher, console = os.getpid(), 900001, 900000
    code = str(module.E2E_PREFIX)
    listing = {"self": 900009, "service": 900002, "processes": [
        {"pid": console, "parent": 4, "exe": "C:\\Windows\\System32\\cmd.exe", "cmd": "cmd /c 3-e2e-cleanup-as-admin.cmd"},
        {"pid": launcher, "parent": console, "exe": "C:\\kit\\venv\\Scripts\\python.exe",
         "cmd": "venv\\Scripts\\python.exe scripts\\platform_client_windows_e2e.py --cleanup-leftovers"},
        {"pid": me, "parent": launcher, "exe": "C:\\uv\\python.exe", "cmd": "python.exe scripts\\platform_client_windows_e2e.py"},
        {"pid": 900002, "parent": 600, "exe": f"{code}\\runtime\\python.exe", "cmd": f"\"{code}\\runtime\\python.exe\" -I svc.py"},
        {"pid": 900003, "parent": 1, "exe": "C:\\uv\\python.exe", "cmd": "python.exe -I scripts\\platform-client-windows.py install"},
        {"pid": 900004, "parent": 1, "exe": f"{code}\\versions\\9.1.0\\venv\\Scripts\\python.exe", "cmd": "python.exe -m pip install"},
        {"pid": 900005, "parent": 1, "exe": "C:\\Windows\\explorer.exe", "cmd": "explorer.exe"},
    ]}
    monkeypatch.setattr(module.E2E, "run", lambda self, cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(listing), ""))
    active = runner.active_processes()
    assert [a.split()[0] for a in active] == ["900003", "900004"]
    runner.journal.close()


# -- Credential Manager (e2e-4952 cleanup: "production credential_manager changed") -------------

LISTING = """
Currently stored credentials:

    Target: MicrosoftAccount:target=SSO_POP_Device
    Type: Generic 
    User: 02abcdefghijkl
    Saved for this logon only
    
    Target: WindowsLive:target=virtualapp/didlogical
    Type: Generic 
    User: 02abcdefghijkl
    Local machine persistence
    
    Target: LegacyGeneric:target=git:https://github.com
    Type: Generic 
    User: someone@example.com
    Local machine persistence
    
"""


def test_credential_listing_is_kept_as_metadata_without_order_or_user_names(e2e):
    module, _ = e2e
    entries = module.credential_entries(LISTING)
    assert {e[0] for e in entries} == {"MicrosoftAccount:target=SSO_POP_Device",
                                       "WindowsLive:target=virtualapp/didlogical",
                                       "LegacyGeneric:target=git:https://github.com"}
    assert {e[2] for e in entries} == {"Saved for this logon only", "Local machine persistence"}
    assert not any("someone@example.com" in field or "02abcdefghijkl" in field for e in entries for field in e)
    blocks = LISTING.split("\n    \n")
    assert module.credential_entries("\n    \n".join(reversed(blocks))) == entries  # order is not a change
    assert module.credential_entries("Currently stored credentials:\n\n* NONE *\n") == frozenset()
    assert module.redact("MicrosoftAccount:user=someone@example.com") == "MicrosoftAccount:user=<account>"


def test_a_windows_sign_in_refresh_is_reported_not_failed(e2e, tmp_path):
    module, _ = e2e
    after = LISTING.replace("User: 02abcdefghijkl\n    Saved for this logon only",
                            "User: 02zzzzzzzzzzzz\n    Saved for this logon only")
    managed, other = module.credential_changes(LISTING, after)
    assert managed == ["MicrosoftAccount:target=SSO_POP_Device"] and other == []
    runner = module.E2E(tmp_path, None, "https://x.invalid")
    runner.before = {"credential_manager": LISTING}
    runner.check_credentials(after)
    note = next(line for line in runner.lines if "Windows refreshed an entry" in line)
    assert "before [Generic, Saved for this logon only, user " in note and "Impact:" in note
    assert "02abcdefghijkl" not in note and "02zzzzzzzzzzzz" not in note
    runner.journal.close()


@pytest.mark.parametrize("after", [
    LISTING + "    Target: LegacyGeneric:target=NatureWorkerPlatformClient\n    Type: Generic \n"
              "    User: svc\n    Local machine persistence\n",                            # added (ours)
    LISTING + "    Target: LegacyGeneric:target=platform.example.invalid\n    Type: Generic \n"
              "    User: svc\n    Local machine persistence\n",                            # added (any name)
    LISTING.replace("    Target: LegacyGeneric:target=git:https://github.com", "    Target: LegacyGeneric:target=x"),
    LISTING.replace("User: someone@example.com", "User: other@example.com"),           # user changed
    LISTING.replace("    Target: MicrosoftAccount:target=SSO_POP_Device", "    Target: x"),  # managed removed
    LISTING + "    Target: MicrosoftAccount:target=SSO_POP_User\n    Type: Generic \n"
              "    User: 02x\n    Saved for this logon only\n",                           # managed added
])
def test_every_other_credential_change_still_fails(e2e, tmp_path, after):
    module, _ = e2e
    runner = module.E2E(tmp_path, None, "https://x.invalid")
    runner.before = {"credential_manager": LISTING}
    with pytest.raises(module.Check, match="were added, removed or changed") as failure:
        runner.check_credentials(after)
    assert "example.com" not in str(failure.value)  # account names never reach the report
    runner.journal.close()


def test_unparsed_listings_are_compared_whole(e2e):
    module, _ = e2e
    german = "Derzeit gespeicherte Anmeldeinformationen:\n\n    Ziel: LegacyGeneric:target=a\n"
    assert module.credential_changes(german, german) == ([], [])
    assert module.credential_changes(german, german + "    Ziel: LegacyGeneric:target=b\n")[1]


def test_credential_files_of_the_service_account_fail_the_check(e2e, tmp_path):
    module, _ = e2e
    profile = tmp_path / "ServiceProfiles" / "NatureWorkerPlatformClientE2E"
    vault = profile / "AppData" / "Local" / "Microsoft" / "Credentials"
    vault.mkdir(parents=True)
    runner = module.E2E(tmp_path, None, "https://x.invalid")
    runner.system.get_registry = lambda key, name: str(profile) if runner.sid in key else None
    runner.before = {"credential_manager": LISTING}
    runner.check_credentials(LISTING)  # an empty Credentials folder is fine
    (vault / "0123456789ABCDEF").write_bytes(b"\0")
    with pytest.raises(module.Check, match="service account stored Credential Manager files"):
        runner.check_credentials(LISTING)
    runner.journal.close()


# Native Windows: read-only (cmdkey /list, registry and folder listings); no credential is touched.
windows_only = pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows only")


@windows_only
def test_native_cmdkey_listing_parses_every_entry(e2e):
    module, _ = e2e
    listing = subprocess.run([module.CMDKEY, "/list"], capture_output=True, text=True, timeout=60).stdout
    targets = [ln for ln in listing.splitlines() if ln.strip().lower().startswith("target:")]
    entries = module.credential_entries(listing)
    if targets:
        assert len({t.split(":", 1)[1].strip() for t in targets}) == len({e[0] for e in entries})
        assert all(e[2] for e in entries)  # each entry has its persistence
    assert module.credential_changes(listing, listing) == ([], [])


@windows_only
def test_native_service_profile_of_a_builtin_account(e2e):
    module, _ = e2e
    from nature_worker.platform_client.windows_setup import WindowsSystem
    profile = module.service_profile(WindowsSystem(), "S-1-5-19", "LocalService")  # NT AUTHORITY\LOCAL SERVICE
    assert profile is not None and profile.name.lower() == "localservice"
    assert isinstance(module.profile_credential_files(profile), list)


@windows_only
def test_native_snapshot_records_the_user_and_elevation(e2e, tmp_path):
    module, _ = e2e
    from nature_worker.windows_security import current_token
    runner = module.E2E(tmp_path, None, "https://x.invalid")
    runner.credential_snapshot()
    runner.journal.close()
    journal = (tmp_path / "e2e-journal.log").read_text(encoding="utf-8")
    token = current_token()
    assert f"Credential Manager as {token.user} (elevated {token.elevated})" in journal
