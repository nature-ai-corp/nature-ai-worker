# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle of scripts/platform_client_windows_e2e.py: a report is written for every
outcome (also when the preconditions fail), and cleanup removes only what the run
itself created. Scenarios and the OS are replaced; the real ``run()`` is driven."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "platform_client_windows_e2e.py"
SCENARIOS = ("s_install", "s_repair", "s_signed_upgrade", "s_failed_upgrade_rolls_back", "s_refused_tampered",
             "s_killed_during_verification", "s_user_rollback", "s_uninstall_keeps_data_then_reinstall",
             "s_uninstall_with_data", "s_production_untouched")


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
