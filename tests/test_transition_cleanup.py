import pytest
from nature_worker.installation_mode import ModeError, initialize_standalone, read_mode
from nature_worker.mode_transition import commit_platform_transition
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
                                commit=lambda: commit_platform_transition(tmp_path, verify_ready=lambda: True))
    assert read_mode(tmp_path) == "platform_client"
    assert called == []
    assert not (tmp_path / FAILED_CLEANUP_MARKER).exists()


def test_failed_transition_marker_blocks_application_startup(tmp_path):
    from nature_worker.app import create_app
    from nature_worker.config import Config
    initialize_standalone(tmp_path / "data")
    (tmp_path / "data" / FAILED_CLEANUP_MARKER).write_text("unresolved")
    config = Config(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace")
    with pytest.raises(ModeError, match="cleanup verification"):
        create_app(config)
    assert read_mode(config.data_dir) == "standalone"


def test_unresolved_transition_does_not_initialize_new_workspace(tmp_path):
    from nature_worker.app import create_app
    from nature_worker.config import Config
    data_dir = tmp_path / "data"
    initialize_standalone(data_dir)
    (data_dir / FAILED_CLEANUP_MARKER).write_text("incomplete")
    workspace = tmp_path / "new-workspace"
    with pytest.raises(ModeError):
        create_app(Config(data_dir=data_dir, workspace_dir=workspace))
    assert not workspace.exists()


def test_exception_after_commit_does_not_cleanup_enterprise_data(tmp_path):
    initialize_standalone(tmp_path)
    called = []
    def commit_then_raise():
        commit_platform_transition(tmp_path, verify_ready=lambda: True)
        raise RuntimeError("interrupted just after commit")
    with pytest.raises(ModeError, match="mode committed"):
        attempt_platform_transition(
            tmp_path, prepare=lambda: True,
            cleanup_and_verify=lambda: called.append("deleted") or True,
            commit=commit_then_raise,
        )
    assert read_mode(tmp_path) == "platform_client"
    assert called == []


def test_commit_without_mode_change_is_failure_and_cleans_staging(tmp_path):
    initialize_standalone(tmp_path)
    called = []
    with pytest.raises(ModeError, match="did not activate"):
        attempt_platform_transition(
            tmp_path, prepare=lambda: True,
            cleanup_and_verify=lambda: called.append("cleanup") or True,
            commit=lambda: None,
        )
    assert called == ["cleanup"]
    assert read_mode(tmp_path) == "standalone"
