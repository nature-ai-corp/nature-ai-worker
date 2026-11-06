# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Platform Client Windows E2E process-scope regressions (also runnable on Linux).

Extract only the pure classifier from the Windows-only E2E driver to avoid
importing Windows service and security routines into cross-platform tests.
"""
import ast
from pathlib import Path

import pytest


@pytest.fixture
def classify():
    source = Path(__file__).resolve().parents[1] / "scripts" / "platform_client_windows_e2e.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    functions = [node for node in module.body
                 if isinstance(node, ast.FunctionDef) and node.name == "scoped_client_processes"]
    assert len(functions) == 1
    namespace = {"Path": Path}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["scoped_client_processes"]


def test_another_platform_client_is_not_e2e_residue(classify):
    processes = [
        r'15396 "C:\Program Files\NatureAIWorkerUIAcceptance\PlatformClient\runtime\python.exe" '
        r'-I "C:\Program Files\NatureAIWorkerUIAcceptance\PlatformClient\bin\platform-client-service.py"'
    ]
    assert classify(
        processes,
        Path(r"C:\Program Files\NatureAIWorkerE2E\PlatformClient"),
        "NatureWorkerPlatformClientE2E",
    ) == []


def test_real_e2e_process_remains_a_failure(classify):
    e2e = (
        r'100 "C:\Program Files\NatureAIWorkerE2E\PlatformClient\runtime\python.exe" '
        r'-I "C:\Program Files\NatureAIWorkerE2E\PlatformClient\bin\platform-client-service.py"'
    )
    service = r"101 python.exe -m NatureWorkerPlatformClientE2E"
    unrelated = r"15396 python.exe -I C:\Program Files\NatureAIWorkerUIAcceptance\PlatformClient\bin\platform-client-service.py"
    assert classify(
        [unrelated, e2e, service],
        Path(r"C:\Program Files\NatureAIWorkerE2E\PlatformClient"),
        "NatureWorkerPlatformClientE2E",
    ) == [e2e, service]


def test_e2e_prefix_must_not_match_similarly_named_directory(classify):
    process = r"100 python.exe C:\Program Files\NatureAIWorkerE2E\PlatformClientBackup\service.py"
    assert classify(
        [process], Path(r"C:\Program Files\NatureAIWorkerE2E\PlatformClient"),
        "NatureWorkerPlatformClientE2E",
    ) == []
