# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import sys
from nature_worker.windows_storage import inspect_windows_storage_permissions


def test_missing_windows_acl_verification_is_unknown(tmp_path):
    result = inspect_windows_storage_permissions(tmp_path / "unprovisioned")
    assert result.clean is None


def test_windows_storage_never_approves_based_only_on_existing_folder(tmp_path):
    enterprise = tmp_path / "enterprise"
    enterprise.mkdir()
    result = inspect_windows_storage_permissions(enterprise)
    assert result.clean is None


def test_symlink_is_not_approved(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    result = inspect_windows_storage_permissions(link)
    assert result.clean is not True
