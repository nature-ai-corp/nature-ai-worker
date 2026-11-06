# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for the public-repository history disclosure gate."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_checker():
    script = Path(__file__).resolve().parents[1] / "scripts" / "repo_checks.py"
    spec = importlib.util.spec_from_file_location("worker_repo_checks_history_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_history_scans_removed_private_addresses_hosts_and_confidential_markers(monkeypatch):
    checker = _load_checker()
    # The blob is no longer tracked in the current tree, but remains reachable
    # through a commit. Use synthetic literals, not real infrastructure values.
    historical_blob = (
        "server=10." + "18.27.36\n"
        + "host=synthetic." + "example.ts.net\n"
        + "mark=confidential " + "and proprietary\n"
    ).encode("utf-8")
    oid = "a" * 40

    def fake_run(args, **kwargs):
        command = " ".join(args)
        if "rev-list" in command:
            return SimpleNamespace(stdout=oid + " docs/old-config.txt\n")
        if "cat-file -t" in command:
            return SimpleNamespace(stdout="blob\n")
        if "cat-file blob" in command:
            return SimpleNamespace(stdout=historical_blob)
        raise AssertionError(f"Unexpected git command: {command}")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    findings = checker.Findings()
    checker.check_history(findings)
    assert {category for category, _, _ in findings} == {
        "history-internal-address",
        "history-confidential",
    }
    assert any("tailnet host name" in detail for _, _, detail in findings)
    assert any("private/tailnet address" in detail for _, _, detail in findings)
    assert all("10.18.27.36" not in detail and "synthetic.example.ts.net" not in detail
               for _, _, detail in findings)


def test_history_respects_test_fixture_address_exception_but_flags_confidential(monkeypatch):
    checker = _load_checker()
    text = ("10." + "18.27.36\n" + "confidential " + "and proprietary\n").encode()
    oid = "b" * 40

    def fake_run(args, **kwargs):
        command = " ".join(args)
        if "rev-list" in command:
            return SimpleNamespace(stdout=oid + " tests/old_fixture.txt\n")
        if "cat-file -t" in command:
            return SimpleNamespace(stdout="blob\n")
        if "cat-file blob" in command:
            return SimpleNamespace(stdout=text)
        raise AssertionError(command)

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    findings = checker.Findings()
    checker.check_history(findings)
    assert {category for category, _, _ in findings} == {"history-confidential"}
