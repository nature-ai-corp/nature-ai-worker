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
short folder under %TEMP%, removed afterwards); the native tests only list and read (LocalService's
profile, registry hive and Credential Manager folder) and fail if anything tries to delete. No
service, installation, registry value, network or port is changed or used.

Run it from a clean checkout of the commit under test, with the locked dependencies installed:
    elevated PowerShell:  .\\.venv\\Scripts\\python.exe scripts\\windows_privileged_security_tests.py --out C:\\nw-sec-1
Writes NEW_DIR\\summary.txt, summary.json and junit.xml. Exit 0 only if every required test passed.
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


def evaluate(found: dict[str, list[tuple[str, str, str]]]) -> tuple[bool, list[dict]]:
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
    return ok, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="new folder for the reports")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows only.")
    if args.out.exists():
        parser.error(f"{args.out} exists: use a new folder.")
    links, admin, devmode = can_create_symlinks(), elevated(), developer_mode()
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True,
                                text=True).stdout.strip())
    env_info = {"commit": commit, "worktree_clean": not dirty, "windows": platform.version(), "python": sys.version.split()[0],
                "elevated": admin, "developer_mode": devmode, "can_create_symlinks": links}
    print(json.dumps(env_info, indent=2))
    if not links:
        print("Refused: this account cannot create symbolic links. Run from an elevated prompt, or enable "
              "Developer Mode, on the dedicated test computer.")
        return 2
    args.out.mkdir(parents=True)
    # A new folder of its own, removed afterwards (tests keep their trees within MAX_PATH from %TEMP%).
    base = Path(tempfile.mkdtemp(prefix="nw-sec-"))
    junit = args.out / "junit.xml"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NATURE_WORKER_") and not k.endswith("_API_KEY")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", "-q",
                               f"--basetemp={base / 't'}", f"--junitxml={junit}", *REQUIRED, *OPTIONAL],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=1800)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    (args.out / "pytest-output.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    ok, rows = evaluate(outcomes(junit)) if junit.exists() else (False, [])
    summary = {**env_info, "pytest_exit": proc.returncode, "seconds": round(time.time() - started),
               "temp_removed": not base.exists(), "result": "PASS" if ok else "INCOMPLETE OR FAILED", "tests": rows}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = [f"Nature AI Worker privileged Windows security tests: {summary['result']}",
             f"commit {commit}{'' if not dirty else ' (WORKING TREE NOT CLEAN)'}; Windows {env_info['windows']}; "
             f"Python {env_info['python']}; elevated {admin}; Developer Mode {devmode}; symlinks {links}", ""]
    for row in rows:
        lines.append(f"{row['status']:8} {'required' if row['required'] else 'optional'}  {row['test']} "
                     f"({row['cases']} case{'s' if row['cases'] != 1 else ''}; needs {row['needs']})")
        lines += [f"           {d}" for d in row["details"]]
    (args.out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
