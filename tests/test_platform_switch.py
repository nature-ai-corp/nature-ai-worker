# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import pytest
from nature_worker.installation_mode import initialize_standalone, read_mode, ModeError
from nature_worker.platform_switch import TransitionOperations, switch_to_platform_client
from nature_worker.transition_cleanup import FAILED_CLEANUP_MARKER


def operations(trace, fail_at=None, cleanup=True):
    def check(name):
        def call():
            trace.append(name)
            return name != fail_at
        return call
    return TransitionOperations(
        authenticate_platform=check("auth"),
        verify_sc_interface=check("interface"),
        verify_os_isolation=check("isolation"),
        verify_credential_backend=check("vault"),
        verify_staging_ready=check("staging"),
        cleanup_staging_and_verify=lambda: trace.append("cleanup") or cleanup,
    )


def test_success_commits_after_all_checks(tmp_path):
    initialize_standalone(tmp_path)
    trace = []
    switch_to_platform_client(tmp_path, operations(trace))
    assert read_mode(tmp_path) == "platform_client"
    assert trace == ["isolation", "vault", "auth", "interface", "staging"]


@pytest.mark.parametrize("failed", ["isolation", "vault", "auth", "interface", "staging"])
def test_failed_switch_keeps_standalone_and_cleans(tmp_path, failed):
    initialize_standalone(tmp_path)
    trace = []
    with pytest.raises(ModeError):
        switch_to_platform_client(tmp_path, operations(trace, fail_at=failed))
    assert read_mode(tmp_path) == "standalone"
    assert trace[-1] == "cleanup"
    assert not (tmp_path / FAILED_CLEANUP_MARKER).exists()


def test_cleanup_failure_blocks_local_agent(tmp_path):
    initialize_standalone(tmp_path)
    with pytest.raises(ModeError):
        switch_to_platform_client(tmp_path, operations([], fail_at="auth", cleanup=False))
    assert read_mode(tmp_path) == "standalone"
    assert (tmp_path / FAILED_CLEANUP_MARKER).exists()
