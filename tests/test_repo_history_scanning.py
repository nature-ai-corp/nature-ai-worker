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


def test_exact_audited_legacy_rc_warning_is_benign_but_other_markers_are_not(monkeypatch):
    checker = _load_checker()
    blob_id = "0984e73a90f68584c1e2b2c8ebd44a4424d1bed6"
    warning = ("    throw 'Unexpected build provenance or local trust-key override; "
               "do not " "distribute this build.'")
    base_lines = ["# historical RC helper"] * 80 + [warning, "other=confidential " + "and proprietary"]
    text = ("\n".join(base_lines) + "\n").encode()

    def scan(oid, path, blob):
        def fake_run(args, **kwargs):
            command = " ".join(args)
            if "rev-list" in command:
                return SimpleNamespace(stdout=oid + " " + path + "\n")
            if "cat-file -t" in command:
                return SimpleNamespace(stdout="blob\n")
            if "cat-file blob" in command:
                return SimpleNamespace(stdout=blob)
            raise AssertionError(command)
        monkeypatch.setattr(checker.subprocess, "run", fake_run)
        found = checker.Findings()
        checker.check_history(found)
        return [(kind, location) for kind, location, _ in found]

    findings = scan(blob_id, "scripts/windows_rc_stage.ps1", text)
    assert len(findings) == 1 and findings[0][0] == "history-confidential"
    assert findings[0][1].endswith(":82")
    # Any other object ID or path must still flag the old warning.
    assert len(scan("c" * 40, "scripts/windows_rc_stage.ps1", text)) == 2
    assert len(scan(blob_id, "docs/windows_rc_stage.ps1", text)) == 2
    # A modified warning at that line is NOT exempted.
    altered = text.replace(b"do not " + b"distribute", b"confidential " + b"and proprietary")
    assert len(scan(blob_id, "scripts/windows_rc_stage.ps1", altered)) == 2
