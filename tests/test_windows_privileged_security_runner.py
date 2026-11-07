# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""scripts/windows_privileged_security_tests.py: the runner for the security tests that need
symbolic links or elevation. Every test it names must exist (a rename must not silently drop a
security test), and a skip must never count as a pass."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


# -- the interpreter that runs pytest (regression: 29 MISSING after a run started by another Python) --

PYTEST_ONLY = ("pytest", "_pytest", "pluggy", "iniconfig", "packaging", "pygments", "colorama", "py.py")


@pytest.fixture(scope="module")
def pytest_only_python(tmp_path_factory):
    """A Python with pytest but without the Worker's dependencies (no fastapi): the interpreter of
    the failed elevated run, where conftest could not import and nothing was reported."""
    root = tmp_path_factory.mktemp("pytest-only")
    base = getattr(sys, "_base_executable", sys.executable)
    subprocess.run([base, "-m", "venv", "--without-pip", str(root / "venv")], check=True, timeout=300)
    packages = root / "packages"
    packages.mkdir()
    site = Path(pytest.__file__).resolve().parents[1]
    for name in PYTEST_ONLY:
        if (site / name).is_dir():
            shutil.copytree(site / name, packages / name)
        elif (site / name).is_file():  # one-file modules (pytest's py.py)
            shutil.copyfile(site / name, packages / name)
    python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_site = next((root / "venv").glob("Lib/site-packages" if os.name == "nt" else "lib/python*/site-packages"))
    (venv_site / "pytest-only.pth").write_text(str(packages) + "\n")
    check = subprocess.run([str(python), "-c", "import pytest\ntry:\n import fastapi\nexcept ImportError: print('no fastapi')"],
                           capture_output=True, text=True, timeout=60)
    assert check.returncode == 0 and "no fastapi" in check.stdout, check.stdout + check.stderr
    return python


def _env():
    return {k: v for k, v in os.environ.items() if not k.startswith("NATURE_WORKER_") and not k.endswith("_API_KEY")}


def test_collection_check_refuses_an_interpreter_without_the_worker_dependencies(pytest_only_python):
    problems, output = runner.collect(str(pytest_only_python), _env())
    assert problems[0].startswith("pytest could not collect the tests (exit ")
    assert "fastapi" in problems[0] and "fastapi" in output
    assert sum(p.startswith("not collected: ") for p in problems) == 29 + len(runner.REQUIRED)


def test_collection_check_accepts_the_intended_environment():
    problems, _ = runner.collect(sys.executable, _env())
    assert problems == []


def test_a_collection_failure_reports_every_case_missing_and_never_passes(tmp_path, monkeypatch, pytest_only_python):
    monkeypatch.setattr(runner, "can_create_symlinks", lambda: True)
    out = tmp_path / "out"
    assert runner.main(["--out", str(out), "--python", str(pytest_only_python)]) == 3
    text = (out / "summary.txt").read_text(encoding="utf-8")
    assert text.splitlines()[0].endswith("COLLECTION FAILED")
    assert "29 privileged cases: 0 PASS, 0 FAIL, 0 NOT RUN, 29 MISSING" in text and "fastapi" in text
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert {p["status"] for p in summary["privileged"]} == {"MISSING"} and len(summary["privileged"]) == 29
    assert summary["interpreter"] == str(pytest_only_python) and summary["result"] != "PASS"
    assert not (out / "junit.xml").exists()


def test_pytest_runs_in_the_intended_environment_whatever_python_started_the_runner(tmp_path, monkeypatch,
                                                                                     pytest_only_python):
    # The failed run: the runner itself needs only the standard library, so it started under a
    # Python without the test dependencies and handed that interpreter to pytest.
    monkeypatch.setattr(runner, "can_create_symlinks", lambda: True)
    monkeypatch.setattr(runner.sys, "executable", str(pytest_only_python))
    out = tmp_path / "out"
    runner.main(["--out", str(out), "--python", str(Path(sys.prefix) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["runner_interpreter"] == str(pytest_only_python)
    assert summary["interpreter"] != str(pytest_only_python)
    assert summary["privileged_cases"]["MISSING"] == 0  # every case was collected and reported
    assert (out / "junit.xml").exists()


def test_the_checkouts_venv_is_preferred_over_the_starting_interpreter(tmp_path):
    assert runner.pytest_interpreter(tmp_path) == Path(sys.executable)  # no .venv: the current Python
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    assert runner.pytest_interpreter(tmp_path) == venv_python
    other = tmp_path / "other" / "python.exe"
    assert runner.pytest_interpreter(tmp_path, other) == other  # --python wins


def test_a_run_without_a_junit_report_fails_with_every_case_missing(tmp_path, monkeypatch):
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "--junitxml" in " ".join(map(str, cmd)):  # the test run: exits 0 but writes no report
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(runner, "can_create_symlinks", lambda: True)
    monkeypatch.setattr(runner, "collect", lambda python, env: ([], ""))
    monkeypatch.setattr(runner.subprocess, "run", run)
    out = tmp_path / "out"
    assert runner.main(["--out", str(out), "--python", sys.executable]) == 1
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["privileged_cases"] == {"PASS": 0, "FAIL": 0, "NOT RUN": 0, "MISSING": 29}
    assert any("no JUnit report" in p for p in summary["problems"]) and summary["result"] != "PASS"
