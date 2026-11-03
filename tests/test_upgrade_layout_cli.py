# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Installation layout, launchers, the CLI, and the Platform Client toolkit's
OS boundary (with a recording command runner; real systemd is not used)."""
import json
import os
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nature_worker.upgrade import __main__ as cli
from nature_worker.upgrade.layout import Installation, Layout, LayoutError
from nature_worker.upgrade.toolkit import PlatformClientToolkit, StandaloneToolkit, ToolkitError, clean_env
from upgrade_fixtures import install_fake_version, make_bundle, managed_install

POSIX = os.name != "nt"


def test_current_pointer_switches_atomically_and_only_to_complete_versions(tmp_path):
    layout, _ = managed_install(tmp_path, "1.0.0")
    assert layout.current() == "1.0.0"
    (layout.versions / "1.1.0").mkdir()
    with pytest.raises(LayoutError, match="not completely installed"):
        layout.set_current("1.1.0")
    install_fake_version(layout, "1.1.0", 1)
    (layout.version_dir("1.1.0") / ".incomplete").write_text("")
    with pytest.raises(LayoutError):
        layout.set_current("1.1.0")
    (layout.version_dir("1.1.0") / ".incomplete").unlink()
    layout.set_current("1.1.0")
    assert (layout.prefix / "current").read_text() == "1.1.0"
    assert layout.installed_versions() == ["1.0.0", "1.1.0"]
    (layout.prefix / "current").write_text("../../etc")
    with pytest.raises(LayoutError, match="corrupt"):
        layout.current()


@pytest.mark.skipif(not POSIX, reason="POSIX launcher")
def test_posix_launcher_runs_the_current_version(tmp_path):
    layout, inst = managed_install(tmp_path, "1.0.0")
    python = layout.python("1.0.0")
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho \"$NATURE_WORKER_DATA_DIR|$NATURE_WORKER_INSTALL_PREFIX|$*\"\n")
    python.chmod(0o755)
    launcher = layout.write_launchers(inst)[0]
    out = subprocess.run([str(launcher), "--port", "9"], capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"{inst.data_dir}|{layout.prefix}|-I -m nature_worker --port 9"
    (layout.prefix / "current").write_text("1.0.0;rm -rf /")
    bad = subprocess.run([str(launcher)], capture_output=True, text=True)
    assert bad.returncode == 1 and "Invalid active version" in bad.stderr


def test_windows_launcher_text(tmp_path, monkeypatch):
    layout, inst = managed_install(tmp_path, "1.0.0")
    monkeypatch.setattr(sys, "platform", "win32")
    layout.bin.mkdir()
    text = layout._write_cmd("nature-worker", "nature_worker", {"NATURE_WORKER_DATA_DIR": str(inst.data_dir)}).read_text()
    assert 'set /p VERSION=<"%PREFIX%\\current"' in text
    assert '"%PREFIX%\\versions\\%VERSION%\\venv\\Scripts\\python.exe" -I -m nature_worker %*' in text
    assert "findstr" in text
    desktop = layout._write_cmd("nature-worker-desktop", "nature_worker.desktop", {}).read_text()
    assert "title Nature AI Worker (Standalone)" in desktop
    assert 'NatureAIWorker.exe" -I -m nature_worker.desktop' in desktop
    assert "title Nature AI Worker (Standalone)" not in text
    with pytest.raises(LayoutError, match="Unsupported character"):
        layout._write_cmd("x", "m", {"A": "C:\\a&b"})


def test_unmanaged_installation_status(capsys, monkeypatch):
    monkeypatch.delenv("NATURE_WORKER_INSTALL_PREFIX", raising=False)
    assert cli.main(["status"]) == 0
    assert "not a managed installation" in capsys.readouterr().out


def test_cli_upgrade_requires_matching_confirmation(tmp_path, capsys, monkeypatch):
    key = Ed25519PrivateKey.generate()
    layout, inst = managed_install(tmp_path, "1.0.0")
    from nature_worker.upgrade.release import public_key_entry
    layout.local_keys.write_text(json.dumps({"schema": 1, "keys": [public_key_entry(key, "t")]}))
    bundle = make_bundle(tmp_path, key, "1.1.0")
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    monkeypatch.setattr(StandaloneToolkit, "data_schema", lambda self, v: 1)
    monkeypatch.setattr(StandaloneToolkit, "install_version",
                        lambda self, r: pytest.fail("must not install without authorization"))
    args = ["upgrade", "--prefix", str(layout.prefix), "--bundle", str(bundle)]
    assert cli.main(args) == 2
    assert "Re-run with --confirm-version 1.1.0" in capsys.readouterr().out
    assert cli.main(args + ["--confirm-version", "1.2.0"]) == 2
    out = capsys.readouterr().out
    assert "does not match" in out and "locally trusted key - NOT an official" in out
    assert [h["outcome"] for h in layout.history()] == ["declined", "declined"]


def test_cli_check_shows_notes_and_eligibility(tmp_path, capsys, monkeypatch):
    key = Ed25519PrivateKey.generate()
    layout, inst = managed_install(tmp_path, "1.0.0")
    from nature_worker.upgrade.release import public_key_entry
    layout.local_keys.write_text(json.dumps({"schema": 1, "keys": [public_key_entry(key, "t")]}))
    monkeypatch.setattr(StandaloneToolkit, "data_schema", lambda self, v: 1)
    bundle = make_bundle(tmp_path, key, "1.1.0", notes="## Fixed\n- the thing")
    assert cli.main(["check", "--prefix", str(layout.prefix), "--bundle", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "Installed:  1.0.0" in out and "Available:  1.1.0" in out and "- the thing" in out
    available = json.loads((layout.upgrade / "available.json").read_text())
    assert available["eligible"] and available["version"] == "1.1.0"


def test_register_local_build_records_an_unsigned_build(tmp_path, capsys):
    prefix = tmp_path / "opt"
    (prefix / "versions" / "0.1.0+gabc" / "venv").mkdir(parents=True)
    args = ["register-local-build", "--prefix", str(prefix), "--version", "0.1.0+gabc", "--edition",
            "platform_client", "--data-dir", str(tmp_path / "var"), "--account", "nature-pc",
            "--service", "nature-worker-platform-client.service", "--port", "8090", "--commit", "abc"]
    assert cli.main(args) == 0
    layout = Layout(prefix)
    assert layout.current() == "0.1.0+gabc"
    assert layout.installed_record("0.1.0+gabc")["signer_scope"] == "unsigned-local-build"
    inst = layout.installation()
    assert (inst.edition, inst.account, inst.port) == ("platform_client", "nature-pc", 8090)
    if POSIX:
        assert (prefix / "bin" / "nature-worker-platform-client").read_text().count("nature_worker.platform_client")
    other = args.copy()
    other[other.index(str(tmp_path / "var"))] = str(tmp_path / "elsewhere")
    assert cli.main(other) == 3 and "different edition or data root" in capsys.readouterr().out


def test_child_processes_get_no_pip_or_python_overrides(monkeypatch):
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    monkeypatch.setenv("NATURE_WORKER_DATA_DIR", "/elsewhere")
    env = clean_env()
    assert "PIP_INDEX_URL" not in env and "PYTHONPATH" not in env and "NATURE_WORKER_DATA_DIR" not in env
    assert env["PIP_CONFIG_FILE"] == os.devnull


class Recorder:
    def __init__(self):
        self.calls = []

    def run(self, cmd, *, user=None, env=None, timeout=900):
        self.calls.append((user, cmd))
        return subprocess.CompletedProcess(cmd, 0, '{"data_schema": 1}\n', "")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux Platform Client toolkit")
def test_platform_client_toolkit_runs_data_work_as_its_account_and_requires_root(tmp_path):
    layout, _ = managed_install(tmp_path)
    inst = Installation("platform_client", tmp_path / "pc", None, "nature-pc", "nwpc.service", 8090)
    inst.data_dir.mkdir()
    runner = Recorder()
    toolkit = PlatformClientToolkit(layout, inst, runner=runner)
    toolkit.data_schema("1.0.0")
    toolkit.migrate("1.0.0")
    assert [user for user, _ in runner.calls] == ["nature-pc", "nature-pc"]
    assert all("--data-dir" in cmd for _, cmd in runner.calls)
    checks = dict((name, ok) for name, ok, _ in toolkit.check_permissions())
    assert checks["root"] is (os.geteuid() == 0)
    assert checks["code_root_owned"] is False  # a user-owned prefix is never accepted


def test_platform_client_toolkit_refuses_incomplete_records(tmp_path):
    layout, _ = managed_install(tmp_path)
    with pytest.raises(ToolkitError):
        PlatformClientToolkit(layout, Installation("platform_client", tmp_path / "pc"))


@pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="non-root POSIX")
def test_standalone_toolkit_refuses_root_and_foreign_owners(tmp_path):
    layout, inst = managed_install(tmp_path)
    checks = {name: ok for name, ok, _ in StandaloneToolkit(layout, inst).check_permissions()}
    assert checks == {"not_root": True, "owner": True}


def test_prompts_treat_end_of_input_as_no(monkeypatch):
    """Windows' NUL device reports isatty(); input() then hits end of file."""
    class Tty:
        def isatty(self):
            return True

        def readline(self):
            return ""
    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda _q="": (_ for _ in ()).throw(EOFError()))
    assert cli._ask("Type it: ") is None
    assert cli._confirm("Proceed?", assume=False) is False
