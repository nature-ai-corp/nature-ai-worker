# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import json
import pytest

from nature_worker.installation_mode import ModeError
from nature_worker.platform_runtime import check_platform_startup, require_platform_startup


def test_standalone_never_launches_platform_client(tmp_path):
    result = check_platform_startup(tmp_path, sc_runtime_available=True,
                                    os_isolation_verified=True,
                                    credential_backend_verified=True)
    assert not result.allowed
    assert "platform-client-installation-required" in result.blockers


def test_platform_mode_requires_all_verified_dependencies(tmp_path):
    (tmp_path / "installation-mode.json").write_text(
        json.dumps({"schema": 1, "mode": "platform_client"})
    )
    result = check_platform_startup(tmp_path, sc_runtime_available=False,
                                    os_isolation_verified=False,
                                    credential_backend_verified=False)
    assert not result.allowed
    assert len(result.blockers) == 3
    with pytest.raises(ModeError):
        require_platform_startup(tmp_path, sc_runtime_available=False,
                                 os_isolation_verified=False,
                                 credential_backend_verified=False)


def test_bad_marker_fails_closed(tmp_path):
    (tmp_path / "installation-mode.json").write_text("bad")
    assert "invalid-mode-marker" in check_platform_startup(
        tmp_path, sc_runtime_available=True,
        os_isolation_verified=True, credential_backend_verified=True,
    ).blockers
