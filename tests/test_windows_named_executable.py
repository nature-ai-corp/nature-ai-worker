# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows interpreter-copy compatibility for the named Standalone process."""
import shutil
import subprocess
import sys
import venv

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named venv executable")
def test_renamed_windows_venv_launcher_can_run_installed_python(tmp_path):
    venv_dir = tmp_path / "worker-venv"
    venv.create(venv_dir, with_pip=False)
    scripts = venv_dir / "Scripts"
    original = scripts / "python.exe"
    renamed = scripts / "NatureAIWorker.exe"
    assert original.exists()
    shutil.copy2(original, renamed)
    result = subprocess.run(
        [str(renamed), "-I", "-c", "import sys; print(sys.version_info.major)"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3"
