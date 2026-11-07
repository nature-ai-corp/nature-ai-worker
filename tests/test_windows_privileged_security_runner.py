# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""scripts/windows_privileged_security_tests.py: the runner for the security tests that need
symbolic links or elevation. Every test it names must exist (a rename must not silently drop a
security test), and a skip must never count as a pass."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import windows_privileged_security_tests as runner  # noqa: E402


def test_every_named_security_test_exists():
    ids = [*runner.REQUIRED, *runner.OPTIONAL]
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *ids],
                          cwd=REPO, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    collected = {line.split("[", 1)[0] for line in proc.stdout.splitlines() if "::" in line}
    assert collected == set(ids)
    assert len(runner.REQUIRED) == 20 and len(runner.OPTIONAL) == 1


def _junit(tmp_path, cases):
    body = "".join(
        f'<testcase classname="{cls}" name="{name}">{extra}</testcase>' for cls, name, extra in cases)
    path = tmp_path / "junit.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{body}</testsuite></testsuites>')
    return path


def _cls(test_id):
    return test_id.split("::")[0][:-3].replace("/", ".")


def _all_pass():
    """Every privileged case, plus one ordinary case of every function, all passing."""
    return ([(_cls(c), c.split("::")[1], "") for c in runner.PRIVILEGED]
            + [(_cls(t), t.split("::")[1] + "[a]", "") for t in [*runner.REQUIRED, *runner.OPTIONAL]])


def test_all_required_tests_passing_is_a_pass(tmp_path):
    ok, rows, _ = runner.evaluate(runner.outcomes(_junit(tmp_path, _all_pass())))
    assert ok and {r["status"] for r in rows} == {"PASS"}


def test_a_skipped_case_is_not_run_not_a_pass(tmp_path):
    cases = _all_pass()
    cls, name, _ = cases[0]
    cases.append((cls, name.replace("[a]", "[b]"), '<skipped message="symbolic links not permitted"/>'))
    ok, rows, _ = runner.evaluate(runner.outcomes(_junit(tmp_path, cases)))
    assert not ok
    row = next(r for r in rows if r["test"] == next(iter(runner.REQUIRED)))
    assert row["status"] == "NOT RUN" and "symbolic links not permitted" in row["details"][0]


def test_a_failure_or_a_missing_test_fails(tmp_path):
    cases = _all_pass()
    cls, name, _ = cases[-len(runner.REQUIRED) - 1 + 1]  # the ordinary case of the second function
    cases[-len(runner.REQUIRED) - 1 + 1] = (cls, name, '<failure message="boom"/>')
    ok, rows, _ = runner.evaluate(runner.outcomes(_junit(tmp_path, cases[:-3])))
    assert not ok
    statuses = {r["test"]: r["status"] for r in rows}
    assert statuses[list(runner.REQUIRED)[1]] == "FAIL"
    assert "MISSING" in statuses.values()


def test_only_the_optional_test_skipped_is_still_a_pass(tmp_path):
    cases = [c for c in _all_pass() if not c[1].startswith("test_native_e2e_service_profile")]
    cls, _, _ = _all_pass()[-1]
    cases.append((cls, "test_native_e2e_service_profile_holds_only_windows_standard_content",
                  '<skipped message="no e2e service profile on this computer"/>'))
    ok, rows, _ = runner.evaluate(runner.outcomes(_junit(tmp_path, cases)))
    assert ok and next(r for r in rows if not r["required"])["status"] == "NOT RUN"


def test_each_privileged_case_is_reported_individually(tmp_path):
    cases = _all_pass()
    skipped = runner.PRIVILEGED[5]
    cases = [c if f"{c[0].replace('.', '/')}.py::{c[1]}" != skipped else
             (c[0], c[1], '<skipped message="symbolic links not permitted"/>') for c in cases]
    missing = runner.PRIVILEGED[-1]
    cases = [c for c in cases if f"{c[0].replace('.', '/')}.py::{c[1]}" != missing]
    ok, _, privileged = runner.evaluate(runner.outcomes(_junit(tmp_path, cases)))
    assert not ok and len(privileged) == 29 == len(set(runner.PRIVILEGED))
    status = {p["case"]: p["status"] for p in privileged}
    assert status[skipped] == "NOT RUN" and status[missing] == "MISSING"
    assert sum(s == "PASS" for s in status.values()) == 27


def test_privileged_cases_are_exactly_the_ones_skipped_without_privileges(tmp_path):
    """Without symlink rights (developer computers, CI) exactly these 29 cases skip; with them,
    all pass. Either way the runner's list matches the tests as they are."""
    junit = tmp_path / "junit.xml"
    base = runner.short_base()  # as the runner does: the profile tests refuse bases near MAX_PATH
    try:
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}",
                        f"--basetemp={base / 't'}", *runner.REQUIRED], cwd=REPO, capture_output=True, timeout=600)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    found = runner.outcomes(junit)
    every = {f"{t.split('::')[0]}::{n}": o for t, cases in found.items() for n, o, _ in cases}
    assert set(runner.PRIVILEGED) <= set(every)
    if runner.can_create_symlinks():
        assert {every[c] for c in runner.PRIVILEGED} <= {"passed", "skipped"}
    else:
        assert {c for c, o in every.items() if o == "skipped"} == set(runner.PRIVILEGED)
        assert all(o == "passed" for c, o in every.items() if c not in runner.PRIVILEGED)
