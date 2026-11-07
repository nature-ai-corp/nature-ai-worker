# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Run the Windows security tests that ordinary test runs skip, on a dedicated test computer.

    python scripts/windows_privileged_security_tests.py --out NEW_DIR

29 security tests need to create symbolic links (Developer Mode or an elevated prompt) or to
read LocalService's registry hive and Credential Manager (elevated). Without those rights
pytest skips them, on developer computers and in CI alike. This runner makes the result
explicit: it checks the rights first, runs exactly these tests and nothing else, and reports
every skip among them as NOT RUN, never as a pass.

What the tests touch: symbolic links and files only inside pytest's own temporary folder (a new
short folder such as C:\\nws-xxxx, removed afterwards); the native tests only list and read (LocalService's
profile, registry hive and Credential Manager folder) and fail if anything tries to delete. No
service, installation, registry value, network or port is changed or used.

Run it from a clean checkout of the commit under test, with the locked dependencies installed:
    elevated PowerShell:  .\\.venv\\Scripts\\python.exe scripts\\windows_privileged_security_tests.py --out C:\\nw-sec-1
Writes NEW_DIR\\summary.txt, summary.json, collect-output.txt, pytest-output.txt and junit.xml. Exit 0 only if every required test passed.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SYMLINK = "symbolic links (Developer Mode or elevated)"
ADMIN = "administrator (elevated prompt)"
# Test functions (every parametrized case included) and the right each needs.
REQUIRED = {
    "tests/test_security.py::test_escape_attempts_are_blocked": SYMLINK,
    "tests/test_security.py::test_writes_outside_workspace_are_blocked": SYMLINK,
    "tests/test_security.py::test_symlinks_inside_workspace_still_work": SYMLINK,
    "tests/test_security.py::test_listing_hides_escaping_symlinks": SYMLINK,
    "tests/test_security.py::test_dangling_symlinks_do_not_break_listing": SYMLINK,
    "tests/test_document_input.py::test_a_document_link_out_of_the_workspace_is_refused": SYMLINK,
    "tests/test_document_writing.py::test_a_worker_file_replaced_by_a_symlink_to_a_user_file_needs_approval": SYMLINK,
    "tests/test_installation_mode.py::test_marker_symlink_is_rejected": SYMLINK,
    "tests/test_platform_crypto.py::test_rejects_symlinked_file": SYMLINK,
    "tests/test_platform_storage.py::test_symlink_root_blocks": SYMLINK,
    "tests/test_release.py::test_damaged_or_swapped_files_are_refused": SYMLINK,
    "tests/test_windows_platform_client.py::test_remove_service_profile_refuses_a_linked_profile_folder": SYMLINK,
    "tests/test_windows_platform_client.py::test_the_observed_windows_profile_is_all_windows_standard": SYMLINK,
    "tests/test_windows_platform_client.py::test_the_observed_profile_is_removed_on_request_once_its_hive_is_unloaded":
        SYMLINK,
    "tests/test_windows_platform_client.py::test_a_file_windows_does_not_create_blocks_removal": SYMLINK,
    "tests/test_windows_platform_client.py::test_a_link_that_is_not_windows_own_junction_blocks_removal": SYMLINK,
    "tests/test_windows_platform_client.py::test_a_kept_profile_never_fails_an_ordinary_uninstall_or_blocks_a_new_installation":
        SYMLINK,
    "tests/test_windows_platform_client.py::test_uninstall_never_follows_links_out_of_the_data_root": SYMLINK,
    "tests/test_windows_platform_client.py::test_native_service_profile_inspection_is_read_only": ADMIN,
    "tests/test_windows_e2e_lifecycle.py::test_native_service_profile_of_a_builtin_account": ADMIN,
}
# Reported, not required: needs the Platform Client e2e service profile
# (scripts/platform_client_windows_e2e.py), which only some test computers have.
OPTIONAL = {
    "tests/test_windows_platform_client.py::test_native_e2e_service_profile_holds_only_windows_standard_content":
        "the NatureWorkerPlatformClientE2E service profile, elevated",
}


def short_base() -> Path:
    """A new, empty folder with a short path. The Platform Client profile tests build a copy of a
    real Windows service profile (deepest file about 145 characters below the base) and refuse to
    run beyond MAX_PATH, so %TEMP% under a long user name is too deep. Prefer the system drive's
    root (C:\\nws-xxxx); fall back to %TEMP% if that folder cannot be created."""
    root = os.environ.get("SystemDrive", "C:") + "\\"
    try:
        return Path(tempfile.mkdtemp(prefix="nws-", dir=root))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="nws-"))


def can_create_symlinks() -> bool:
    with tempfile.TemporaryDirectory(prefix="nw-link-probe-") as tmp:
        target = Path(tmp) / "target"
        target.write_text("x")
        try:
            os.symlink(target, Path(tmp) / "link")
            return True
        except OSError:
            return False


def developer_mode() -> bool | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock") as key:
            return winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")[0] == 1
    except OSError:
        return None


def elevated() -> bool:
    sys.path.insert(0, str(REPO / "src"))
    from nature_worker.windows_security import current_token
    return bool(current_token().elevated)


def function_of(classname: str, name: str) -> str:
    """'tests.test_x' + 'test_f[case]' -> 'tests/test_x.py::test_f'."""
    return classname.replace(".", "/") + ".py::" + name.split("[", 1)[0]


def outcomes(junit: Path) -> dict[str, list[tuple[str, str, str]]]:
    """Per test function: (case, outcome, message) for every case pytest reported."""
    found: dict[str, list[tuple[str, str, str]]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            node = case.find(tag)
            if node is not None:
                outcome, message = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag], \
                    (node.get("message") or "").strip()
                break
        found.setdefault(function_of(case.get("classname", ""), case.get("name", "")), []).append(
            (case.get("name", ""), outcome, message))
    return found


_WPC = "tests/test_windows_platform_client.py::"
# The 29 cases that need symbolic links or elevation, each of which must be reported and PASS.
# (The same test functions have further cases that run anywhere; they must pass too.)
PRIVILEGED = (
    "tests/test_security.py::test_escape_attempts_are_blocked[link_to_secret.txt]",
    "tests/test_security.py::test_escape_attempts_are_blocked[link_dir/secret.txt]",
    "tests/test_security.py::test_escape_attempts_are_blocked[link_dir]",
    "tests/test_security.py::test_writes_outside_workspace_are_blocked[link_dir/evil.txt]",
    "tests/test_security.py::test_writes_outside_workspace_are_blocked[link_to_secret.txt]",
    "tests/test_security.py::test_symlinks_inside_workspace_still_work",
    "tests/test_security.py::test_listing_hides_escaping_symlinks",
    "tests/test_security.py::test_dangling_symlinks_do_not_break_listing",
    "tests/test_document_input.py::test_a_document_link_out_of_the_workspace_is_refused",
    "tests/test_document_writing.py::test_a_worker_file_replaced_by_a_symlink_to_a_user_file_needs_approval",
    "tests/test_installation_mode.py::test_marker_symlink_is_rejected",
    "tests/test_platform_crypto.py::test_rejects_symlinked_file",
    "tests/test_platform_storage.py::test_symlink_root_blocks",
    "tests/test_release.py::test_damaged_or_swapped_files_are_refused[symlink]",
    _WPC + "test_remove_service_profile_refuses_a_linked_profile_folder",
    _WPC + "test_the_observed_windows_profile_is_all_windows_standard",
    _WPC + "test_the_observed_profile_is_removed_on_request_once_its_hive_is_unloaded",
    _WPC + "test_a_file_windows_does_not_create_blocks_removal[Documents/notes.txt-files Windows does not create: Documents]",
    _WPC + "test_a_file_windows_does_not_create_blocks_removal[Desktop/report.pdf-files Windows does not create: Desktop]",
    _WPC + "test_a_file_windows_does_not_create_blocks_removal[AppData/Local/Temp/nwpc.tmp-files Windows does not create: AppData]",
    _WPC + "test_a_file_windows_does_not_create_blocks_removal[AppData/Roaming/Vendor/settings.json-files Windows does not create: AppData]",
    _WPC + "test_a_file_windows_does_not_create_blocks_removal[notes.txt-files Windows does not create: notes.txt]",
    _WPC + "test_a_link_that_is_not_windows_own_junction_blocks_removal[Documents/My Music-outside]",
    _WPC + "test_a_link_that_is_not_windows_own_junction_blocks_removal[Documents/My Music-Documents]",
    _WPC + "test_a_link_that_is_not_windows_own_junction_blocks_removal[Desktop/shortcut-Music]",
    _WPC + "test_a_kept_profile_never_fails_an_ordinary_uninstall_or_blocks_a_new_installation",
    _WPC + "test_uninstall_never_follows_links_out_of_the_data_root",
    _WPC + "test_native_service_profile_inspection_is_read_only",
    "tests/test_windows_e2e_lifecycle.py::test_native_service_profile_of_a_builtin_account",
)
OUTCOME = {"passed": "PASS", "failed": "FAIL", "error": "FAIL", "skipped": "NOT RUN"}


def evaluate(found: dict[str, list[tuple[str, str, str]]]) -> tuple[bool, list[dict], list[dict]]:
    """Per function (every case must pass) and per privileged case (each must be present and PASS)."""
    rows, ok = [], True
    for test, right in {**REQUIRED, **OPTIONAL}.items():
        cases = found.get(test, [])
        required = test in REQUIRED
        if not cases:
            status = "MISSING"
        elif any(o in ("failed", "error") for _, o, _ in cases):
            status = "FAIL"
        elif any(o == "skipped" for _, o, _ in cases):
            status = "NOT RUN"
        else:
            status = "PASS"
        if required and status != "PASS":
            ok = False
        rows.append({"test": test, "required": required, "needs": right, "status": status, "cases": len(cases),
                     "details": [f"{n}: {o}{' - ' + m if m else ''}" for n, o, m in cases if o != "passed"]})
    by_id = {f"{test.split('::')[0]}::{name}": (outcome, message)
             for test, cases in found.items() for name, outcome, message in cases}
    privileged = []
    for case in PRIVILEGED:
        outcome, message = by_id.get(case, ("missing", "not reported"))
        status = OUTCOME.get(outcome, "MISSING")
        ok = ok and status == "PASS"
        privileged.append({"case": case, "status": status, "message": message})
    return ok, rows, privileged


def pytest_interpreter(repo: Path = REPO, override: Path | None = None) -> Path:
    """The interpreter that runs pytest: the given one, else the checkout's own .venv (which has
    the locked test dependencies), never just whichever Python started this script. A runner
    started by another Python once ran pytest without the Worker's dependencies: conftest
    failed to import and all 29 cases were MISSING."""
    if override is not None:
        return override
    venv = repo / ".venv" / "Scripts" / "python.exe"
    return venv if venv.is_file() else Path(sys.executable)


def collect(python: str, env: dict) -> tuple[list[str], str]:
    """Collect the tests with the interpreter that will run them, before running anything.
    Returns the problems (empty if every required test function and all 29 privileged cases are
    collected) and pytest's output. An interpreter without the test dependencies (for example
    not the checkout's .venv) fails here, with pytest's own error, instead of reporting 29
    MISSING cases without a reason."""
    proc = subprocess.run([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           *REQUIRED, *OPTIONAL], cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
    output = proc.stdout + proc.stderr
    collected = {line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if "::" in line}
    problems = []
    if proc.returncode != 0:
        errors = [line.strip() for line in output.splitlines() if line.startswith(("E ", "ERROR", "ImportError"))]
        problems.append(f"pytest could not collect the tests (exit {proc.returncode}) with {python}: "
                        + ("; ".join(errors[-3:]) or "see collect-output.txt"))
    problems += [f"not collected: {case}" for case in PRIVILEGED if case not in collected]
    problems += [f"not collected: {test}" for test in REQUIRED
                 if not any(c == test or c.startswith(test + "[") for c in collected)]
    return problems, output


def write_reports(out: Path, summary: dict, rows: list[dict], privileged: list[dict]) -> list[str]:
    counts = summary["privileged_cases"]
    lines = [f"Nature AI Worker privileged Windows security tests: {summary['result']}",
             f"commit {summary['commit']}{'' if summary['worktree_clean'] else ' (WORKING TREE NOT CLEAN)'}; "
             f"Windows {summary['windows']}; Python {summary['python']}; elevated {summary['elevated']}; "
             f"Developer Mode {summary['developer_mode']}; symlinks {summary['can_create_symlinks']}",
             f"interpreter {summary['interpreter']} (prefix {summary['interpreter_prefix']}); "
             f"pytest exit {summary.get('pytest_exit')}"]
    lines += [f"PROBLEM: {problem}" for problem in summary.get("problems", [])]
    lines += ["", f"The {len(PRIVILEGED)} privileged cases: " + ", ".join(f"{n} {s}" for s, n in counts.items()), ""]
    for i, p in enumerate(privileged, 1):
        lines.append(f"{i:2}. {p['status']:8} {p['case']}" + (f"  -- {p['message']}" if p["status"] != "PASS" else ""))
    lines += ["", "Whole test functions (every case, including those that run without privileges):"]
    for row in rows:
        lines.append(f"{row['status']:8} {'required' if row['required'] else 'optional'}  {row['test']} "
                     f"({row['cases']} case{'s' if row['cases'] != 1 else ''}; needs {row['needs']})")
        lines += [f"           {d}" for d in row["details"]]
    (out / "summary.json").write_text(json.dumps({**summary, "privileged": privileged, "tests": rows}, indent=2) + "\n",
                                      encoding="utf-8")
    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="new folder for the reports")
    parser.add_argument("--python", type=Path, help="interpreter for pytest (default: the checkout's .venv)")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows only.")
    if args.out.exists():
        parser.error(f"{args.out} exists: use a new folder.")
    links, admin, devmode = can_create_symlinks(), elevated(), developer_mode()
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True,
                                text=True).stdout.strip())
    # pytest runs with the checkout's environment (locked dependencies), verified by collection below.
    python = str(pytest_interpreter(override=args.python))
    probe = subprocess.run([python, "-c", "import sys; print(sys.prefix); print(sys.version.split()[0])"],
                           capture_output=True, text=True)
    prefix, version = (probe.stdout.split() + ["?", "?"])[:2] if probe.returncode == 0 else ("not runnable", "?")
    env_info = {"commit": commit, "worktree_clean": not dirty, "windows": platform.version(),
                "python": version, "interpreter": python, "interpreter_prefix": prefix,
                "runner_interpreter": sys.executable,
                "elevated": admin, "developer_mode": devmode, "can_create_symlinks": links}
    print(json.dumps(env_info, indent=2))
    if not links:
        print("Refused: this account cannot create symbolic links. Run from an elevated prompt, or enable "
              "Developer Mode, on the dedicated test computer.")
        return 2
    args.out.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NATURE_WORKER_") and not k.endswith("_API_KEY")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    problems, collect_output = collect(python, env)
    (args.out / "collect-output.txt").write_text(collect_output, encoding="utf-8")
    if problems:
        _, rows, privileged = evaluate({})
        for p in privileged:
            p["message"] = "not run: the collection check failed (see PROBLEM above)"
        summary = {**env_info, "pytest_exit": None, "problems": problems, "result": "COLLECTION FAILED",
                   "privileged_cases": {"PASS": 0, "FAIL": 0, "NOT RUN": 0, "MISSING": len(PRIVILEGED)}}
        print("\n".join(write_reports(args.out, summary, rows, privileged)))
        print(f"pytest needs the checkout's environment: create {REPO / '.venv'} with the locked dependencies.")
        return 3
    base = short_base()  # removed afterwards
    env_info["base_folder"] = str(base)
    junit = args.out / "junit.xml"
    started = time.time()
    try:
        proc = subprocess.run([python, "-m", "pytest", "-p", "no:cacheprovider", "-rs", "-q",
                               f"--basetemp={base / 't'}", f"--junitxml={junit}", *REQUIRED, *OPTIONAL],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=1800)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    (args.out / "pytest-output.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    ok, rows, privileged = evaluate(outcomes(junit) if junit.exists() else {})  # no report: all MISSING
    problems = [] if proc.returncode == 0 else [f"pytest exited {proc.returncode} (see pytest-output.txt)"]
    if not junit.exists():
        problems.append("pytest wrote no JUnit report: no case can count as run")
    ok = ok and not problems
    counts = {s: sum(1 for p in privileged if p["status"] == s) for s in ("PASS", "FAIL", "NOT RUN", "MISSING")}
    summary = {**env_info, "pytest_exit": proc.returncode, "seconds": round(time.time() - started),
               "temp_removed": not base.exists(), "problems": problems,
               "result": "PASS" if ok else "INCOMPLETE OR FAILED", "privileged_cases": counts}
    print("\n".join(write_reports(args.out, summary, rows, privileged)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
