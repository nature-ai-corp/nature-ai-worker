import pytest
from nature_worker.installation_mode import ModeError, initialize_standalone, read_mode
from nature_worker.transition_cleanup import (
    FAILED_CLEANUP_MARKER, attempt_platform_transition, require_no_failed_sc_residue
)


def test_failure_cleanup_removes_staged_server_data(tmp_path):
    initialize_standalone(tmp_path)
    staged = tmp_path / "sc-staging"
    def prepare():
        staged.write_text("enterprise test material")
        return False
    def cleanup():
        staged.unlink()
        return not staged.exists()
    with pytest.raises(ModeError):
        attempt_platform_transition(tmp_path, prepare=prepare,
                                    cleanup_and_verify=cleanup,
                                    commit=lambda: pytest.fail("must not commit"))
    assert read_mode(tmp_path) == "standalone"
    assert not staged.exists()
    require_no_failed_sc_residue(tmp_path)


def test_cleanup_failure_blocks_standalone_access(tmp_path):
    initialize_standalone(tmp_path)
    with pytest.raises(ModeError):
        attempt_platform_transition(tmp_path, prepare=lambda: False,
                                    cleanup_and_verify=lambda: False,
                                    commit=lambda: None)
    assert read_mode(tmp_path) == "standalone"
    assert (tmp_path / FAILED_CLEANUP_MARKER).exists()
    with pytest.raises(ModeError, match="cleanup verification"):
        require_no_failed_sc_residue(tmp_path)


def test_prepare_exception_still_triggers_cleanup(tmp_path):
    initialize_standalone(tmp_path)
    called = []
    def failing():
        raise ConnectionError("auth failure")
    with pytest.raises(ConnectionError):
        attempt_platform_transition(tmp_path, prepare=failing,
                                    cleanup_and_verify=lambda: called.append(1) or True,
                                    commit=lambda: None)
    assert called == [1]
    assert read_mode(tmp_path) == "standalone"


def test_success_does_not_invoke_cleanup(tmp_path):
    initialize_standalone(tmp_path)
    called = []
    attempt_platform_transition(tmp_path, prepare=lambda: True,
                                cleanup_and_verify=lambda: called.append(1) or True,
                                commit=lambda: None)
    assert called == []
    assert not (tmp_path / FAILED_CLEANUP_MARKER).exists()
