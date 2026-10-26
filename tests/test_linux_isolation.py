# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import os
import sys

import pytest
from nature_worker.installation_mode import ModeError, PLATFORM_CLIENT, initialize_platform_client, read_mode
from nature_worker.linux_isolation import inspect_linux_isolation, linux_isolation_verified

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux probe")


@linux
def test_same_account_as_standalone_is_not_isolation(tmp_path):
    sc, sa = tmp_path / "sc", tmp_path / "standalone"
    sc.mkdir(mode=0o700)
    sa.mkdir()
    findings = {f.check: f for f in inspect_linux_isolation(sc, sa)}
    assert findings["separate_from_standalone"].clean is False
    assert not linux_isolation_verified(tuple(findings.values()))


@linux
def test_group_or_other_access_to_sc_root_is_rejected(tmp_path):
    sc = tmp_path / "sc"
    sc.mkdir()
    os.chmod(sc, 0o750)
    findings = {f.check: f for f in inspect_linux_isolation(sc, tmp_path / "none")}
    assert findings["sc_root_private"].clean is False


@linux
def test_overlapping_roots_and_missing_root_are_not_verified(tmp_path):
    findings = {f.check: f for f in inspect_linux_isolation(tmp_path / "sa" / "sc", tmp_path / "sa")}
    assert findings["independent_data_roots"].clean is False
    assert findings["sc_root_private"].clean is None


def test_fresh_platform_client_install_needs_empty_root(tmp_path):
    root = tmp_path / "pc"
    initialize_platform_client(root)
    assert read_mode(root) == PLATFORM_CLIENT
    initialize_platform_client(root)  # idempotent
    standalone = tmp_path / "sa"
    standalone.mkdir()
    (standalone / "worker.db").write_bytes(b"")
    with pytest.raises(ModeError):
        initialize_platform_client(standalone)
    assert read_mode(standalone) is None


@linux
def test_recorded_standalone_owner_is_used_when_home_is_not_traversable(tmp_path):
    sc = tmp_path / "sc"
    sc.mkdir(mode=0o700)
    hidden = tmp_path / "no-access" / "data"
    same = {f.check: f for f in inspect_linux_isolation(sc, hidden, standalone_uid=os.geteuid())}
    assert same["separate_from_standalone"].clean is False
    other = {f.check: f for f in inspect_linux_isolation(sc, hidden, standalone_uid=os.geteuid() + 1)}
    assert other["separate_from_standalone"].clean is True
    assert other["sc_root_private"].clean is True
