# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Guard against accidental in-place enterprise-to-local mode fallback."""
import json

import pytest

from nature_worker.installation_mode import (
    MODE_FILE, ModeError, initialize_standalone, read_mode, require_standalone,
)
from os_helpers import make_link


def test_new_standalone_manifest(tmp_path):
    initialize_standalone(tmp_path)
    assert read_mode(tmp_path) == "standalone"
    require_standalone(tmp_path)
    assert json.loads((tmp_path / MODE_FILE).read_text()) == {
        "schema": 1, "mode": "standalone"
    }


def test_existing_legacy_standalone_directory(tmp_path):
    (tmp_path / "worker.db").write_bytes(b"old")
    initialize_standalone(tmp_path)
    assert (tmp_path / "worker.db").read_bytes() == b"old"


def test_enterprise_marker_blocks_standalone(tmp_path):
    (tmp_path / MODE_FILE).write_text('{"schema":1,"mode":"platform_client"}')
    with pytest.raises(ModeError, match="Uninstall"):
        initialize_standalone(tmp_path)
    assert read_mode(tmp_path) == "platform_client"


@pytest.mark.parametrize("value", ["garbage", "{}", '{"schema":1,"mode":"oops"}',
                                     '{"schema":2,"mode":"standalone"}'])
def test_invalid_mode_never_defaults_to_standalone(tmp_path, value):
    (tmp_path / MODE_FILE).write_text(value)
    with pytest.raises(ModeError):
        initialize_standalone(tmp_path)


def test_marker_symlink_is_rejected(tmp_path):
    other = tmp_path / "other"
    other.write_text('{"schema":1,"mode":"standalone"}')
    make_link(other, tmp_path / MODE_FILE)
    with pytest.raises(ModeError):
        initialize_standalone(tmp_path)
