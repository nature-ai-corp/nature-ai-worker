# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Upgrade transaction: eligibility, authorization, rollback and recovery.

The engine, layout, journal, bundle verification and data backup/restore are
real; only pip, processes and services are scripted (upgrade_fixtures.py).
"""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nature_worker.maintenance import read_upgrade_marker
from nature_worker.upgrade.engine import UpgradeEngine
from nature_worker.upgrade.toolkit import StopRefused, ToolkitError
from upgrade_fixtures import FakeToolkit, db_schema, make_bundle, managed_install, trusted

KEY = Ed25519PrivateKey.generate()


@pytest.fixture
def setup(tmp_path):
    layout, inst = managed_install(tmp_path, "1.0.0", schema=1)
    toolkit = FakeToolkit(layout, inst)
    engine = UpgradeEngine(layout, toolkit, keys=[trusted(KEY)])
    return engine, toolkit, layout, inst, tmp_path


def yes(_plan):
    return True


def test_successful_upgrade_order_journal_and_history(setup):
    engine, toolkit, layout, inst, tmp = setup
    toolkit.running = True
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    assert out.outcome == "succeeded", out.message
    assert toolkit.calls == ["install", "stop", "ensure_stopped", "backup", "migrate", "after_migrate",
                             "health:1.1.0", "prune"]
    assert layout.current() == "1.1.0" and db_schema(inst.data_dir) == 2
    assert layout.state() is None and read_upgrade_marker(inst.data_dir) is None
    assert toolkit.started == 1  # it was running before, so it is running again
    last = layout.history()[-1]
    assert last["outcome"] == "succeeded" and (last["from"], last["to"]) == ("1.0.0", "1.1.0")
    assert last["signer_scope"] == "local" and "token" not in json.dumps(layout.history())
    assert db_schema(__import__("pathlib").Path(last["backup"])) == 1
    assert not (layout.upgrade / "staging").exists()


def test_not_authorized_changes_nothing(setup):
    engine, toolkit, layout, inst, tmp = setup
    seen = []
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", notes="Fixes things")),
                         lambda plan: seen.append(plan.describe()) or False)
    assert out.outcome == "declined" and toolkit.calls == []
    assert seen[0]["eligible"] and seen[0]["notes"] == "Fixes things" and seen[0]["installed"] == "1.0.0"
    assert layout.current() == "1.0.0" and layout.history()[-1]["outcome"] == "declined"


@pytest.mark.parametrize("kwargs, reason", [
    ({"version": "1.0.0"}, "not newer"),
    ({"version": "0.9.0"}, "not newer"),
    ({"version": "2.0.0", "min_from": "1.5.0"}, "needs 1.5.0 or later"),
    ({"version": "1.1.0", "editions": ("platform_client",)}, "not for standalone"),
    ({"version": "1.1.0", "target": {"os": "plan9", "arch": "mips", "python": "2.7"}}, "plan9"),
    ({"version": "1.1.0", "data_schema": 0}, "data is at 1"),
])
def test_ineligible_releases_are_never_authorized_or_installed(setup, kwargs, reason):
    engine, toolkit, layout, inst, tmp = setup
    version = kwargs.pop("version")
    asked = []
    out = engine.upgrade(str(make_bundle(tmp, KEY, version, **kwargs)), lambda p: asked.append(p) or True)
    assert out.outcome == "ineligible" and reason in out.message
    assert asked == [] and toolkit.calls == [] and layout.current() == "1.0.0"


def test_untrusted_and_tampered_bundles_are_refused_before_any_check(setup):
    engine, toolkit, layout, inst, tmp = setup
    other = Ed25519PrivateKey.generate()
    out = engine.upgrade(str(make_bundle(tmp, other, "1.1.0")), yes)
    assert out.outcome == "ineligible" and "does not trust" in out.message
    bundle = make_bundle(tmp, KEY, "1.1.0")
    wheel = next((bundle / "wheels").glob("dep-*.whl"))
    data = bytearray(wheel.read_bytes())
    data[0] ^= 1
    wheel.write_bytes(bytes(data))
    out = engine.upgrade(str(bundle), yes)
    assert out.outcome == "ineligible" and "SHA-256" in out.message
    assert toolkit.calls == [] and [h["outcome"] for h in layout.history()] == ["refused", "refused"]


def test_install_failure_changes_nothing(setup):
    engine, toolkit, layout, inst, tmp = setup
    toolkit.fail["install"] = ToolkitError("pip failed")
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0")), yes)
    assert out.outcome == "failed" and "1.0.0 stays active" in out.message
    assert "remove_incomplete" in toolkit.calls and "stop" not in toolkit.calls
    assert layout.current() == "1.0.0" and layout.state() is None


def test_running_work_that_does_not_finish_aborts_without_changes(setup):
    engine, toolkit, layout, inst, tmp = setup
    toolkit.running = True
    toolkit.fail["stop"] = StopRefused("still working")
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    assert out.outcome == "aborted"
    assert layout.current() == "1.0.0" and db_schema(inst.data_dir) == 1
    assert read_upgrade_marker(inst.data_dir) is None and layout.state() is None


@pytest.mark.parametrize("step, restored", [("backup", False), ("migrate", True),
                                            ("after_migrate", True), ("health:1.1.0", True)])
def test_failure_after_stop_rolls_back_code_and_data(setup, step, restored):
    engine, toolkit, layout, inst, tmp = setup
    toolkit.running = True
    toolkit.fail[step] = ToolkitError(f"{step} broke")
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    assert out.outcome == "rolled_back", out.message
    assert layout.current() == "1.0.0" and db_schema(inst.data_dir) == 1
    assert ("restore" in toolkit.calls) is restored
    assert "health:1.0.0" in toolkit.calls and toolkit.started == 1
    assert read_upgrade_marker(inst.data_dir) is None
    aside = list((inst.data_dir / "backups").glob("failed-upgrade-*"))
    if restored:
        assert len(aside) == 1 and (aside[0] / "worker.db").exists()  # replaced data kept, not deleted
    assert layout.history()[-1]["outcome"] == "rolled_back"


def test_failed_rollback_blocks_the_worker_until_recovered(setup):
    engine, toolkit, layout, inst, tmp = setup
    toolkit.fail["health:1.1.0"] = ToolkitError("new version broken")
    toolkit.fail["health:1.0.0"] = ToolkitError("old version broken too")
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    assert out.outcome == "repair_required"
    assert layout.state()["phase"] == "repair_required"
    assert read_upgrade_marker(inst.data_dir) is not None  # the Worker refuses to start
    out = engine.recover(lambda _prompt: True)
    assert out.outcome == "rolled_back", out.message
    assert layout.current() == "1.0.0" and db_schema(inst.data_dir) == 1
    assert read_upgrade_marker(inst.data_dir) is None and layout.state() is None


def _interrupt(engine, toolkit, tmp, step):
    """Simulate a crash: the step raises BaseException, which the engine does not handle."""
    class Crash(BaseException):
        pass
    toolkit.fail[step] = Crash()
    with pytest.raises(Crash):
        engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)


@pytest.mark.parametrize("step, phase", [("install", "installing"), ("stop", "stopping"),
                                         ("backup", "backing_up"), ("after_migrate", "migrating")])
def test_interrupted_upgrade_is_recovered_only_with_confirmation(setup, step, phase):
    engine, toolkit, layout, inst, tmp = setup
    _interrupt(engine, toolkit, tmp, step)
    assert layout.state()["phase"] == phase
    prompts = []
    if phase in ("backing_up", "migrating"):
        assert engine.recover(lambda p: prompts.append(p) or False).outcome == "declined"
        assert layout.state()["phase"] == phase  # declining changes nothing
    out = engine.recover(lambda p: prompts.append(p) or True)
    assert out.outcome in ("recovered", "rolled_back"), out.message
    assert layout.current() == "1.0.0" and db_schema(inst.data_dir) == 1
    assert layout.state() is None and read_upgrade_marker(inst.data_dir) is None
    if phase == "migrating":
        assert "restores the data root from the backup" in prompts[0]


def test_interrupted_verification_is_finished_when_the_new_version_works(setup):
    engine, toolkit, layout, inst, tmp = setup
    _interrupt(engine, toolkit, tmp, "health:1.1.0")
    out = engine.recover(lambda _p: True)
    assert out.outcome == "succeeded"
    assert layout.current() == "1.1.0" and db_schema(inst.data_dir) == 2


def test_upgrade_refused_while_another_is_unfinished(setup):
    engine, toolkit, layout, inst, tmp = setup
    _interrupt(engine, toolkit, tmp, "after_migrate")
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.2.0", data_schema=2)), yes)
    assert out.outcome == "ineligible" and "recover" in out.message


def test_user_rollback_keeps_compatible_data(setup):
    engine, toolkit, layout, inst, tmp = setup
    engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=1)), yes)
    out = engine.rollback(lambda prompt: "data stays as it is" in prompt)
    assert out.outcome == "rolled_back_by_user", out.message
    assert layout.current() == "1.0.0" and "restore" not in toolkit.calls


def test_user_rollback_restores_backup_only_when_data_is_incompatible(setup):
    engine, toolkit, layout, inst, tmp = setup
    engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    prompts = []
    assert engine.rollback(lambda p: prompts.append(p) or False).outcome == "declined"
    assert layout.current() == "1.1.0"
    out = engine.rollback(lambda p: True)
    assert out.outcome == "rolled_back_by_user"
    assert "restored from the backup" in prompts[0] and "moved aside" in prompts[0]
    assert layout.current() == "1.0.0" and db_schema(inst.data_dir) == 1
    assert list((inst.data_dir / "backups").glob("rolled-back-*"))


def test_user_rollback_refused_when_needed_backup_is_missing(setup):
    import shutil
    engine, toolkit, layout, inst, tmp = setup
    engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    shutil.rmtree(layout.history()[-1]["backup"])
    out = engine.rollback(lambda p: True)
    assert out.outcome == "failed" and "missing" in out.message
    assert layout.current() == "1.1.0" and db_schema(inst.data_dir) == 2


def test_installation_mode_change_during_upgrade_rolls_back(setup):
    engine, toolkit, layout, inst, tmp = setup
    original = toolkit.health

    def tamper(version, token):
        if version == "1.1.0":  # something rewrites the marker while the new version runs
            (inst.data_dir / "installation-mode.json").write_text('{"schema": 1, "mode": "platform_client"}\n')
        return original(version, token)
    toolkit.health = tamper
    out = engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), yes)
    assert out.outcome == "rolled_back" and "installation mode changed" in out.message
    assert json.loads((inst.data_dir / "installation-mode.json").read_text())["mode"] == "standalone"


def test_health_check_gets_the_marker_token(setup):
    from nature_worker.maintenance import token_digest
    engine, toolkit, layout, inst, tmp = setup
    seen = {}
    original = toolkit.health

    def capture(version, token):
        seen["marker"] = read_upgrade_marker(inst.data_dir)
        return original(version, token)
    toolkit.health = capture
    engine.upgrade(str(make_bundle(tmp, KEY, "1.1.0")), yes)
    assert seen["marker"]["token_sha256"] == token_digest(toolkit.health_tokens[0])
    assert toolkit.health_tokens[0] not in (layout.state_file.parent / "history.jsonl").read_text()


def test_platform_client_requires_the_same_client_interface(tmp_path):
    from nature_worker.upgrade.layout import Installation
    layout, _ = managed_install(tmp_path)
    inst = Installation("platform_client", tmp_path / "pc", None, "nature-pc", "x.service", 8090)
    layout.write_installation(inst)
    inst.data_dir.mkdir()
    (inst.data_dir / "installation-mode.json").write_text('{"schema": 1, "mode": "platform_client"}\n')
    toolkit = FakeToolkit(layout, inst)
    toolkit.edition = "platform_client"
    toolkit.data_schema = lambda v: 1
    engine = UpgradeEngine(layout, toolkit, keys=[trusted(KEY)])
    plan = engine.check(str(make_bundle(tmp_path, KEY, "1.1.0", client_interface="2.0")))
    assert not plan.eligible
    assert any("Client Interface 2.0" in r for r in plan.describe()["reasons"])
    assert engine.check(str(make_bundle(tmp_path, KEY, "1.1.0"))).eligible
