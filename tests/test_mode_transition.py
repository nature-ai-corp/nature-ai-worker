# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import pytest

from nature_worker.installation_mode import ModeError, initialize_standalone, read_mode
from nature_worker.mode_transition import commit_platform_transition


def test_failed_verification_preserves_standalone(tmp_path):
    initialize_standalone(tmp_path)
    original = (tmp_path / "installation-mode.json").read_bytes()
    with pytest.raises(ModeError, match="remains active"):
        commit_platform_transition(tmp_path, verify_ready=lambda: False)
    assert read_mode(tmp_path) == "standalone"
    assert (tmp_path / "installation-mode.json").read_bytes() == original


def test_auth_or_setup_exception_preserves_standalone(tmp_path):
    initialize_standalone(tmp_path)
    def fail():
        raise ConnectionError("Platform login failed")
    with pytest.raises(ConnectionError):
        commit_platform_transition(tmp_path, verify_ready=fail)
    assert read_mode(tmp_path) == "standalone"


def test_success_commits_to_platform_client(tmp_path):
    initialize_standalone(tmp_path)
    commit_platform_transition(tmp_path, verify_ready=lambda: True)
    assert read_mode(tmp_path) == "platform_client"
    with pytest.raises(ModeError):
        commit_platform_transition(tmp_path, verify_ready=lambda: True)


def test_no_transition_without_standalone_install(tmp_path):
    with pytest.raises(ModeError):
        commit_platform_transition(tmp_path, verify_ready=lambda: True)
    assert read_mode(tmp_path) is None
