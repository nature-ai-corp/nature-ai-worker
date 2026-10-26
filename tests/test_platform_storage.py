# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Read-only clean-state validator tests (no enterprise data deletion)."""
from nature_worker.platform_storage import platform_paths, validate_clean


def test_no_enterprise_root_is_clean(tmp_path):
    root = tmp_path / "enterprise"
    assert validate_clean(root).clean
    assert not root.exists()
    assert len(platform_paths(root)) == 8


def test_empty_enterprise_root_still_blocks(tmp_path):
    root = tmp_path / "enterprise"
    root.mkdir()
    report = validate_clean(root)
    assert not report.clean
    assert any(s.startswith("root:") for s in report.residues)


def test_enterprise_cache_blocks(tmp_path):
    root = tmp_path / "enterprise"
    (root / "cache").mkdir(parents=True)
    report = validate_clean(root)
    assert not report.clean
    assert any(s.startswith("cache:") for s in report.residues)


def test_symlink_root_blocks(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "enterprise"
    root.symlink_to(target, target_is_directory=True)
    assert not validate_clean(root).clean


def test_os_state_and_install_markers_block(tmp_path):
    marker = tmp_path / "mode-marker"
    marker.write_text("platform_client")
    report = validate_clean(
        tmp_path / "no-enterprise-root",
        active_processes=True,
        credential_entries=("platform-login",),
        installation_markers=(marker,),
    )
    assert not report.clean
    assert any("running-platform-client" == s for s in report.residues)
    assert any("os-credential" in s for s in report.residues)
    assert any("marker" in s for s in report.residues)


def test_does_not_delete_enterprise_data(tmp_path):
    root = tmp_path / "enterprise"
    root.mkdir()
    secret = root / "private.txt"
    secret.write_text("must remain until authorized cleanup")
    validate_clean(root)
    assert secret.read_text() == "must remain until authorized cleanup"
