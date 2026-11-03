# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows Platform Client (S-C): identity and permission analysis, the service
preflight, the installer (with a recording fake of the OS), the upgrade toolkit
and the service host. Native Windows APIs are exercised by the Windows-only tests
at the end; everything else runs on any OS."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import pytest

from nature_worker.installation_mode import ModeError
from nature_worker.platform_client import windows_profile, windows_setup, winservice
from nature_worker.platform_client.windows_setup import ACCOUNT, Setup, SetupError
from nature_worker.windows_isolation import evaluate_windows_isolation
from nature_worker.windows_security import (
    ADMINISTRATORS, SYSTEM, TRUSTED_INSTALLER, Ace, Acl, Token, admin_only_problems, parent_problems,
    private_problems, service_sid,
)

SVC = service_sid("NatureWorkerPlatformClient")
USER = "S-1-5-21-1111111111-2222222222-3333333333-1001"
USERS, AUTH_USERS, EVERYONE = "S-1-5-32-545", "S-1-5-11", "S-1-1-0"
FULL, MODIFY, READ = 0x1F01FF, 0x1301BF, 0x1200A9


def private_acl(account=SVC, *extra):
    return Acl(ADMINISTRATORS, True, (Ace(SYSTEM, FULL, flags=3), Ace(ADMINISTRATORS, FULL, flags=3),
                                      Ace(account, MODIFY, flags=3), *extra))


# -- identities and permissions -------------------------------------------------------

def test_service_sid_matches_windows():
    assert service_sid("TrustedInstaller") == TRUSTED_INSTALLER
    assert service_sid("trustedinstaller") == TRUSTED_INSTALLER  # names are case-insensitive
    assert SVC.startswith("S-1-5-80-") and SVC != TRUSTED_INSTALLER


def test_private_data_root_analysis():
    assert private_problems(private_acl(), SVC) == []
    assert "inherits" in private_problems(Acl(ADMINISTRATORS, False, private_acl().aces), SVC)[0]
    assert "S-1-5-32-545" in private_problems(private_acl(SVC, Ace(USERS, READ)), SVC)[0]
    assert USER in private_problems(private_acl(SVC, Ace(USER, FULL)), SVC)[0]
    assert "owner" in private_problems(Acl(USER, True, private_acl().aces), SVC)[0]
    assert "grants no access" in private_problems(private_acl(USER), SVC)[-1]
    # An inherit-only entry does not grant access to the folder itself; a deny entry never grants.
    assert private_problems(private_acl(SVC, Ace(USERS, READ, flags=0x08 | 3)), SVC) == []
    assert private_problems(private_acl(SVC, Ace(EVERYONE, FULL, type=1)), SVC) == []


def test_code_and_parent_analysis():
    program_files = Acl(TRUSTED_INSTALLER, True, (Ace(TRUSTED_INSTALLER, FULL), Ace(SYSTEM, FULL),
                                                  Ace(ADMINISTRATORS, FULL), Ace(USERS, READ)))
    assert admin_only_problems(program_files) == []
    assert "writable by" in admin_only_problems(Acl(ADMINISTRATORS, True, (Ace(USERS, MODIFY),)))[0]
    assert "owner" in admin_only_problems(Acl(USER, True, ()))[0]
    # C:\ lets Authenticated Users create folders (0x4), which cannot replace an existing one.
    drive_root = Acl(SYSTEM, False, (Ace(AUTH_USERS, 0x4), Ace(AUTH_USERS, MODIFY, flags=0x08 | 3)))
    assert parent_problems(drive_root) == []
    assert "replace" in parent_problems(Acl(SYSTEM, False, (Ace(USERS, 0x40),)))[0]


# -- service preflight --------------------------------------------------------------

def findings(tmp_path, token=Token(SVC, frozenset({USERS}), False), acl=None, standalone_sid=USER):
    sc, sa = tmp_path / "pc", tmp_path / "sa"
    sc.mkdir(exist_ok=True)
    sa.mkdir(exist_ok=True)
    result = evaluate_windows_isolation(token, sc, sa, standalone_sid, read=lambda _p: acl or private_acl())
    return {f.check: f.clean for f in result}


def test_isolation_preflight_passes_only_for_a_separate_unprivileged_account(tmp_path):
    assert set(findings(tmp_path).values()) == {True}
    assert findings(tmp_path, Token(USER, frozenset({ADMINISTRATORS}), True))["unprivileged_account"] is False
    assert findings(tmp_path, Token(SYSTEM, frozenset(), True))["unprivileged_account"] is False
    assert findings(tmp_path, Token(USER, frozenset(), False), acl=private_acl(USER))["separate_from_standalone"] is False
    assert findings(tmp_path, standalone_sid=None)["separate_from_standalone"] is None  # unknown, never clean
    assert findings(tmp_path, acl=private_acl(SVC, Ace(USERS, READ)))["sc_root_private"] is False


def test_isolation_preflight_rejects_overlapping_roots(tmp_path):
    root = tmp_path / "pc"
    root.mkdir()
    result = evaluate_windows_isolation(Token(SVC, frozenset(), False), root, root / "standalone", USER,
                                        read=lambda _p: private_acl())
    assert {f.check: f.clean for f in result}["independent_data_roots"] is False


def test_server_uses_the_windows_probe_and_memory_only_sessions(tmp_path, monkeypatch):
    from nature_worker.os_probes import ProbeFinding
    from nature_worker.platform_client import server
    from nature_worker.installation_mode import initialize_platform_client
    initialize_platform_client(tmp_path / "pc")
    monkeypatch.setattr(sys, "platform", "win32")
    seen = {}

    def probe(sc, sa, sid):
        seen["sid"] = sid
        return (ProbeFinding("unprivileged_account", True, "ok"),)
    monkeypatch.setattr(server, "inspect_windows_isolation", probe)
    settings = server.ClientSettings(tmp_path / "pc", "https://platform.example", tmp_path / "sa",
                                     standalone_sid=USER)
    preflight, _ = server.startup_findings(settings, True)
    assert preflight.allowed and seen["sid"] == USER


# -- installer ----------------------------------------------------------------------

@dataclass
class Status:
    state: int

    @property
    def name(self):
        return {1: "stopped", 4: "running"}[self.state]


class FakeWindows:
    """Records every OS operation and emulates its effect on the (temporary) disk."""

    def __init__(self, tmp_path, *, admin=True):
        self.tmp = tmp_path
        self.calls: list[list[str]] = []
        self.registry: dict[str, dict] = {}
        self.service: int | None = None
        self.acls: dict[Path, Acl] = {}
        self.admin = admin
        self.busy_ports: set[int] = set()
        self.start_fails_for: set[str] = set()
        self.fail_pip = False
        self.names: set[str] = set()
        self.private_sid = SVC
        self.account_sid_value = None
        self.resolve_override = None
        self.status_override = None
        self.prefix = None
        self.profiles = tmp_path / "ServiceProfiles"
        self.loaded_hives: set[str] = set()

    # identity and permissions
    def token(self):
        return Token(USER, frozenset({ADMINISTRATORS}) if self.admin else frozenset(), self.admin)

    def read_acl(self, path):
        path = Path(path)
        if path in self.acls:
            return self.acls[path]
        if self.prefix and (path == self.prefix or self.prefix in path.parents):
            return Acl(ADMINISTRATORS, False, (Ace(SYSTEM, FULL), Ace(ADMINISTRATORS, FULL), Ace(USERS, READ)))
        return private_acl(self.private_sid)

    def is_link(self, path):
        return Path(path).is_symlink()

    # commands
    def run(self, cmd, *, env=None, timeout=0, check=True):
        cmd = [str(c) for c in cmd]
        if "\\" in cmd[0] and PureWindowsPath(cmd[0]).name in ("sc.exe", "icacls.exe"):
            assert PureWindowsPath(cmd[0]).parent.name.lower() == "system32"  # never a bare name
            cmd[0] = PureWindowsPath(cmd[0]).stem if cmd[0].endswith("icacls.exe") else "sc.exe"
        elif cmd[0] in ("sc.exe", "icacls", "powershell"):
            raise AssertionError(f"{cmd[0]} run by bare name in elevated code")
        self.calls.append(cmd)
        if cmd[0] == "icacls":  # like Windows: a service SID resolves only while that service exists
            for arg in cmd:
                sid = arg[1:].split(":")[0] if arg.startswith("*S-1-5-80-") else None
                if sid and (self.service is None or sid != self.account_sid_value):
                    raise SetupError(f"Command failed (exit 1332): icacls ... *{sid}: No mapping between "
                                     "account names and security IDs was done.")
        if self.fail_pip and "pip" in cmd:
            raise SetupError("python -I -m pip failed (exit 1): no matching distribution")
        out = ""
        if cmd[1:3] == ["-I", "-c"] and "version_info" in cmd[3]:
            out = f"{sys.version_info[0]}.{sys.version_info[1]}\n"
        elif cmd[1:4] == ["-I", "-m", "venv"]:
            python = Path(cmd[4]) / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("")
        elif "register-local-build" in cmd:
            args = dict(zip(cmd[cmd.index("register-local-build") + 1::2],
                            cmd[cmd.index("register-local-build") + 2::2]))
            prefix = Path(args["--prefix"])
            version = prefix / "versions" / args["--version"]
            (version / "INSTALLED.json").write_text("{}")
            (version / ".incomplete").unlink(missing_ok=True)
            (prefix / "installation.json").write_text(json.dumps({
                "schema": 1, "edition": "platform_client", "data_dir": args["--data-dir"],
                "account": args["--account"], "service": args["--service"], "port": int(args["--port"])}))
            (prefix / "current").write_text(args["--version"])
            (prefix / "bin").mkdir(exist_ok=True)
        elif cmd[-1] == "init":
            data = Path(env["NWPC_DATA_DIR"])
            from nature_worker.installation_mode import initialize_platform_client
            initialize_platform_client(data)
        elif cmd[0] == "sc.exe" and cmd[1] in ("create", "config"):
            self.service = self.service or 1
            key = rf"SYSTEM\CurrentControlSet\Services\{cmd[2]}"
            self.registry.setdefault(key, {})["ObjectName"] = cmd[cmd.index("obj=") + 1]
            self.account_sid_value = service_sid(cmd[2])
        elif cmd[0] == "sc.exe" and cmd[1] == "sidtype":
            self.registry.setdefault(rf"SYSTEM\CurrentControlSet\Services\{cmd[2]}", {})["ServiceSidType"] = \
                {"unrestricted": 1, "restricted": 3, "none": 0}[cmd[3]]
        elif cmd[0] == "sc.exe" and cmd[1] == "delete":
            self.service = None
            self.registry.pop(rf"SYSTEM\CurrentControlSet\Services\{cmd[2]}", None)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def set_protected_dacl(self, path, owner, grants):
        self.calls.append(["set_protected_dacl", str(path), owner, *[f"{sid}:{mask:#x}" for sid, mask in grants]])
        self.acls[Path(path)] = Acl(owner, True, tuple(Ace(sid, mask, flags=3) for sid, mask in grants))

    def account_sid(self, name):
        if self.service is None:
            raise SetupError(f"Windows cannot resolve the account {name} (error 1332)")
        return self.resolve_override or service_sid(name.split("\\", 1)[1])

    # service
    def service_status(self, name):
        self.names.add(name)
        return Status(self.service) if self.service else None

    def start_service(self, name, timeout):
        self.names.add(name)
        self.service = 4
        # Like Windows: the first start creates the virtual account's profile and registers it.
        sid = service_sid(name)
        profile = self.profiles / name
        if not profile.exists():
            (profile / "AppData" / "Local" / "Temp").mkdir(parents=True)
            (profile / "NTUSER.DAT").write_bytes(b"\0" * 64)
            (profile / "ntuser.dat.LOG1").write_bytes(b"")
            self.registry[rf"{windows_profile.PROFILE_LIST}\{sid}"] = {
                "ProfileImagePath": rf"%SystemRoot%\ServiceProfiles\{name}"}

    def user_hive_loaded(self, sid):
        return sid in self.loaded_hives

    def delete_profile(self, sid):
        self.calls.append(["DeleteProfileW", sid])
        key = rf"{windows_profile.PROFILE_LIST}\{sid}"
        path = self.registry.pop(key)["ProfileImagePath"]
        shutil.rmtree(self.profiles / PureWindowsPath(path).name)

    def stop_service(self, name, timeout):
        self.names.add(name)
        if self.service:
            self.service = 1

    def http_status(self, port):
        if self.service != 4:
            return None
        current = (self.prefix / "current").read_text()
        if current in self.start_fails_for:
            return None
        key = next(k for k, v in self.registry.items() if k.startswith("SYSTEM") and "Environment" in v)
        env = dict(v.split("=", 1) for v in self.registry[key]["Environment"])
        status = {"version": current.split("+")[0], "build": current, "platform": env["NWPC_PLATFORM_ORIGIN"]}
        return self.status_override(status) if self.status_override else status

    def http_probe(self, port):
        status = self.http_status(port)
        if status is None:
            return {"error": "ConnectionRefusedError: [WinError 10061]"}
        return {"http": 200, "body": json.dumps(status), "json": status}

    def port_in_use(self, port):
        return port in self.busy_ports

    def platform_client_processes(self):
        return []

    # registry
    def set_registry(self, key, values):
        self.registry.setdefault(key, {}).update(values)

    def get_registry(self, key, name):
        return self.registry.get(key, {}).get(name)

    def delete_registry_tree(self, key):
        self.registry.pop(key, None)

    def sleep(self, _s):
        pass


def source_archive(tmp_path, commit="a" * 40, version="0.2.0"):
    src = tmp_path / f"src-{commit[:6]}"
    (src / "src" / "nature_worker").mkdir(parents=True, exist_ok=True)
    (src / "src" / "nature_worker" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (src / "requirements.lock").write_text("")
    tar = tmp_path / f"src-{commit[:6]}.tar"
    with tarfile.open(tar, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as t:
        t.add(src, arcname=".")
    return tar


@pytest.fixture
def win(tmp_path, tmp_path_factory, monkeypatch, request):
    monkeypatch.setattr(windows_setup, "start_menu", lambda: tmp_path / "start")
    monkeypatch.delenv("NATURE_WORKER_INSTALL_PREFIX", raising=False)
    profiles = short_profiles_root(tmp_path_factory)
    monkeypatch.setenv("SystemRoot", str(profiles.parent))  # ProfileList: %SystemRoot%\ServiceProfiles\...
    monkeypatch.setattr(windows_profile, "profiles_root", lambda: profiles)
    fake = FakeWindows(tmp_path)
    fake.profiles = profiles
    service = getattr(request, "param", None)
    setup = Setup(tmp_path / "Program Files" / "PC", tmp_path / "ProgramData" / "NatureAIWorker" / "PC",
                  service=service, system=fake, say=lambda _m: None)
    fake.prefix, fake.private_sid = setup.prefix, setup.sid
    # The runtime copy is the installer's own interpreter; emulate it without copying Python.
    def runtime():
        setup.runtime_python.parent.mkdir(parents=True, exist_ok=True)
        setup.runtime_python.write_text("")
    monkeypatch.setattr(setup, "ensure_runtime", runtime)
    standalone = tmp_path / "standalone-data"
    standalone.mkdir()
    (standalone / "worker.db").write_text("standalone work")
    fake.acls[standalone] = Acl(USER, True, (Ace(USER, FULL),))
    return setup, fake, standalone


def install(setup, standalone, tmp_path, **kwargs):
    kwargs.setdefault("archive", source_archive(tmp_path))
    return setup.install(origin="https://platform.example", standalone_data=standalone, **kwargs)


def test_install_creates_an_isolated_service_installation(win, tmp_path):
    setup, fake, standalone = win
    before = {p: p.read_bytes() for p in standalone.rglob("*") if p.is_file()}
    label = install(setup, standalone, tmp_path)
    assert label == "0.2.0+gaaaaaaaaaaaa"
    sc = [c for c in fake.calls if c[0] == "sc.exe"]
    create = next(c for c in sc if c[1] == "create")
    assert create[create.index("obj=") + 1] == ACCOUNT
    assert f'"{setup.runtime_python}" -I "{setup.prefix / "bin" / "platform-client-service.py"}"' in create
    assert ["sc.exe", "privs", "NatureWorkerPlatformClient", "SeChangeNotifyPrivilege"] in sc
    assert ["sc.exe", "sidtype", "NatureWorkerPlatformClient", "unrestricted"] in sc
    grant = next(c for c in fake.calls if c[0] == "set_protected_dacl" and c[1] == str(setup.data))
    assert grant[2:] == [ADMINISTRATORS, f"{SYSTEM}:0x1f01ff", f"{ADMINISTRATORS}:0x1f01ff", f"{SVC}:0x1301bf"]
    env = dict(v.split("=", 1) for v in fake.registry[setup.service_key]["Environment"])
    assert env["NWPC_STANDALONE_SID"] == USER and env["NWPC_DATA_DIR"] == str(setup.data)
    assert env["NWPC_HOST"] == "127.0.0.1" and env["NWPC_PORT"] == "8090"
    assert fake.registry[setup.uninstall_key]["DisplayVersion"] == label
    assert json.loads((setup.data / "installation-mode.json").read_text())["mode"] == "platform_client"
    assert not (setup.prefix / "upgrade" / "staging" / "windows-install").exists()
    # The Standalone installation is never touched.
    assert {p: p.read_bytes() for p in standalone.rglob("*") if p.is_file()} == before
    assert not any(str(standalone) in arg for c in fake.calls for arg in c if c[0] != "sc.exe")


@pytest.mark.parametrize("win", ["NatureWorkerPlatformClientE2E"], indirect=True)
def test_a_separate_namespace_never_touches_the_production_names(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    setup.uninstall(delete_local_data=True)
    e2e_sid = service_sid("NatureWorkerPlatformClientE2E")
    assert setup.account == "NT SERVICE\\NatureWorkerPlatformClientE2E" and setup.sid == e2e_sid != SVC
    assert fake.names == {"NatureWorkerPlatformClientE2E"}
    sc_names = {c[2] for c in fake.calls if c[0] == "sc.exe"}
    assert sc_names == {"NatureWorkerPlatformClientE2E"}
    assert all(k.endswith("E2E") for k in [setup.service_key, setup.uninstall_key])
    assert not any(f"*{SVC}" in arg for c in fake.calls for arg in c)  # the production account is never granted
    assert setup.shortcut.name == "Nature AI Worker Platform Client E2E.url"
    assert not (tmp_path / "Program Files").exists()  # empty parents created by the install are removed


def test_install_refusals(win, tmp_path):
    setup, fake, standalone = win
    with pytest.raises(SetupError, match="https"):
        setup.install(origin="http://100.64.0.1:8000", standalone_data=standalone)
    fake.admin = False
    with pytest.raises(SetupError, match="administrator"):
        install(setup, standalone, tmp_path)
    fake.admin = True
    fake.busy_ports.add(8090)
    with pytest.raises(SetupError, match="already used"):
        install(setup, standalone, tmp_path)
    fake.busy_ports.clear()
    setup.data.mkdir(parents=True)
    (setup.data / "installation-mode.json").write_text('{"schema": 1, "mode": "standalone"}')
    with pytest.raises(SetupError, match="not a Platform Client installation"):
        install(setup, standalone, tmp_path)
    assert fake.service is None and not (setup.prefix / "current").exists()


def test_install_refuses_storage_prepared_by_a_non_administrator(win, tmp_path):
    setup, fake, standalone = win
    setup.data.parent.mkdir(parents=True)
    fake.acls[setup.data.parent] = Acl(USER, False, (Ace(USER, FULL),))
    with pytest.raises(SetupError, match="non-administrator"):
        install(setup, standalone, tmp_path)


def test_install_refuses_an_archive_without_its_commit(win, tmp_path):
    setup, _, standalone = win
    tar = tmp_path / "plain.tar"
    with tarfile.open(tar, "w") as t:
        t.addfile(tarfile.TarInfo("README"), io.BytesIO(b""))
    with pytest.raises(SetupError, match="does not name its commit"):
        install(setup, standalone, tmp_path, archive=tar)


def test_failed_reinstall_reactivates_the_previous_build(win, tmp_path):
    setup, fake, standalone = win
    first = install(setup, standalone, tmp_path)
    fake.start_fails_for.add("0.2.0+gbbbbbbbbbbbb")
    with pytest.raises(SetupError, match="did not answer"):
        install(setup, standalone, tmp_path, archive=source_archive(tmp_path, "b" * 40))
    assert setup.current() == first and fake.service == 4


def test_reinstall_that_fails_to_build_restarts_the_running_build(win, tmp_path):
    setup, fake, standalone = win
    first = install(setup, standalone, tmp_path)
    fake.fail_pip = True
    with pytest.raises(SetupError, match="pip"):
        install(setup, standalone, tmp_path, archive=source_archive(tmp_path, "c" * 40))
    assert setup.current() == first and fake.service == 4 and fake.http_status(8090)["build"] == first


@pytest.mark.parametrize("win", ["NatureWorkerPlatformClientUIAcceptance"], indirect=True)
def test_reinstall_over_a_running_named_namespace_keeps_data_identity_and_privacy(win, tmp_path):
    """The UIAcceptance update (6fca3ff -> b145c63): same service, SID, port and data; the data
    root's DACL is rewritten to exactly the private entries; the old build stays for rollback."""
    setup, fake, standalone = win
    first = install(setup, standalone, tmp_path, port=8091)
    (setup.data / "kept.json").write_text("kept")
    fake.busy_ports.add(8091)  # held by this namespace's own running service
    sid, env_before = setup.sid, fake.registry[setup.service_key]["Environment"]
    fake.calls.clear()
    second = install(setup, standalone, tmp_path, port=8091, archive=source_archive(tmp_path, "e" * 40))
    assert second == "0.2.0+geeeeeeeeeeee" != first and setup.current() == second and fake.service == 4
    assert (setup.data / "kept.json").read_text() == "kept"
    assert fake.http_status(8091)["build"] == second
    assert setup.sid == sid == service_sid("NatureWorkerPlatformClientUIAcceptance")
    assert any(c[:3] == ["sc.exe", "config", setup.service] for c in fake.calls)
    assert not any(c[:2] == ["sc.exe", "create"] for c in fake.calls)
    assert fake.registry[setup.service_key]["Environment"] == env_before  # same origin, port, Standalone SID
    acl = fake.acls[setup.data]
    assert acl.protected and acl.grantees() == {SYSTEM, ADMINISTRATORS, sid}
    assert private_problems(acl, sid) == []
    assert (setup.prefix / "versions" / first).is_dir()  # kept for rollback
    assert "UIAcceptance" in setup.uninstall_key and "UIAcceptance" in setup.shortcut.name


def test_repair_reapplies_everything_but_keeps_version_origin_and_data(win, tmp_path):
    setup, fake, standalone = win
    label = install(setup, standalone, tmp_path)
    (setup.data / "conversations.json").write_text("kept")
    fake.calls.clear()
    assert setup.repair() == label
    assert any(c[:2] == ["sc.exe", "config"] for c in fake.calls)
    assert any(c[0] == "set_protected_dacl" and c[1] == str(setup.data) for c in fake.calls)
    env = dict(v.split("=", 1) for v in fake.registry[setup.service_key]["Environment"])
    assert env["NWPC_PLATFORM_ORIGIN"] == "https://platform.example"
    assert (setup.data / "conversations.json").read_text() == "kept"


def test_uninstall_keeps_data_unless_asked_and_reports_clean_validation(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    report = setup.uninstall(delete_local_data=False)
    assert fake.service is None and not setup.prefix.exists()
    assert setup.uninstall_key not in fake.registry
    assert setup.data.exists() and report["failures"] == []
    assert report["checks"]["other_app_locations"] is False and not report["standalone_install_allowed"]
    report = setup.uninstall(delete_local_data=True)
    assert not setup.data.exists() and report["checks"]["other_app_locations"] is True
    # Enterprise device policy cannot be verified locally, so a Standalone reinstall stays blocked.
    assert report["checks"]["device_policy"] is None and not report["standalone_install_allowed"]
    assert standalone.exists() and (standalone / "worker.db").read_text() == "standalone work"


# -- the service account's Windows profile -----------------------------------------------

def profile_of(setup, fake):
    return fake.profiles / setup.service


def test_ordinary_uninstall_keeps_the_service_profile_and_says_so(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    profile = profile_of(setup, fake)
    assert (profile / "NTUSER.DAT").exists()  # Windows made it on the first start
    report = setup.uninstall(delete_local_data=True)
    assert (profile / "NTUSER.DAT").exists() and not any(c[0] == "DeleteProfileW" for c in fake.calls)
    summary = report["service_profile"]
    assert summary["exists"] and summary["removable"] and summary["credential_files"] == []
    # No credential file in the only place the service's Credential Manager entries could be.
    assert report["checks"]["os_credentials"] is True and report["failures"] == []


def test_remove_service_profile_uses_deleteprofile_for_the_verified_profile_only(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    sibling = fake.profiles / "LocalService"
    (sibling / "AppData").mkdir(parents=True)
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert ["DeleteProfileW", setup.sid] in fake.calls and report["failures"] == []
    assert not profile_of(setup, fake).exists()
    assert rf"{windows_profile.PROFILE_LIST}\{setup.sid}" not in fake.registry
    assert (sibling / "AppData").is_dir()  # no other ServiceProfiles folder is touched
    assert report["service_profile"]["exists"] is False


def credential_file(profile):
    path = profile / "AppData" / "Local" / "Microsoft" / "Credentials" / "0123ABCD"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"opaque")


def dpapi_key(profile):
    path = profile / "AppData" / "Roaming" / "Microsoft" / "Protect" / "S-1-5-80-1" / "key"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"opaque")


def other_data(profile):
    (profile / "Documents").mkdir()
    (profile / "Documents" / "notes.txt").write_text("someone's")


@pytest.mark.parametrize("damage, reason, os_credentials", [
    (credential_file, "Credential Manager file", False),
    (dpapi_key, "DPAPI master keys and the data root is kept", True),
    (other_data, "files Windows does not create: Documents", True),
])
def test_remove_service_profile_refuses_meaningful_state(win, tmp_path, damage, reason, os_credentials):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    profile = profile_of(setup, fake)
    damage(profile)
    report = setup.uninstall(delete_local_data=False, remove_service_profile=True)
    assert not any(c[0] == "DeleteProfileW" for c in fake.calls)
    assert any("service account profile" in f and reason in f for f in report["failures"])
    assert report["checks"]["os_credentials"] is os_credentials
    files = [p for p in profile.rglob("*") if p.is_file()]
    assert files and all(p.read_bytes() for p in files if p.name != "ntuser.dat.LOG1")  # nothing deleted


def test_dpapi_keys_alone_do_not_block_removal_when_the_data_is_deleted_too(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    dpapi_key(profile_of(setup, fake))
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert report["failures"] == [] and not profile_of(setup, fake).exists()


@pytest.mark.parametrize("tamper, reason", [
    ("elsewhere", "ProfileList names"),
    ("unregistered", "ProfileList has no entry"),
    ("owner", "owned by"),
    ("ungranted", "do not grant the service account"),
    ("loaded", "registry hive loaded"),
])
def test_remove_service_profile_refuses_unverified_identity(win, tmp_path, tamper, reason):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    profile, key = profile_of(setup, fake), rf"{windows_profile.PROFILE_LIST}\{setup.sid}"
    other = tmp_path / "Users" / "someone"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("not ours")
    if tamper == "elsewhere":
        fake.registry[key]["ProfileImagePath"] = str(other)
    elif tamper == "unregistered":
        del fake.registry[key]
    elif tamper == "owner":
        fake.acls[profile] = Acl(USER, True, (Ace(SYSTEM, FULL), Ace(setup.sid, FULL)))
    elif tamper == "ungranted":
        fake.acls[profile] = Acl(SYSTEM, True, (Ace(SYSTEM, FULL), Ace(ADMINISTRATORS, FULL)))
    else:
        fake.loaded_hives.add(setup.sid)
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert not any(c[0] == "DeleteProfileW" for c in fake.calls)
    assert any(reason in f for f in report["failures"]), report["failures"]
    assert (profile / "NTUSER.DAT").exists() and (other / "keep.txt").read_text() == "not ours"


def test_remove_service_profile_refuses_a_linked_profile_folder(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    profile = profile_of(setup, fake)
    target = tmp_path / "elsewhere"
    profile.rename(target)
    try:
        os.symlink(target, profile, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links not permitted")
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert not any(c[0] == "DeleteProfileW" for c in fake.calls)
    assert any("link or junction" in f for f in report["failures"])
    assert (target / "NTUSER.DAT").exists()


# The profile Windows really created for the e2e service (native inspection at 8fa4a0f): standard
# empty folders, desktop.ini files, compatibility junctions, registry hives and default shortcuts.
OBSERVED_PROFILE = json.loads((Path(__file__).parent / "data" / "windows_service_profile_e2e.json")
                              .read_text(encoding="utf-8"))


# Windows' MAX_PATH: 259 characters plus the terminating NUL, unless long paths are enabled.
MAX_PATH = 260


def short_profiles_root(tmp_path_factory):
    """A short folder for synthetic service profiles (never the real ServiceProfiles). Under
    tmp_path (pytest's per-test folder, named after the test) the observed profile's longest file,
    126 characters below the profile, reached 261 characters natively and could not be created."""
    root = tmp_path_factory.mktemp("sr") / "ServiceProfiles"
    assert windows_profile.profiles_root() not in (root, *root.parents)
    root.mkdir()
    return root


def build_observed_profile(profile, make_link):
    """The observed profile, rebuilt: every file (sizes shortened), folder and junction, each
    junction pointing to the folder Windows points it to."""
    too_long = [e for e in OBSERVED_PROFILE["entries"]
                if len(str(profile.joinpath(*PureWindowsPath(e.rpartition(" (")[0]).parts))) >= MAX_PATH]
    if too_long:
        raise ValueError(f"{profile} is too deep for Windows' MAX_PATH ({MAX_PATH}): {too_long[0]}")
    for folder in OBSERVED_PROFILE["empty_folders"]:
        (profile / folder).mkdir(parents=True, exist_ok=True)
    links = []
    for entry in OBSERVED_PROFILE["entries"]:
        rel, _, kind = entry.rpartition(" (")
        path = profile.joinpath(*PureWindowsPath(rel).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "link, not followed)":
            links.append((rel, path))
        else:
            path.write_bytes(b"\0" * min(int(kind.split()[0]), 16))
    for rel, path in links:
        target = profile.joinpath(*windows_profile._WINDOWS_LINKS[rel.lower().replace("\\", "/")].split("/"))
        target.mkdir(parents=True, exist_ok=True)
        make_link(target, path)
    return len(links)


def test_observed_profile_fixture_fits_windows_max_path(tmp_path_factory):
    """Regression (native run at 39ab6bb: 11 FileNotFoundError): every observed path fits MAX_PATH
    under the fixtures' root, and a root too deep is refused with the reason, not FileNotFoundError."""
    root = short_profiles_root(tmp_path_factory) / "NatureWorkerPlatformClient"
    longest = max(len(str(root.joinpath(*PureWindowsPath(e.rpartition(" (")[0]).parts)))
                  for e in OBSERVED_PROFILE["entries"])
    assert longest < MAX_PATH
    deep = root / ("d" * (MAX_PATH - len(str(root)) - 120))
    with pytest.raises(ValueError, match="too deep for Windows' MAX_PATH"):
        build_observed_profile(deep, symlink)
    assert not deep.exists()  # refused before creating anything


def symlink(target, path):
    try:
        os.symlink(target, path, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links not permitted")


@pytest.fixture
def observed(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    profile = profile_of(setup, fake)
    shutil.rmtree(profile)
    profile.mkdir()
    assert build_observed_profile(profile, symlink) == 17
    return setup, fake, profile


def test_the_observed_windows_profile_is_all_windows_standard(observed):
    setup, fake, profile = observed
    setup.uninstall(delete_local_data=True)
    inspected = setup.service_profile()
    assert inspected.other_data == [] and inspected.bad_links == [] and inspected.problems == []
    assert sum("(link, not followed)" in f for f in inspected.files) == 17
    # Desktop, Documents, Downloads, ... exist and are empty: standard, not data.
    assert all((profile / folder).is_dir() for folder in ("Desktop", "Documents", "Downloads", "Saved Games"))


def test_the_observed_profile_is_removed_on_request_once_its_hive_is_unloaded(observed):
    setup, fake, profile = observed
    fake.loaded_hives.add(setup.sid)  # as observed natively after the service was deleted
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert report["service_profile"]["problems"] == [
        "Windows has its registry hive loaded (restart Windows, then try again)"]
    assert profile.is_dir() and not any(c[0] == "DeleteProfileW" for c in fake.calls)
    fake.loaded_hives.clear()  # after a restart
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert report["failures"] == [] and ["DeleteProfileW", setup.sid] in fake.calls and not profile.exists()


@pytest.mark.parametrize("rel, reason", [
    ("Documents/notes.txt", "files Windows does not create: Documents"),
    ("Desktop/report.pdf", "files Windows does not create: Desktop"),
    ("AppData/Local/Temp/nwpc.tmp", "files Windows does not create: AppData"),
    ("AppData/Roaming/Vendor/settings.json", "files Windows does not create: AppData"),
    ("notes.txt", "files Windows does not create: notes.txt"),
])
def test_a_file_windows_does_not_create_blocks_removal(observed, rel, reason):
    setup, fake, profile = observed
    path = profile.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert any(reason in f for f in report["failures"]), report["failures"]
    assert not any(c[0] == "DeleteProfileW" for c in fake.calls) and (profile / "NTUSER.DAT").exists()


@pytest.mark.parametrize("link, target", [
    ("Documents/My Music", "outside"),          # a Windows junction name, pointing out of the profile
    ("Documents/My Music", "Documents"),        # pointing to the wrong folder of the profile
    ("Desktop/shortcut", "Music"),              # not a Windows junction at all
])
def test_a_link_that_is_not_windows_own_junction_blocks_removal(observed, tmp_path, link, target):
    setup, fake, profile = observed
    path = profile.joinpath(*link.split("/"))
    if path.is_symlink():
        path.unlink()
    destination = tmp_path / "outside" if target == "outside" else profile / target
    destination.mkdir(exist_ok=True)
    symlink(destination, path)
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert any("not Windows' own junctions" in f and Path(link).name in f for f in report["failures"])
    assert not any(c[0] == "DeleteProfileW" for c in fake.calls)


def test_a_kept_profile_never_fails_an_ordinary_uninstall_or_blocks_a_new_installation(observed, tmp_path):
    setup, fake, profile = observed
    (profile / "Documents" / "notes.txt").write_text("someone's")  # not removable
    fake.loaded_hives.add(setup.sid)
    report = setup.uninstall(delete_local_data=True)
    assert report["failures"] == [] and report["service_profile"]["removable"] is False
    # Kept profiles are no residue of the Worker: credentials verified, locations clean.
    assert report["checks"]["os_credentials"] is True and report["checks"]["other_app_locations"] is True
    standalone = tmp_path / "Standalone"
    install(setup, standalone, tmp_path)  # Windows reuses the profile for the same service SID
    assert fake.service == 4 and (profile / "Documents" / "notes.txt").read_text() == "someone's"


def test_an_inspection_that_fails_never_fails_the_uninstall(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)

    def denied(_sid):
        raise PermissionError(5, "Access is denied")
    fake.user_hive_loaded = denied
    report = setup.uninstall(delete_local_data=True, remove_service_profile=True)
    assert not setup.prefix.exists() and not setup.data.exists()
    assert any("cannot be read" in f for f in report["failures"])
    assert not any(c[0] == "DeleteProfileW" for c in fake.calls)


def test_remove_service_profile_is_refused_while_the_service_is_registered(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    profile = windows_profile.inspect(fake, setup.service, setup.sid, service_registered=True, data_kept=False)
    assert any("still registered" in p for p in profile.problems)
    with pytest.raises(ValueError):
        windows_profile.remove(fake, profile)


def test_uninstall_never_follows_links_out_of_the_data_root(win, tmp_path):
    setup, _, standalone = win
    install(setup, standalone, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours")
    try:
        os.symlink(outside, setup.data / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links not permitted")
    setup.uninstall(delete_local_data=True)
    assert (outside / "keep.txt").read_text() == "not ours"


def test_setup_command_refuses_other_platforms(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert windows_setup.main(["status"]) == 2
    assert "platform-client-linux-install.sh" in capsys.readouterr().err


# -- layout and upgrade toolkit ------------------------------------------------------

def test_windows_platform_client_launchers_and_service_bootstrap(tmp_path, monkeypatch):
    from nature_worker.upgrade.layout import Installation, Layout
    layout = Layout(tmp_path / "PC")
    layout.prefix.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    inst = Installation("platform_client", tmp_path / "data", None, ACCOUNT, "NatureWorkerPlatformClient", 8090)
    names = {p.name for p in layout.write_launchers(inst)}
    assert names == {"nature-worker-platform-client.cmd", "nature-worker-upgrade.cmd",
                     "nature-worker-platform-client-setup.cmd", "platform-client-service.py"}
    bootstrap = (layout.bin / "platform-client-service.py").read_text()
    assert "winservice import main" in bootstrap and 'PREFIX / "current"' in bootstrap
    # The bootstrap refuses a tampered pointer before importing anything from it.
    (layout.prefix / "current").write_text("1.0.0;evil")
    result = subprocess.run([sys.executable, "-I", str(layout.bin / "platform-client-service.py")],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "Invalid active version" in result.stderr


def test_standalone_launchers_on_windows_have_no_platform_client_parts(tmp_path, monkeypatch):
    from nature_worker.upgrade.layout import Installation, Layout
    layout = Layout(tmp_path / "SA")
    layout.prefix.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    inst = Installation("standalone", tmp_path / "data", tmp_path / "ws")
    # The desktop launcher is the Windows installer's Start-menu entry; no service, no setup command.
    assert {p.name for p in layout.write_launchers(inst)} == {"nature-worker.cmd", "nature-worker-upgrade.cmd",
                                                             "nature-worker-desktop.cmd"}


class Recorder:
    def __init__(self):
        self.calls = []

    def run(self, cmd, *, user=None, env=None, timeout=900):
        self.calls.append((user, [str(c) for c in cmd]))
        return subprocess.CompletedProcess(cmd, 0, '{"data_schema": 1}\n', "")


def test_windows_toolkit_selection_data_work_and_permission_hand_back(tmp_path, monkeypatch):
    from nature_worker.upgrade import toolkit as tk
    from upgrade_fixtures import managed_install
    layout, _ = managed_install(tmp_path)
    inst = tk.Installation("platform_client", tmp_path / "pc", None, ACCOUNT, "NatureWorkerPlatformClient", 8090)
    inst.data_dir.mkdir()
    (inst.data_dir / "worker.db").write_text("")
    monkeypatch.setattr(sys, "platform", "win32")
    runner = Recorder()
    toolkit = tk.toolkit_for(layout, inst, runner=runner)
    assert isinstance(toolkit, tk.WindowsPlatformClientToolkit)
    toolkit.migrate("1.0.0")
    assert runner.calls[-1][0] is None  # no runuser: administrators may work in the data root
    toolkit._hand_back(inst.data_dir)
    toolkit._hand_back(inst.data_dir / "backups")  # missing: nothing to do
    resets = [cmd for _, cmd in runner.calls if cmd[0].endswith("icacls.exe")]
    assert resets == [[resets[0][0], str(inst.data_dir / "*"), "/reset", "/T", "/C", "/Q"]]  # never the root itself
    assert PureWindowsPath(resets[0][0]).parent.name.lower() == "system32"


def test_linux_toolkit_is_unchanged_on_linux(tmp_path, monkeypatch):
    from nature_worker.upgrade import toolkit as tk
    from upgrade_fixtures import managed_install
    layout, _ = managed_install(tmp_path)
    inst = tk.Installation("platform_client", tmp_path / "pc", None, "nature-pc", "nwpc.service", 8090)
    monkeypatch.setattr(sys, "platform", "linux")
    assert type(tk.toolkit_for(layout, inst, runner=Recorder())) is tk.PlatformClientToolkit


# -- service host ----------------------------------------------------------------------

class FakeServer:
    def __init__(self):
        self.started = False
        self.should_exit = False

    def run(self):
        self.started = True
        while not self.should_exit:
            time.sleep(0.05)


class FakeWatcher:
    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NWPC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NWPC_PLATFORM_ORIGIN", "https://platform.example")
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)


def test_service_reports_ready_only_when_serving_and_stops_on_request(tmp_path, monkeypatch):
    from nature_worker.platform_client import __main__ as entry
    service_env(monkeypatch, tmp_path)
    server, watcher = FakeServer(), FakeWatcher()
    monkeypatch.setattr(entry, "build_server", lambda _s: (server, watcher))
    stop, ready = threading.Event(), threading.Event()
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", winservice.run_service(stop, ready.set)))
    thread.start()
    assert ready.wait(5) and watcher.running
    stop.set()
    thread.join(5)
    assert result["code"] == 0 and server.should_exit and not watcher.running
    log = winservice.service_log(tmp_path).read_text()
    assert "starting" in log and "serving on 127.0.0.1:8090" in log and "stopped" in log


def test_service_blocked_by_preflight_logs_why_and_never_reports_ready(tmp_path, monkeypatch):
    from nature_worker.platform_client import __main__ as entry
    service_env(monkeypatch, tmp_path)

    def blocked(_settings):
        raise ModeError("Platform Client startup blocked: os-execution-isolation-unverified")
    monkeypatch.setattr(entry, "build_server", blocked)
    ready = threading.Event()
    assert winservice.run_service(threading.Event(), ready.set) == winservice.EXIT_BLOCKED
    assert not ready.is_set()
    assert "os-execution-isolation-unverified" in winservice.service_log(tmp_path).read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_service_host_requires_windows():
    from nature_worker.windows_service import ServiceError, ServiceHost
    with pytest.raises(ServiceError):
        ServiceHost("x", lambda *_: 0).dispatch()


# -- native Windows APIs (Windows only) --------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@windows_only
def test_native_token_and_acl_reads(tmp_path):
    from nature_worker.config import current_user_sid, restrict_to_owner
    from nature_worker.windows_security import current_token, read_acl
    token = current_token()
    assert token.user == current_user_sid()
    folder = tmp_path / "private"
    folder.mkdir()
    restrict_to_owner(folder)
    acl = read_acl(folder)
    assert acl.protected and acl.grantees() == {token.user, SYSTEM, ADMINISTRATORS}
    assert private_problems(acl, token.user) == [] or acl.owner not in (SYSTEM, ADMINISTRATORS)


@windows_only
def test_native_service_query_of_a_builtin_and_a_missing_service():
    from nature_worker import windows_service
    assert windows_service.query("EventLog").name == "running"
    assert windows_service.query("NatureWorkerNoSuchService") is None


@windows_only
def test_service_host_outside_the_scm_says_how_to_start_it():
    from nature_worker.windows_service import ServiceError, ServiceHost
    with pytest.raises(ServiceError, match="sc.exe start"):
        ServiceHost("NatureWorkerTest", lambda *_: 0).dispatch()


@windows_only
def test_native_service_profile_inspection_is_read_only():
    """LocalService's own profile, as Windows registers it: identity matched, never removable
    (its hive is loaded), and nothing is deleted. Without elevation the folder may be unreadable."""
    import ctypes

    class NoDelete(windows_setup.WindowsSystem):
        def delete_profile(self, sid):
            raise AssertionError("an inspection must never delete")

    system = NoDelete()
    assert system.user_hive_loaded("S-1-5-19") is True
    assert system.user_hive_loaded("S-1-5-80-0-0-0-0-1") is False
    profile = windows_profile.inspect(system, "LocalService", "S-1-5-19", service_registered=False,
                                      data_kept=False)
    assert profile.exists and profile.registered
    assert not any("ProfileList" in p for p in profile.problems), profile.problems
    assert profile.problems  # loaded, or unreadable without elevation: never removable
    assert profile.unreadable or profile.loaded
    assert ctypes.WinDLL("userenv").DeleteProfileW  # the Windows API the removal uses exists


class NativeProfileSystem(windows_setup.WindowsSystem):
    """The real file system, links and junctions; the profile's registration, owner and hive
    state as Windows reported them for the e2e profile. Never deletes anything."""

    def __init__(self, root, sid):
        self.root, self.sid = root, sid

    def get_registry(self, key, name):
        if key.endswith(self.sid) and name == "ProfileImagePath":
            return str(self.root / "NatureWorkerPlatformClientE2E")
        return None

    def read_acl(self, path):
        return Acl(OBSERVED_PROFILE["owner"], True, (Ace(SYSTEM, FULL), Ace(ADMINISTRATORS, FULL), Ace(self.sid, FULL)))

    def user_hive_loaded(self, sid):
        return False

    def delete_profile(self, sid):
        raise AssertionError("an inspection must never delete")


def junction(target, path):
    import _winapi
    _winapi.CreateJunction(str(target), str(path))


@windows_only
def test_native_observed_profile_with_real_junctions_is_windows_standard(tmp_path_factory):
    sid = service_sid("NatureWorkerPlatformClientE2E")
    tmp_path = short_profiles_root(tmp_path_factory)
    profile = tmp_path / "NatureWorkerPlatformClientE2E"
    profile.mkdir()
    assert build_observed_profile(profile, junction) == 17
    system = NativeProfileSystem(tmp_path, sid)
    inspected = windows_profile.inspect(system, "NatureWorkerPlatformClientE2E", sid, root=tmp_path,
                                        service_registered=False, data_kept=False)
    assert inspected.bad_links == [] and inspected.other_data == [] and inspected.problems == []
    assert sum("(link, not followed)" in f for f in inspected.files) == 17  # NTFS junctions seen as links
    # A real junction with a Windows name, pointing out of the profile, blocks removal.
    outside = tmp_path / "outside"
    outside.mkdir()
    os.rmdir(profile / "Documents" / "My Music")  # removes the junction, not its target
    assert (profile / "Music").is_dir()
    junction(outside, profile / "Documents" / "My Music")
    inspected = windows_profile.inspect(system, "NatureWorkerPlatformClientE2E", sid, root=tmp_path,
                                        service_registered=False, data_kept=False)
    assert inspected.bad_links == [str(PureWindowsPath("Documents", "My Music"))]


@windows_only
def test_native_e2e_service_profile_holds_only_windows_standard_content():
    """Read-only, when the real e2e profile exists and this prompt may list it (elevated)."""
    profile = windows_profile.profiles_root() / "NatureWorkerPlatformClientE2E"
    try:
        os.listdir(profile)
    except FileNotFoundError:
        pytest.skip("no e2e service profile on this computer")
    except PermissionError:
        pytest.skip("listing the e2e service profile needs an elevated prompt")

    class NoDelete(windows_setup.WindowsSystem):
        def delete_profile(self, sid):
            raise AssertionError("an inspection must never delete")
    sid = service_sid("NatureWorkerPlatformClientE2E")
    inspected = windows_profile.inspect(NoDelete(), "NatureWorkerPlatformClientE2E", sid,
                                        service_registered=False, data_kept=False)
    assert inspected.registered and not any("ProfileList" in p for p in inspected.problems)
    assert inspected.credential_files == [] and inspected.other_data == [] and inspected.bad_links == []


# -- runtime copy and diagnostics (first native e2e run: venv launcher missing) ------------

def fake_python_root(root):
    for rel in ("python.exe", "python311.dll", "Lib/venv/__init__.py", "Lib/venv/scripts/nt/python.exe",
                "Lib/ensurepip/__init__.py", "Lib/ensurepip/_bundled/pip-24.0-py3-none-any.whl",
                "Lib/os.py", "Lib/test/test_os.py", "Lib/site-packages/x/__init__.py", "Scripts/pip.exe",
                "Doc/index.html", "tcl/tk.tcl", "DLLs/_ssl.pyd", "Lib/__pycache__/os.cpython-311.pyc"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")
    return root


def test_runtime_copy_keeps_the_venv_launcher_and_drops_only_exact_paths(tmp_path):
    base, dest = fake_python_root(tmp_path / "base"), tmp_path / "runtime"
    windows_setup.copy_runtime(base, dest)
    assert (dest / "Lib" / "venv" / "scripts" / "nt" / "python.exe").is_file()  # was dropped by "Scripts"
    assert (dest / "Lib" / "os.py").is_file() and (dest / "DLLs" / "_ssl.pyd").is_file()
    for gone in ("Scripts", "Doc", "tcl", "Lib/test", "Lib/site-packages/x", "Lib/__pycache__/os.cpython-311.pyc"):
        assert not (dest / gone).exists(), gone
    assert (dest / "Lib" / "site-packages").is_dir() and windows_setup.runtime_problems(dest) == []


def test_runtime_exclusions_ignore_case_like_windows(tmp_path):
    base = fake_python_root(tmp_path / "base")
    (base / "Scripts").rename(base / "SCRIPTS")
    windows_setup.copy_runtime(base, tmp_path / "runtime")
    assert not (tmp_path / "runtime" / "SCRIPTS").exists()
    assert (tmp_path / "runtime" / "Lib" / "venv" / "scripts" / "nt" / "python.exe").is_file()


def test_an_incomplete_runtime_is_refused_naming_the_missing_file(win, tmp_path, monkeypatch):
    setup, fake, _ = win
    base = fake_python_root(tmp_path / "base")
    (base / "Lib" / "venv" / "scripts" / "nt" / "python.exe").unlink()
    monkeypatch.setattr(windows_setup.sys, "base_prefix", str(base))
    with pytest.raises(SetupError, match=r"incomplete: missing Lib/venv/scripts/nt/python.exe"):
        Setup.ensure_runtime(setup)


def test_command_failures_name_the_command_and_the_missing_program(tmp_path):
    system = windows_setup.WindowsSystem()
    missing = tmp_path / "runtime" / "python.exe"
    with pytest.raises(SetupError, match="does not exist") as info:
        system.run([str(missing), "-I", "-m", "venv", str(tmp_path / "venv")])
    assert str(missing) in str(info.value) and "-m venv" in str(info.value)
    with pytest.raises(SetupError) as info:
        system.run([sys.executable, "-c", "import sys; print('step 1'); sys.stderr.write('Error: boom'); sys.exit(3)"])
    message = str(info.value)
    assert "Command failed (exit 3)" in message and sys.executable in message and "Error: boom" in message


def test_an_unrelated_listener_on_the_port_is_not_a_running_platform_client(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    fake.busy_ports.add(8090)  # e.g. the Linux Platform Client in WSL, forwarded to Windows' localhost
    report = setup.uninstall(delete_local_data=True)
    assert report["checks"]["running_processes"] is True


def test_process_probe_filters_diagnostic_powershell_and_keeps_real_client(monkeypatch):
    """The CIM query must not match its own PowerShell -Command text."""
    seen = {}

    def run(command, **kwargs):
        seen["script"] = command[-1]
        return subprocess.CompletedProcess(command, 0,
                                           "4242 python.exe -I platform-client-service.py\\n", "")

    system = windows_setup.WindowsSystem()
    monkeypatch.setattr(system, "run", run)
    assert system.platform_client_processes() == [
        "4242 python.exe -I platform-client-service.py"
    ]
    assert "$_.Name -match '^(python|pythonw)(\\\\.exe)?
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    fake.platform_client_processes = lambda: ["4242 python.exe -I platform-client-service.py"]
    report = setup.uninstall(delete_local_data=True)
    assert report["checks"]["running_processes"] is False and report["processes"]


# -- service identity before ACLs (native e2e: icacls exit 1332) -------------------------

def test_the_service_is_registered_and_its_sid_verified_before_any_acl_names_it(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    create = next(i for i, c in enumerate(fake.calls) if c[:2] == ["sc.exe", "create"])
    sidtype = next(i for i, c in enumerate(fake.calls) if c[:2] == ["sc.exe", "sidtype"])
    grant = next(i for i, c in enumerate(fake.calls) if c[0] == "set_protected_dacl" and f"{setup.sid}:0x1301bf" in c)
    assert create < sidtype < grant


def test_granting_a_service_sid_before_the_service_exists_fails_like_windows(win):
    setup, fake, _ = win
    with pytest.raises(SetupError):
        setup.verify_service_identity()  # nothing may name the SID before the service is registered


def test_a_sid_windows_maps_differently_is_never_granted(win, tmp_path):
    setup, fake, standalone = win
    fake.resolve_override = "S-1-5-80-1-2-3-4-5"
    with pytest.raises(SetupError, match="not the expected"):
        install(setup, standalone, tmp_path)
    assert not any(c[0] == "set_protected_dacl" and c[1] == str(setup.data) for c in fake.calls)
    assert fake.service is None  # the first installation removed the service it created


def test_a_service_registered_under_another_account_is_refused(win, tmp_path, monkeypatch):
    setup, fake, standalone = win
    original = fake.run

    def run(cmd, **kwargs):
        result = original(cmd, **kwargs)
        if fake.calls[-1][:2] == ["sc.exe", "create"]:
            fake.registry[setup.service_key]["ObjectName"] = "LocalSystem"
        return result
    monkeypatch.setattr(fake, "run", run)
    with pytest.raises(SetupError, match="LocalSystem"):
        install(setup, standalone, tmp_path)


def test_repair_restores_a_damaged_service_account_before_checking_it(win, tmp_path):
    setup, fake, standalone = win
    label = install(setup, standalone, tmp_path)
    fake.registry[setup.service_key]["ObjectName"] = "LocalSystem"
    fake.registry[setup.service_key]["ServiceSidType"] = 0
    assert setup.repair() == label
    assert fake.registry[setup.service_key]["ObjectName"] == setup.account
    assert fake.registry[setup.service_key]["ServiceSidType"] == 1


@windows_only
def test_native_service_account_resolution():
    from nature_worker.windows_security import WindowsSecurityError, lookup_account_sid
    assert lookup_account_sid("NT SERVICE\\TrustedInstaller") == TRUSTED_INSTALLER == service_sid("TrustedInstaller")
    with pytest.raises(WindowsSecurityError):  # an unregistered service has no account yet
        lookup_account_sid("NT SERVICE\\NatureWorkerNoSuchService")


@windows_only
def test_native_least_privilege_acl_with_a_service_sid(tmp_path):
    """The installer's icacls grants, applied for real to a registered service account."""
    from nature_worker.config import current_user_sid
    from nature_worker.windows_security import read_acl, system_tool
    folder = tmp_path / "data"
    folder.mkdir()
    user = current_user_sid()
    subprocess.run([system_tool("icacls.exe"), str(folder), "/inheritance:r", "/grant:r", f"*{user}:(OI)(CI)F",
                    f"*{TRUSTED_INSTALLER}:(OI)(CI)M"], check=True, capture_output=True)
    acl = read_acl(folder)
    assert acl.protected and acl.grantees() == {user, TRUSTED_INSTALLER}
    assert not acl.grantees() & {USERS, AUTH_USERS, EVERYONE}
    unregistered = service_sid("NatureWorkerNoSuchService")
    result = subprocess.run([system_tool("icacls.exe"), str(folder), "/grant", f"*{unregistered}:(OI)(CI)M"],
                            capture_output=True, text=True)
    assert result.returncode == 1332  # the native failure this ordering prevents


# -- exact installed build (native e2e: "did not answer as 0.1.0+g81f1b9d82f62") ----------

def test_install_requires_the_exact_build_not_the_base_version(win, tmp_path):
    setup, fake, standalone = win
    # What the 81f1b9d service answered: its base version, no build field.
    fake.status_override = lambda st: {k: v for k, v in st.items() if k != "build"}
    with pytest.raises(SetupError) as info:
        install(setup, standalone, tmp_path)
    message = str(info.value)
    assert "reports version '0.2.0' and no build, not 0.2.0+gaaaaaaaaaaaa" in message
    assert "last answer: HTTP 200" in message and '"version": "0.2.0"' in message and "service state: running" in message


def test_a_different_build_is_never_accepted(win, tmp_path):
    setup, fake, standalone = win
    fake.status_override = lambda st: {**st, "build": "0.2.0+gbbbbbbbbbbbb"}
    with pytest.raises(SetupError, match="runs build '0.2.0\\+gbbbbbbbbbbbb', not 0.2.0\\+gaaaaaaaaaaaa"):
        install(setup, standalone, tmp_path)


def test_no_answer_is_reported_with_the_connection_error(win, tmp_path):
    setup, fake, standalone = win
    fake.start_fails_for.add("0.2.0+gaaaaaaaaaaaa")
    with pytest.raises(SetupError, match="last answer: ConnectionRefusedError"):
        install(setup, standalone, tmp_path)


def test_build_identity_rules():
    from nature_worker.build_identity import build_mismatch
    assert build_mismatch({"version": "0.1.0", "build": "0.1.0+gabc"}, "0.1.0+gabc") is None
    assert build_mismatch({"version": "0.1.0", "build": "0.1.0+gabc"}, "0.1.0") is not None
    assert build_mismatch({"version": "9.1.0", "build": None}, "9.1.0") is not None  # unmanaged: never a match
    assert build_mismatch({"version": "9.1.0"}, "9.1.0") is None  # a signed release from before the field
    assert build_mismatch({"version": "0.1.0"}, "0.1.0+gabc") is not None


def test_the_running_build_is_the_versions_directory_the_code_was_loaded_from(tmp_path):
    """Real interpreter, real versioned layout (native on Windows: Lib\\site-packages)."""
    import shutil
    import nature_worker
    prefix = tmp_path / "PC"
    site = prefix / "versions" / "0.1.0+g81f1b9d82f62" / "venv" / ("Lib" if os.name == "nt" else "lib") / "site-packages"
    shutil.copytree(Path(nature_worker.__file__).parent, site / "nature_worker",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (prefix / "installation.json").write_text("{}")
    code = ("import site, sys; site.addsitedir(sys.argv[1]); import nature_worker.build_identity as b; "
            "print(b.__file__.startswith(sys.argv[1]), b.running_build())")
    # -S: like the service runtime, whose own site-packages is empty, only the layout's copy is importable.
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(site)], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["True", "0.1.0+g81f1b9d82f62"]
    (prefix / "installation.json").unlink()  # not a managed installation: no build claimed
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(site)], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["True", "None"]


def test_status_reports_the_installed_build(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from nature_worker.platform_client import server
    monkeypatch.setattr(server, "running_build", lambda: "0.1.0+g81f1b9d82f62")
    settings = server.ClientSettings(tmp_path / "pc", "https://platform.example", tmp_path / "sa")
    with TestClient(server.create_app(settings, verify_startup=False)) as client:
        status = client.get("/api/status").json()
    assert status["build"] == "0.1.0+g81f1b9d82f62" and status["version"] == nature_worker_version()


def nature_worker_version():
    import nature_worker
    return nature_worker.__version__


def test_repair_removes_an_added_users_entry_and_revalidates(win, tmp_path):
    """Native e2e (f119259): repair refused because icacls /grant:r kept the added Users entry."""
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    fake.acls[setup.data] = private_acl(setup.sid, Ace(USERS, READ, flags=3))
    setup.repair()
    acl = fake.read_acl(setup.data)
    assert private_problems(acl, setup.sid) == [] and USERS not in acl.grantees()


def test_reinstall_over_retained_data_with_a_damaged_acl_restores_it(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    setup.uninstall(delete_local_data=False)
    fake.acls[setup.data] = private_acl(setup.sid, Ace(USERS, READ, flags=3))
    install(setup, standalone, tmp_path, archive=source_archive(tmp_path, "d" * 40))
    assert USERS not in fake.read_acl(setup.data).grantees()


def test_protected_dacl_sddl_is_exact():
    from nature_worker.windows_security import WindowsSecurityError, protected_dacl_sddl
    assert protected_dacl_sddl(ADMINISTRATORS, [(SYSTEM, 0x1F01FF), (SVC, 0x1301BF)]) == \
        f"O:{ADMINISTRATORS}D:PAI(A;OICI;0x1f01ff;;;{SYSTEM})(A;OICI;0x1301bf;;;{SVC})"
    with pytest.raises(WindowsSecurityError):
        protected_dacl_sddl("S-1-5-32-544)(A;;FA;;;WD", [])


@windows_only
def test_native_dacl_replacement_removes_foreign_entries(tmp_path):
    from nature_worker.config import current_user_sid
    from nature_worker.windows_security import read_acl, set_protected_dacl, system_tool
    folder = tmp_path / "data"
    folder.mkdir()
    subprocess.run([system_tool("icacls.exe"), str(folder), "/grant", f"*{USERS}:(OI)(CI)R"], check=True,
                   capture_output=True)
    user = current_user_sid()
    set_protected_dacl(folder, user, [(user, 0x1F01FF), (TRUSTED_INSTALLER, 0x1301BF)])
    acl = read_acl(folder)
    assert acl.protected and acl.grantees() == {user, TRUSTED_INSTALLER}
" in seen["script"]
    assert "nature_worker\\\\.platform_client|platform-client-service\\\\.py" in seen["script"]


def test_a_running_platform_client_process_is_residue(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    fake.platform_client_processes = lambda: ["4242 python.exe -I platform-client-service.py"]
    report = setup.uninstall(delete_local_data=True)
    assert report["checks"]["running_processes"] is False and report["processes"]


# -- service identity before ACLs (native e2e: icacls exit 1332) -------------------------

def test_the_service_is_registered_and_its_sid_verified_before_any_acl_names_it(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    create = next(i for i, c in enumerate(fake.calls) if c[:2] == ["sc.exe", "create"])
    sidtype = next(i for i, c in enumerate(fake.calls) if c[:2] == ["sc.exe", "sidtype"])
    grant = next(i for i, c in enumerate(fake.calls) if c[0] == "set_protected_dacl" and f"{setup.sid}:0x1301bf" in c)
    assert create < sidtype < grant


def test_granting_a_service_sid_before_the_service_exists_fails_like_windows(win):
    setup, fake, _ = win
    with pytest.raises(SetupError):
        setup.verify_service_identity()  # nothing may name the SID before the service is registered


def test_a_sid_windows_maps_differently_is_never_granted(win, tmp_path):
    setup, fake, standalone = win
    fake.resolve_override = "S-1-5-80-1-2-3-4-5"
    with pytest.raises(SetupError, match="not the expected"):
        install(setup, standalone, tmp_path)
    assert not any(c[0] == "set_protected_dacl" and c[1] == str(setup.data) for c in fake.calls)
    assert fake.service is None  # the first installation removed the service it created


def test_a_service_registered_under_another_account_is_refused(win, tmp_path, monkeypatch):
    setup, fake, standalone = win
    original = fake.run

    def run(cmd, **kwargs):
        result = original(cmd, **kwargs)
        if fake.calls[-1][:2] == ["sc.exe", "create"]:
            fake.registry[setup.service_key]["ObjectName"] = "LocalSystem"
        return result
    monkeypatch.setattr(fake, "run", run)
    with pytest.raises(SetupError, match="LocalSystem"):
        install(setup, standalone, tmp_path)


def test_repair_restores_a_damaged_service_account_before_checking_it(win, tmp_path):
    setup, fake, standalone = win
    label = install(setup, standalone, tmp_path)
    fake.registry[setup.service_key]["ObjectName"] = "LocalSystem"
    fake.registry[setup.service_key]["ServiceSidType"] = 0
    assert setup.repair() == label
    assert fake.registry[setup.service_key]["ObjectName"] == setup.account
    assert fake.registry[setup.service_key]["ServiceSidType"] == 1


@windows_only
def test_native_service_account_resolution():
    from nature_worker.windows_security import WindowsSecurityError, lookup_account_sid
    assert lookup_account_sid("NT SERVICE\\TrustedInstaller") == TRUSTED_INSTALLER == service_sid("TrustedInstaller")
    with pytest.raises(WindowsSecurityError):  # an unregistered service has no account yet
        lookup_account_sid("NT SERVICE\\NatureWorkerNoSuchService")


@windows_only
def test_native_least_privilege_acl_with_a_service_sid(tmp_path):
    """The installer's icacls grants, applied for real to a registered service account."""
    from nature_worker.config import current_user_sid
    from nature_worker.windows_security import read_acl, system_tool
    folder = tmp_path / "data"
    folder.mkdir()
    user = current_user_sid()
    subprocess.run([system_tool("icacls.exe"), str(folder), "/inheritance:r", "/grant:r", f"*{user}:(OI)(CI)F",
                    f"*{TRUSTED_INSTALLER}:(OI)(CI)M"], check=True, capture_output=True)
    acl = read_acl(folder)
    assert acl.protected and acl.grantees() == {user, TRUSTED_INSTALLER}
    assert not acl.grantees() & {USERS, AUTH_USERS, EVERYONE}
    unregistered = service_sid("NatureWorkerNoSuchService")
    result = subprocess.run([system_tool("icacls.exe"), str(folder), "/grant", f"*{unregistered}:(OI)(CI)M"],
                            capture_output=True, text=True)
    assert result.returncode == 1332  # the native failure this ordering prevents


# -- exact installed build (native e2e: "did not answer as 0.1.0+g81f1b9d82f62") ----------

def test_install_requires_the_exact_build_not_the_base_version(win, tmp_path):
    setup, fake, standalone = win
    # What the 81f1b9d service answered: its base version, no build field.
    fake.status_override = lambda st: {k: v for k, v in st.items() if k != "build"}
    with pytest.raises(SetupError) as info:
        install(setup, standalone, tmp_path)
    message = str(info.value)
    assert "reports version '0.2.0' and no build, not 0.2.0+gaaaaaaaaaaaa" in message
    assert "last answer: HTTP 200" in message and '"version": "0.2.0"' in message and "service state: running" in message


def test_a_different_build_is_never_accepted(win, tmp_path):
    setup, fake, standalone = win
    fake.status_override = lambda st: {**st, "build": "0.2.0+gbbbbbbbbbbbb"}
    with pytest.raises(SetupError, match="runs build '0.2.0\\+gbbbbbbbbbbbb', not 0.2.0\\+gaaaaaaaaaaaa"):
        install(setup, standalone, tmp_path)


def test_no_answer_is_reported_with_the_connection_error(win, tmp_path):
    setup, fake, standalone = win
    fake.start_fails_for.add("0.2.0+gaaaaaaaaaaaa")
    with pytest.raises(SetupError, match="last answer: ConnectionRefusedError"):
        install(setup, standalone, tmp_path)


def test_build_identity_rules():
    from nature_worker.build_identity import build_mismatch
    assert build_mismatch({"version": "0.1.0", "build": "0.1.0+gabc"}, "0.1.0+gabc") is None
    assert build_mismatch({"version": "0.1.0", "build": "0.1.0+gabc"}, "0.1.0") is not None
    assert build_mismatch({"version": "9.1.0", "build": None}, "9.1.0") is not None  # unmanaged: never a match
    assert build_mismatch({"version": "9.1.0"}, "9.1.0") is None  # a signed release from before the field
    assert build_mismatch({"version": "0.1.0"}, "0.1.0+gabc") is not None


def test_the_running_build_is_the_versions_directory_the_code_was_loaded_from(tmp_path):
    """Real interpreter, real versioned layout (native on Windows: Lib\\site-packages)."""
    import shutil
    import nature_worker
    prefix = tmp_path / "PC"
    site = prefix / "versions" / "0.1.0+g81f1b9d82f62" / "venv" / ("Lib" if os.name == "nt" else "lib") / "site-packages"
    shutil.copytree(Path(nature_worker.__file__).parent, site / "nature_worker",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (prefix / "installation.json").write_text("{}")
    code = ("import site, sys; site.addsitedir(sys.argv[1]); import nature_worker.build_identity as b; "
            "print(b.__file__.startswith(sys.argv[1]), b.running_build())")
    # -S: like the service runtime, whose own site-packages is empty, only the layout's copy is importable.
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(site)], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["True", "0.1.0+g81f1b9d82f62"]
    (prefix / "installation.json").unlink()  # not a managed installation: no build claimed
    out = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(site)], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["True", "None"]


def test_status_reports_the_installed_build(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from nature_worker.platform_client import server
    monkeypatch.setattr(server, "running_build", lambda: "0.1.0+g81f1b9d82f62")
    settings = server.ClientSettings(tmp_path / "pc", "https://platform.example", tmp_path / "sa")
    with TestClient(server.create_app(settings, verify_startup=False)) as client:
        status = client.get("/api/status").json()
    assert status["build"] == "0.1.0+g81f1b9d82f62" and status["version"] == nature_worker_version()


def nature_worker_version():
    import nature_worker
    return nature_worker.__version__


def test_repair_removes_an_added_users_entry_and_revalidates(win, tmp_path):
    """Native e2e (f119259): repair refused because icacls /grant:r kept the added Users entry."""
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    fake.acls[setup.data] = private_acl(setup.sid, Ace(USERS, READ, flags=3))
    setup.repair()
    acl = fake.read_acl(setup.data)
    assert private_problems(acl, setup.sid) == [] and USERS not in acl.grantees()


def test_reinstall_over_retained_data_with_a_damaged_acl_restores_it(win, tmp_path):
    setup, fake, standalone = win
    install(setup, standalone, tmp_path)
    setup.uninstall(delete_local_data=False)
    fake.acls[setup.data] = private_acl(setup.sid, Ace(USERS, READ, flags=3))
    install(setup, standalone, tmp_path, archive=source_archive(tmp_path, "d" * 40))
    assert USERS not in fake.read_acl(setup.data).grantees()


def test_protected_dacl_sddl_is_exact():
    from nature_worker.windows_security import WindowsSecurityError, protected_dacl_sddl
    assert protected_dacl_sddl(ADMINISTRATORS, [(SYSTEM, 0x1F01FF), (SVC, 0x1301BF)]) == \
        f"O:{ADMINISTRATORS}D:PAI(A;OICI;0x1f01ff;;;{SYSTEM})(A;OICI;0x1301bf;;;{SVC})"
    with pytest.raises(WindowsSecurityError):
        protected_dacl_sddl("S-1-5-32-544)(A;;FA;;;WD", [])


@windows_only
def test_native_dacl_replacement_removes_foreign_entries(tmp_path):
    from nature_worker.config import current_user_sid
    from nature_worker.windows_security import read_acl, set_protected_dacl, system_tool
    folder = tmp_path / "data"
    folder.mkdir()
    subprocess.run([system_tool("icacls.exe"), str(folder), "/grant", f"*{USERS}:(OI)(CI)R"], check=True,
                   capture_output=True)
    user = current_user_sid()
    set_protected_dacl(folder, user, [(user, 0x1F01FF), (TRUSTED_INSTALLER, 0x1301BF)])
    acl = read_acl(folder)
    assert acl.protected and acl.grantees() == {user, TRUSTED_INSTALLER}
