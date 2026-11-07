# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows Standalone installer: the pinned runtime in signed bundles, its safe unpacking, the
uninstall check, explicit data removal, S-C isolation, pinned wheels, licenses and the Inno
Setup script. The installer itself runs on real Windows (packaging/windows, acceptance kit)."""
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nature_worker.maintenance import RUNTIME_INFO, RUNTIME_LOCK, RuntimeLock
from nature_worker.upgrade import __main__ as cli
from nature_worker.upgrade.layout import Installation, Layout
from nature_worker.upgrade.release import Manifest, ReleaseError, sign_manifest, stage_bundle, verify_bundle
from nature_worker.upgrade.toolkit import StandaloneToolkit, ToolkitError, unpack_runtime
from upgrade_fixtures import make_bundle, trusted

REPO = Path(__file__).resolve().parents[1]
WIN = REPO / "packaging" / "windows"
KEY = Ed25519PrivateKey.generate()
sys.path.insert(0, str(REPO / "scripts"))


def runtime_tar(path: Path, entries: dict[str, bytes] | None = None, link: str | None = None) -> Path:
    entries = entries if entries is not None else {"python/python.exe": b"MZ", "python/bin/python3": b"#!",
                                                  "python/LICENSE.txt": b"PSF"}
    with tarfile.open(path, "w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo("python/evil")
            info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info)
    return path


def with_runtime(bundle: Path, python: str = "3.12.15", **manifest_changes) -> Path:
    """Add a runtime archive to a test bundle and sign it again."""
    archive = runtime_tar(bundle / "rt.tar.gz")
    (bundle / "runtime").mkdir()
    archive = archive.rename(bundle / "runtime" / "cpython.tar.gz")
    m = json.loads((bundle / "release.json").read_text())
    m["target"] = {**m["target"], "python": python.rsplit(".", 1)[0]}
    data = archive.read_bytes()
    m["runtime"] = {"path": "runtime/cpython.tar.gz", "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data), "python": python, "origin": {"url": "https://example.test/x"}}
    m.update(manifest_changes)
    raw = json.dumps(m).encode()
    (bundle / "release.json").write_bytes(raw)
    (bundle / "release.json.sig").write_bytes(sign_manifest(raw, KEY))
    return bundle


# -- the runtime in a signed bundle ------------------------------------------------------------

def test_a_bundle_with_its_runtime_verifies_and_stages_the_runtime(tmp_path):
    bundle = with_runtime(make_bundle(tmp_path, KEY, "1.2.0"))
    release = verify_bundle(bundle, [trusted(KEY)])
    assert release.manifest.runtime.python == "3.12.15"
    staged = stage_bundle(str(bundle), tmp_path / "staging", [trusted(KEY)])
    assert (staged.bundle / "runtime" / "cpython.tar.gz").read_bytes() == (bundle / "runtime" / "cpython.tar.gz").read_bytes()


def test_a_swapped_runtime_is_refused(tmp_path):
    bundle = with_runtime(make_bundle(tmp_path, KEY, "1.2.0"))
    runtime_tar(bundle / "runtime" / "cpython.tar.gz", {"python/python.exe": b"other"})
    with pytest.raises(ReleaseError, match="runtime/cpython.tar.gz"):
        verify_bundle(bundle, [trusted(KEY)])


@pytest.mark.parametrize("runtime,message", [
    ({"path": "wheels/cpython.tar.gz"}, "one .tar.gz archive under runtime/"),
    ({"path": "runtime/cpython.zip"}, "one .tar.gz archive under runtime/"),
    ({"python": "3.11.9"}, "does not match the release target"),
    ({"python": "3.12"}, "does not match the release target"),
])
def test_runtime_manifest_rules(runtime, message):
    entry = {"path": "runtime/cpython.tar.gz", "sha256": "0" * 64, "size": 1, "python": "3.12.15", **runtime}
    with pytest.raises(ReleaseError, match=message):
        Manifest.parse(_manifest(runtime=entry))


def test_a_runtime_file_without_the_runtime_entry_is_refused():
    with pytest.raises(ReleaseError, match="must be a wheel"):
        Manifest.parse(_manifest(artifacts=[_wheel(), {"path": "runtime/x.tar.gz", "sha256": "0" * 64, "size": 1}]))


def _wheel():
    return {"path": "wheels/nature_ai_worker-1.0.0-py3-none-any.whl", "sha256": "0" * 64, "size": 1}


def _manifest(**changes) -> bytes:
    m = {"schema": 1, "product": "nature-ai-worker", "version": "1.0.0", "released_at": "x",
         "target": {"os": "windows", "arch": "x86_64", "python": "3.12"}, "editions": ["standalone"],
         "upgrade_from": {"min": "0.1.0"}, "data_schema": {"standalone": 1},
         "notes": {"path": "RELEASE_NOTES.md", "sha256": "0" * 64, "size": 1},
         "product_wheel": _wheel()["path"], "artifacts": [_wheel()]}
    m.update(changes)
    return json.dumps(m).encode()


# -- unpacking it -------------------------------------------------------------------------------

def test_the_runtime_is_unpacked_into_the_version(tmp_path):
    python = unpack_runtime(runtime_tar(tmp_path / "rt.tar.gz"), tmp_path / "v" / "runtime")
    assert python.is_file() and (tmp_path / "v" / "runtime" / "LICENSE.txt").read_bytes() == b"PSF"
    assert not (tmp_path / "v" / "runtime.unpacking").exists()


@pytest.mark.parametrize("entries,link", [
    ({"python/../../escape.txt": b"x"}, None),
    ({"other/python.exe": b"x"}, None),
    ({"python/python.exe": b"MZ"}, "/etc/passwd"),
])
def test_an_unexpected_archive_entry_refuses_the_whole_runtime(tmp_path, entries, link):
    with pytest.raises(ToolkitError, match="unexpected entry"):
        unpack_runtime(runtime_tar(tmp_path / "rt.tar.gz", entries, link), tmp_path / "v" / "runtime")
    assert not (tmp_path / "v" / "runtime").exists() and not (tmp_path / "escape.txt").exists()


class Answer:
    def __init__(self, version):
        self.version, self.calls = version, []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, self.version + "\n", "")


def _toolkit(tmp_path, version):
    layout = Layout(tmp_path / "prefix")
    return StandaloneToolkit(layout, Installation("standalone", tmp_path / "data", tmp_path / "ws"),
                             runner=Answer(version))


def test_the_version_runtime_is_the_bundled_one_and_must_report_its_pinned_version(tmp_path):
    release = verify_bundle(with_runtime(make_bundle(tmp_path, KEY, "1.2.0")), [trusted(KEY)])
    toolkit = _toolkit(tmp_path, "3.12.15")
    python = toolkit.version_runtime(release, tmp_path / "prefix" / "versions" / "1.2.0")
    assert python.parent.parent.name == "runtime" or python.parent.name == "runtime"
    assert toolkit.runner.calls[0][0] == str(python)
    with pytest.raises(ToolkitError, match="not 3.12.15"):
        _toolkit(tmp_path, "3.12.14").version_runtime(release, tmp_path / "prefix" / "versions" / "1.2.1")


def test_without_a_bundled_runtime_the_upgraders_python_is_used(tmp_path):
    release = verify_bundle(make_bundle(tmp_path, KEY, "1.2.0"), [trusted(KEY)])
    toolkit = _toolkit(tmp_path, "unused")
    assert toolkit.version_runtime(release, tmp_path / "v") == toolkit.base_python


# -- uninstall check, explicit data removal, S-C isolation --------------------------------------

def _installed(tmp_path, edition="standalone"):
    layout = Layout(tmp_path / "prefix")
    layout.prefix.mkdir()
    data, ws = tmp_path / "data", tmp_path / "ws"
    data.mkdir()
    ws.mkdir()
    (data / "worker.db").write_text("db")
    (ws / "my-report.md").write_text("mine")
    layout.write_installation(Installation(edition, data, ws))
    return layout, data, ws


def test_uninstall_check_passes_only_when_nothing_runs(tmp_path, capsys):
    layout, data, _ = _installed(tmp_path)
    assert cli.main(["uninstall-check", "--prefix", str(layout.prefix)]) == 0
    assert "Kept: data" in capsys.readouterr().out
    lock = RuntimeLock(data / RUNTIME_LOCK)
    assert lock.acquire()
    try:
        assert cli.main(["uninstall-check", "--prefix", str(layout.prefix)]) == 2
        assert "is running" in capsys.readouterr().out
    finally:
        lock.release()
    layout.save_state({"id": "x", "phase": "migrating"})
    assert cli.main(["uninstall-check", "--prefix", str(layout.prefix)]) == 2
    assert "did not finish" in capsys.readouterr().out


def test_uninstall_check_is_for_standalone_only(tmp_path, capsys):
    layout, *_ = _installed(tmp_path, "platform_client")
    assert cli.main(["uninstall-check", "--prefix", str(layout.prefix)]) == 3
    assert "Standalone installations only" in capsys.readouterr().out


def test_remove_data_needs_the_exact_folder_and_keeps_the_workspace(tmp_path, capsys):
    layout, data, ws = _installed(tmp_path)
    assert cli.main(["remove-data", "--prefix", str(layout.prefix), "--confirm-path", str(tmp_path)]) == 2
    assert (data / "worker.db").exists()
    assert cli.main(["remove-data", "--prefix", str(layout.prefix), "--confirm-path", str(data)]) == 0
    assert not data.exists() and (ws / "my-report.md").read_text() == "mine"
    assert not list(tmp_path.glob("data.removing-*"))
    assert "Kept: your workspace" in capsys.readouterr().out


def test_remove_data_with_the_workspace_and_without_a_terminal(tmp_path, monkeypatch, capsys):
    layout, data, ws = _installed(tmp_path)
    monkeypatch.setattr(cli, "_ask", lambda q: None)          # nobody to ask: not confirmed
    assert cli.main(["remove-data", "--prefix", str(layout.prefix), "--include-workspace"]) == 2
    assert data.exists() and ws.exists()
    monkeypatch.setattr(cli, "_ask", lambda q: str(data))
    assert cli.main(["remove-data", "--prefix", str(layout.prefix), "--include-workspace"]) == 0
    assert not data.exists() and not ws.exists()


def test_remove_data_refuses_while_running_and_platform_client_data(tmp_path, capsys):
    layout, data, _ = _installed(tmp_path)
    lock = RuntimeLock(data / RUNTIME_LOCK)
    assert lock.acquire()
    try:
        assert cli.main(["remove-data", "--prefix", str(layout.prefix), "--confirm-path", str(data)]) == 2
    finally:
        lock.release()
    assert (data / "worker.db").exists()
    (data / "installation-mode.json").write_text('{"schema": 1, "mode": "platform_client"}\n')
    assert cli.main(["remove-data", "--prefix", str(layout.prefix), "--confirm-path", str(data)]) == 3
    assert (data / "worker.db").exists()


def _windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))


def test_standalone_may_coexist_but_never_lives_in_platform_client_folders(tmp_path, monkeypatch, capsys):
    _windows(monkeypatch, tmp_path)
    # A separate Platform Client installation exists: that alone does not block Standalone.
    assert cli.platform_client_namespace(tmp_path / "user" / "data") is None
    assert cli.platform_client_namespace(tmp_path / "pd" / "NatureAIWorkerX" / "data") is None
    inside = tmp_path / "pd" / "NatureAIWorker" / "PlatformClient" / "data"
    assert cli.platform_client_namespace(inside) == tmp_path / "pd" / "NatureAIWorker"
    assert cli.platform_client_namespace(tmp_path / "pf" / "NatureAIWorker") == tmp_path / "pf" / "NatureAIWorker"


@pytest.mark.parametrize("where", ["--data-dir", "--workspace", "--prefix"])
def test_install_refuses_platform_client_folders_without_touching_them(tmp_path, monkeypatch, capsys, where):
    _windows(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")       # the bundle is built for this host...
    bundle = make_bundle(tmp_path, KEY, "1.2.0")
    monkeypatch.setattr(sys, "platform", "win32")       # ...and installed "on Windows"
    pc = tmp_path / "pd" / "NatureAIWorker" / "PlatformClient"
    args = {"--prefix": tmp_path / "p", "--data-dir": tmp_path / "d", "--workspace": tmp_path / "w"}
    args[where] = pc / "x"
    opened = []
    monkeypatch.setattr(cli, "read_mode", lambda d: opened.append(d))
    code = cli.main(["install", "--bundle", str(bundle), "--confirm-version", "1.2.0",
                     *[str(x) for kv in args.items() for x in kv]])
    assert code == 3 and "inside the Platform Client's folders" in capsys.readouterr().out
    assert not opened and not (tmp_path / "pd").exists() and not (tmp_path / "p").exists()


class FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_SET_VALUE, REG_SZ, REG_DWORD = 1, 2, 4, 1, 4

    def __init__(self, entries):
        self.entries = entries

    def OpenKey(self, root, key, reserved=0, access=0):
        if key not in self.entries:
            raise OSError(2, "not found")
        return FakeKey(self.entries[key])

    def QueryValueEx(self, key, name):
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.values[name] = value


def test_apps_and_features_follows_every_version_switch(tmp_path, monkeypatch):
    from nature_worker.upgrade import layout as layout_mod
    layout = Layout(tmp_path / "app")
    for v in ("0.1.0", "0.1.1+acceptance"):
        d = layout.version_dir(v)
        d.mkdir(parents=True)
        (d / "INSTALLED.json").write_text("{}")
    entry = {"InstallLocation": str(tmp_path / "app") + "\\", "DisplayVersion": "0.1.1+acceptance"}
    fake = FakeWinreg({layout_mod.WINDOWS_UNINSTALL_KEY: entry})
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(layout_mod.os.path, "normcase", lambda p: p.rstrip("\\").rstrip("/"))
    layout.set_current("0.1.0")                  # e.g. a user rollback
    assert entry["DisplayVersion"] == "0.1.0" and (entry["VersionMajor"], entry["VersionMinor"]) == (0, 1)
    layout.set_current("0.1.1+acceptance")       # an upgrade
    assert entry["DisplayVersion"] == "0.1.1+acceptance"
    # Another installation's entry (a different folder) is never changed.
    entry["InstallLocation"] = str(tmp_path / "elsewhere")
    layout.set_current("0.1.0")
    assert entry["DisplayVersion"] == "0.1.1+acceptance"
    # No entry (installed with nature-worker-upgrade, not the installer): the switch still works.
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg({}))
    layout.set_current("0.1.1+acceptance")
    assert layout.current() == "0.1.1+acceptance"


def test_the_installer_app_id_is_the_same_everywhere():
    from nature_worker.upgrade.layout import WINDOWS_APP_ID
    iss = (WIN / "NatureAIWorker.iss").read_text()
    assert f"AppId={{{WINDOWS_APP_ID}" in iss
    assert f'APP_ID = "{WINDOWS_APP_ID}"' in (REPO / "scripts" / "windows_standalone_acceptance.py").read_text()


def test_setup_never_looks_at_platform_client_folders():
    iss = (WIN / "NatureAIWorker.iss").read_text()
    assert "PlatformClient" not in iss.replace("Platform Client", "") and "{commonappdata}" not in iss
    driver = (REPO / "scripts" / "windows_standalone_acceptance.py").read_text()
    snapshot = driver[driver.index("def snapshot"):driver.index("# -- phases")]
    assert "read_bytes" not in snapshot and "read_text" not in snapshot


def test_the_build_backend_is_pinned_by_hash():
    lines = [ln for ln in (REPO / "packaging" / "build-requirements.txt").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines and all(re.search(r"==[\d.]+ --hash=sha256:[0-9a-f]{64}$", ln) for ln in lines)
    source = (REPO / "scripts" / "release_bundle.py").read_text()
    assert "--no-build-isolation" in source and "--require-hashes" in source


# -- desktop launcher -------------------------------------------------------------------------

def test_the_start_menu_entry_opens_a_running_worker(tmp_path, monkeypatch):
    from nature_worker import desktop
    (tmp_path / RUNTIME_INFO).write_text(json.dumps({"port": 8123}))
    opened = []
    monkeypatch.setenv("NATURE_WORKER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "worker_running", lambda d: d == tmp_path)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    desktop.main()
    assert opened == ["http://127.0.0.1:8123/"]


def test_the_start_menu_entry_uses_a_free_port_when_8080_is_taken(tmp_path, monkeypatch):
    from nature_worker import desktop
    import nature_worker.__main__ as server
    monkeypatch.setenv("NATURE_WORKER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NATURE_WORKER_PORT", raising=False)
    monkeypatch.setattr(desktop, "_free", lambda port: False)
    monkeypatch.setattr(desktop, "_any_free_port", lambda: 8765)
    monkeypatch.setattr(desktop.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    started = []
    monkeypatch.setattr(server, "main", lambda: started.append(list(sys.argv)))
    desktop.main()
    assert started and started[0][1:] == ["--port", "8765"]


# -- build: pinned wheels, licenses, the Inno Setup script ----------------------------------------

def test_the_pinned_windows_wheels_are_exactly_the_locked_versions():
    import release_bundle
    pins = release_bundle.read_wheel_hashes(WIN / "wheels-win_amd64-cp312.sha256")
    locked = {ln.split("==")[0].lower().replace("-", "_"): ln.split("==")[1].strip()
              for ln in (REPO / "requirements.lock").read_text().splitlines() if "==" in ln and not ln.startswith("#")}
    pinned = {n.split("-")[0].lower(): n.split("-")[1] for n in pins}
    assert pinned == locked
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in pins.values())
    assert all(n.endswith(("-win_amd64.whl", "-none-any.whl", "-py2.py3-none-any.whl")) for n in pins)


def test_pinned_downloads_must_match_the_list_exactly(tmp_path, monkeypatch):
    import release_bundle
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    product = wheels / "nature_ai_worker-1.0.0-py3-none-any.whl"
    product.write_bytes(b"product")

    def fake_run(cmd, **kw):
        name = cmd[-1].replace("==", "-") + "-py3-none-any.whl"
        (wheels / name).write_bytes(b"tampered" if "bad" in name else b"good")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(release_bundle, "_run", fake_run)
    target = {"os": "windows", "arch": "x86_64", "python": "3.12"}
    good = hashlib.sha256(b"good").hexdigest()
    release_bundle._download_pinned({"ok-1.0-py3-none-any.whl": good}, target, wheels, product)
    (wheels / "ok-1.0-py3-none-any.whl").unlink()
    with pytest.raises(SystemExit, match="does not match its pinned SHA-256"):
        release_bundle._download_pinned({"bad-1.0-py3-none-any.whl": good}, target, wheels, product)


def test_runtime_and_installer_tool_pins_are_complete():
    for name in ("runtime.json", "innosetup.json"):
        pin = json.loads((WIN / name).read_text())
        assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) and pin["url"].startswith("https://github.com/")
    runtime = json.loads((WIN / "runtime.json").read_text())
    assert runtime["python"].startswith("3.12.") and "install_only" in runtime["file"]


def _wheel_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_license_collection_covers_runtime_and_every_wheel(tmp_path):
    import windows_installer
    bundle = tmp_path / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "wheels" / "dep-1.0-py3-none-any.whl").write_bytes(_wheel_bytes({
        "dep-1.0.dist-info/METADATA": "Name: dep\n", "dep-1.0.dist-info/licenses/LICENSE": "DEP LICENSE TEXT"}))
    (bundle / "wheels" / "bare-1.0-py3-none-any.whl").write_bytes(_wheel_bytes({
        "bare-1.0.dist-info/METADATA": "Name: bare\nLicense-Expression: MIT\n"}))
    (bundle / "wheels" / "nature_ai_worker-1.0.0-py3-none-any.whl").write_bytes(_wheel_bytes({}))
    runtime = runtime_tar(tmp_path / "rt.tar.gz", {"python/LICENSE.txt": b"CPYTHON WINDOWS NOTICE"})
    text = windows_installer.license_texts(bundle, runtime)
    assert "CPYTHON WINDOWS NOTICE" in text and "DEP LICENSE TEXT" in text and "License-Expression: MIT" in text
    for component in ("openssl-3", "sqlite", "libffi", "zlib", "liblzma", "expat", "mpdecimal", "bzip2", "tcl"):
        assert f"LICENSE.{component}.txt" in text
    (bundle / "wheels" / "unknown-1.0-py3-none-any.whl").write_bytes(_wheel_bytes({
        "unknown-1.0.dist-info/METADATA": "Name: unknown\n"}))
    with pytest.raises(SystemExit, match="no license"):
        windows_installer.license_texts(bundle, runtime)


def test_the_inno_script_is_per_user_keeps_data_and_gates_upgrades():
    iss = (WIN / "NatureAIWorker.iss").read_text()
    assert "PrivilegesRequired=lowest" in iss and "DefaultDirName={autopf}\\NatureAIWorker" in iss
    assert "{param:CONFIRMUPGRADE|}" in iss and "already installed" in iss and "is not empty" in iss
    assert "uninstall-check" in iss
    deleted = re.findall(r'^Type: \w+; Name: "([^"]+)"', iss, re.M)
    assert all(d.startswith("{app}") for d in deleted) and not any("data" in d.lower() for d in deleted)
    # Every value the script uses is produced by the stage step.
    import windows_installer
    source = Path(windows_installer.__file__).read_text()
    used = set(re.findall(r"\{#(\w+)\}", iss)) | set(re.findall(r"#if (\w+)", iss))
    assert used - {"AppName"} <= set(re.findall(r'"(\w+)": ', source)) | {"TrustKey"}


def test_the_bootstrap_installs_only_the_manifests_wheels_by_hash(tmp_path, monkeypatch):
    sys.path.insert(0, str(WIN))
    import bootstrap
    bundle = make_bundle(tmp_path, KEY, "1.2.0")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["req"] = Path(cmd[cmd.index("-r") + 1]).read_text()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    site = bootstrap.upgrader_site(bundle, tmp_path)
    assert site == tmp_path / "site"
    assert all(flag in seen["cmd"] for flag in ("--no-index", "--require-hashes", "--only-binary=:all:"))
    m = json.loads((bundle / "release.json").read_text())
    for a in m["artifacts"]:
        assert f"--hash=sha256:{a['sha256']}" in seen["req"]


# -- compile-time and kit regressions (native run: "Unknown preprocessor directive", line 289) ------

ISPP_DIRECTIVES = {"include", "define", "undef", "if", "ifdef", "ifndef", "ifexist", "ifnexist", "elif",
                   "else", "endif", "emit", "expr", "insert", "append", "error", "pragma", "sub", "endsub",
                   "for", "file", "dim", "redim"}


def ispp_problems(text: str) -> list[str]:
    """Inno Setup's preprocessor reads every line whose first non-blank character is '#' as a
    directive, also inside [Code]: a Pascal continuation line starting with #13#10 fails to compile."""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            word = re.match(r"#\s*(\w*)", stripped)[1]
            if word not in ISPP_DIRECTIVES:
                problems.append(f"line {number}: {stripped[:60]}")
    return problems


def test_no_line_of_the_installer_script_is_misread_as_a_preprocessor_directive():
    assert ispp_problems((WIN / "NatureAIWorker.iss").read_text()) == []
    # The check itself recognises the line that broke the native build.
    broken = "      Result := 'x' + LogTail(LogFile) +\n        #13#10 + 'Full log: ' + LogFile\n#if TrustKey\n#endif\n"
    assert ispp_problems(broken) == ["line 2: #13#10 + 'Full log: ' + LogFile"]


def test_the_kit_upgrade_chain_is_eligible_so_the_broken_release_tests_the_rollback(tmp_path):
    # Native run nwsa-win2: C was built without B's migration, so upgrading B -> C was refused
    # ("release supports data schema 1, but the data is at 2") instead of failing to start.
    import windows_installer as w
    files = ("src/nature_worker/__init__.py", "src/nature_worker/__main__.py",
             "src/nature_worker/migrations.py", "pyproject.toml")
    built = {}
    for label, extra in w.KIT_VARIANTS:
        src = tmp_path / label
        for name in files:
            (src / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, src / name)
        if "--test-version" in extra:
            w.make_variant(src, extra[extra.index("--test-version") + 1], "--test-migration" in extra,
                           "--test-broken" in extra)
        schema = max(int(n) for n in re.findall(r'^    \((\d+), "', (src / files[2]).read_text(), re.M))
        built[label] = (schema, "broken on purpose" in (src / files[1]).read_text())
    assert [label for label, _ in w.KIT_VARIANTS] == ["A", "B", "C"]
    assert w.KIT_VARIANTS[0][1] == ["--test-version", "0.1.0"], "A must stay 0.1.0 when source advances"
    assert built["A"] == (1, False) and built["B"] == (2, False)
    schema_c, broken_c = built["C"]
    assert schema_c >= built["B"][0] and broken_c   # eligible after B, then fails to start


def test_generated_defines_are_directives_only(tmp_path):
    import windows_installer
    source = Path(windows_installer.__file__).read_text()
    block = source[source.index('(out / "defines.iss").write_text('):source.index('shutil.copyfile(PACKAGING / "NatureAIWorker.iss"')]
    assert '#define {k} "{val}"' in block and "#define TrustKey" in block
    sample = '#define AppVersion "0.1.1+acceptance"\n#define TrustKey 1\n'
    assert ispp_problems(sample) == []


def test_the_runner_reports_missing_installers_without_a_traceback(tmp_path, capsys):
    import windows_standalone_acceptance as driver
    kit = tmp_path / "kit"
    kit.mkdir()
    assert driver.main(["auto", "--kit", str(kit)]) == 2
    err = capsys.readouterr().err
    assert "NOT EXECUTED" in err and "1-build-installers.cmd" in err and "Traceback" not in err
    [report] = (kit / "results").glob("acceptance-auto-*-NOT-EXECUTED.md")
    assert "were not built" in report.read_text()
    # A partial build (one installer of three) is refused the same way, naming what is missing.
    (kit / "installers").mkdir()
    (kit / "installers" / "NatureAIWorker-Setup-0.1.0.exe").write_bytes(b"MZ")
    (kit / "installers" / "SHA256SUMS.txt").write_text("ab" * 32 + "  NatureAIWorker-Setup-0.1.0.exe\n")
    assert driver.main(["finish", "--kit", str(kit)]) == 2
    assert "0.1.1-acceptance, 0.1.2-broken" in capsys.readouterr().err


def test_kit_steps_only_announce_the_next_step_after_success():
    import windows_installer as w
    for template, success in ((w.KIT_ACCEPT, "Next: 3-UI-CHECKLIST.txt"), (w.KIT_FINISH, "Finished."),
                              (w.KIT_BUILD, "Done. Next: 2-acceptance.cmd")):
        gate = template.rindex("exit /b 1")
        assert template.index(success) > gate, success
        assert "if errorlevel 1 (" in template
    assert "rmdir /s /q installers" in w.KIT_BUILD          # never a stale SHA256SUMS.txt from an earlier build


def test_kit_refresh_refuses_other_folders(tmp_path):
    import windows_installer as w
    other = tmp_path / "Documents"
    other.mkdir()
    (other / "keep.txt").write_text("mine")
    base = ["kit", "--private-key", "k", "--trust-key", "t", "--ollama", "http://x", "--out", str(other)]
    with pytest.raises(SystemExit, match="already exists"):
        w.main(base)
    with pytest.raises(SystemExit, match="not an acceptance kit"):
        w.main(base + ["--refresh"])
    assert (other / "keep.txt").read_text() == "mine"


def test_windows_rc_allows_upgrade_from_010_and_records_release_manifest_floor():
    """Regression: the original 0.1.1 RC incorrectly required 0.1.1 as the minimum."""
    import windows_installer

    stage_source = Path(windows_installer.__file__).read_text(encoding="utf-8")
    rc_script = (REPO / "scripts" / "windows_rc_stage.ps1").read_text(encoding="utf-8")
    assert 'p.add_argument("--min-upgrade-from"' in stage_source
    assert 'cmd += ["--min-upgrade-from", args.min_upgrade_from]' in stage_source
    assert '"min_upgrade_from": manifest["upgrade_from"]["min"]' in stage_source
    # The floor is an explicit parameter (default 0.1.0), passed to staging and checked afterwards.
    assert "[string]$MinUpgradeFrom = '0.1.0'" in rc_script
    assert "--min-upgrade-from $MinUpgradeFrom --out $StageDir" in rc_script
    assert "Assert-StagedBuild $StageDir $Version $head $MinUpgradeFrom" in rc_script



def test_windows_acceptance_without_model_seeds_history_and_keeps_settings_unconfigured(monkeypatch):
    """No-model acceptance still exercises real conversation persistence across upgrades."""
    import windows_standalone_acceptance as driver

    run = object.__new__(driver.Run)
    run.expected_configured = False
    run.port = 63147  # isolated unit-test stub; no network request is made
    monkeypatch.setattr(run, "start_worker", lambda: "0.1.0")

    class Html:
        def read(self):
            return b"<html>Worker</html>"

    monkeypatch.setattr(driver.urllib.request, "urlopen", lambda *args, **kwargs: Html())
    created = []

    def api(method, path, body=None, timeout=None):
        if (method, path) == ("GET", "/api/settings"):
            return {"configured": False, "providers": ["ollama"]}
        if (method, path) == ("POST", "/api/conversations"):
            created.append("test-id")
            return {"id": "test-id"}
        raise AssertionError((method, path))

    monkeypatch.setattr(run, "api", api)
    monkeypatch.setattr(run, "conversations", lambda: len(created))
    detail = run.launch_and_configure(None, "unused-model")
    assert run.expected_configured is False
    assert created == ["test-id"]
    assert "one empty conversation seeded" in detail



def test_windows_acceptance_residue_accepts_only_empty_programs_parent(tmp_path, monkeypatch):
    """An empty Windows-created parent is harmless; actual residual content must fail."""
    import windows_standalone_acceptance as driver

    monkeypatch.setattr(driver, "LOCAL", tmp_path)
    run = object.__new__(driver.Run)
    run.results = tmp_path / "results"
    run.results.mkdir()
    before = {"uninstall_keys": [], "start_menu": [], "platform_client": {},
              "settings_key": None, "localappdata": []}
    (run.results / "snapshot-before.json").write_text(json.dumps(before))
    monkeypatch.setattr(run, "uninstall", lambda: 0)
    monkeypatch.setattr(driver.time, "sleep", lambda _: None)

    programs = tmp_path / "Programs"
    programs.mkdir()
    after = {**before, "localappdata": ["Programs"]}
    monkeypatch.setattr(run, "snapshot", lambda: after)
    assert "uninstalled" in run.final_uninstall_and_residue()

    (programs / "unexpected-file.txt").write_text("must be rejected")
    with pytest.raises(driver.Fail, match="new folders"):
        run.final_uninstall_and_residue()
