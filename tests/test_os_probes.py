from pathlib import Path

from nature_worker.os_probes import (
    inspect_known_local_paths, inspect_processes_linux, local_os_diagnostics
)


def test_missing_proc_is_unknown(tmp_path):
    finding = inspect_processes_linux(tmp_path / "not-present")
    assert finding.clean is None


def test_detects_visible_platform_process(tmp_path):
    p = tmp_path / "123"
    p.mkdir()
    (p / "cmdline").write_bytes(b"python\x00-m\x00nature_worker.platform_client\x00")
    finding = inspect_processes_linux(tmp_path)
    assert finding.clean is False
    assert "123" in finding.detail


def test_no_visible_process_stays_unknown(tmp_path):
    p = tmp_path / "123"
    p.mkdir()
    (p / "cmdline").write_bytes(b"python\x00-m\x00other_app\x00")
    assert inspect_processes_linux(tmp_path).clean is None


def test_detects_residue_without_deleting(tmp_path):
    location = tmp_path / "company-data"
    location.mkdir()
    result = inspect_known_local_paths((location,))
    assert result.clean is False
    assert location.exists()


def test_missing_paths_never_count_as_full_host_verification(tmp_path):
    assert inspect_known_local_paths((tmp_path / "missing",)).clean is None


def test_host_diagnostics_never_fabricate_vault_or_policy_success(tmp_path):
    found = {f.check: f for f in local_os_diagnostics((tmp_path / "missing",))}
    assert set(found) == {
        "running_processes", "other_app_locations", "os_credentials",
        "installed_packages", "device_policy"
    }
    assert found["os_credentials"].clean is None
    assert found["device_policy"].clean is None
