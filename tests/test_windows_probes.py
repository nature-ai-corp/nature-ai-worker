# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows registry probe must stay read-only and fail closed off Windows."""
import sys

import pytest

from nature_worker.windows_probes import inspect_windows_uninstall_registry


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows fallback test")
def test_non_windows_never_claims_clean():
    result = inspect_windows_uninstall_registry()
    assert result.check == "installed_packages"
    assert result.clean is None
