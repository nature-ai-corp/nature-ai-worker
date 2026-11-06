# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Install, repair or remove the Windows Platform Client (S-C) from this checkout.

Run with a python.org Python 3.11+ (it becomes the service's runtime). A UAC prompt
asks for administrator rights; the installer continues in that window:

    py -3.12 scripts\\platform-client-windows.py install --origin https://<platform-host> ^
        --standalone-data %LOCALAPPDATA%\\NatureAIWorker\\data
    py -3.12 scripts\\platform-client-windows.py status
    py -3.12 scripts\\platform-client-windows.py uninstall [--delete-local-data]

Exactly the committed HEAD is installed (``git archive``); pass ``--archive FILE``
with a tar made by ``git archive --format=tar HEAD`` to install from elsewhere.
Once installed, the same commands are in
``%ProgramFiles%\\NatureAIWorker\\PlatformClient\\bin\\nature-worker-platform-client-setup.cmd``,
and Apps & Features offers Uninstall and Modify (repair).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nature_worker.platform_client.windows_setup import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
