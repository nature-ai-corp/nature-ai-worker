# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import pytest
from nature_worker.install_gate import CleanEvidence, standalone_install_decision


def verified():
    return {x: True for x in (
        "os_credentials", "running_processes", "installed_packages",
        "other_app_locations", "device_policy",
    )}


def test_all_checks_clean_with_no_residue(tmp_path):
    result = standalone_install_decision(
        tmp_path / "sc", CleanEvidence(verified()),
        mode_marker=tmp_path / "mode.json",
    )
    assert result.allowed


@pytest.mark.parametrize("missing_or_unclean", [None, False])
def test_unverified_os_probe_blocks(tmp_path, missing_or_unclean):
    checks = verified()
    checks["os_credentials"] = missing_or_unclean
    result = standalone_install_decision(
        tmp_path / "sc", CleanEvidence(checks),
        mode_marker=tmp_path / "mode.json",
    )
    assert not result.allowed


def test_install_marker_blocks(tmp_path):
    marker = tmp_path / "mode.json"
    marker.write_text('{"schema":1,"mode":"platform_client"}')
    result = standalone_install_decision(
        tmp_path / "sc", CleanEvidence(verified()), mode_marker=marker,
    )
    assert not result.allowed
    assert any("marker:" in b for b in result.blockers)


def test_missing_probe_is_rejected():
    with pytest.raises(ValueError):
        CleanEvidence({"os_credentials": True})


def test_wrong_probe_type_is_rejected():
    checks = verified()
    checks["device_policy"] = "yes"
    with pytest.raises(ValueError):
        CleanEvidence(checks)
