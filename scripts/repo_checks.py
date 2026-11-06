# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Repository checks for an open-source release (stdlib only; run in CI).

    python scripts/repo_checks.py                 files tracked or about to be committed
    python scripts/repo_checks.py --licenses      also: installed dependency licenses
    python scripts/repo_checks.py --history       also: every blob in the git history

Checks: the Apache-2.0 LICENSE text and NOTICE; SPDX headers in source files;
credentials and private keys; sensitive files (keys, databases, .env, user
data); internal network addresses and proprietary/confidential markers;
release artifacts and binaries; and, with --licenses, that every pinned
dependency has a license compatible with distributing this project under
Apache-2.0 and is listed in THIRD_PARTY.md.

A finding that is intentional can be allowed on its line with the comment
``repo-checks: allow`` and a reason. Exit code 1 if anything is found.
These checks reduce accidents; they do not prove the absence of secrets.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
SPDX = "SPDX-License-Identifier: Apache-2.0"
HEADER_SUFFIXES = {".py", ".sh", ".js", ".css", ".html", ".ps1"}
MAX_FILE_BYTES = 2_000_000

SECRETS = {
    "OpenAI-style API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "credential in URL": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{6,}@[A-Za-z0-9.-]+"),
}
# Obvious test placeholders (real keys are random and do not contain these words).
PLACEHOLDER = re.compile(r"test|dummy|fake|example|secret|smoke|placeholder|e2e|-key-|x{6}|0{8}", re.I)

SENSITIVE_FILES = [
    ".env", ".env.*", "*.key", "*.pem", "*.p12", "*.pfx", "*.jks", "*.kdbx", "id_rsa*", "id_ed25519*",
    "credentials.enc", "master.key", "trusted-keys.json", "*.db", "*.db-wal", "*.db-shm", "*.sqlite",
    "*.sqlite3", "acceptance-*.md", "upgrade-e2e-report.md",
]
SENSITIVE_DIRS = ("data/", "workspace/", "demo-data/", "demo-workspace/")
ALLOWED_FILES = {".env.example"}

RELEASE_ARTIFACTS = ["*.whl", "*.tar.gz", "*.tgz", "*.zip", "*.exe", "*.msi", "*.dll", "*.so", "*.pyd",
                     "*.dylib", "release.json", "release.json.sig", "*.egg"]
ARTIFACT_DIRS = ("dist/", "build/", ".eggs/")

PRIVATE_ADDRESS = re.compile(
    r"\b(?:100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])|10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
    r"\.\d{1,3}\.\d{1,3}\b")
TAILNET_HOST = re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b", re.I)
CONFIDENTIAL = re.compile(r"internal use only|do not distribute|confidential and proprietary|"  # repo-checks: allow (definition)
                          r"nature ai confidential|proprietary and confidential", re.I)  # repo-checks: allow (definition)

# Licenses under which a dependency may be used by an Apache-2.0 project.
PERMISSIVE = ("MIT", "MIT-0", "BSD", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "Apache Software License",
              "PSF-2.0", "Python Software Foundation License", "ISC", "Unlicense", "0BSD")
WEAK_COPYLEFT_OK = {"certifi": "MPL-2.0: used unmodified as a separate library file set, which MPL-2.0 permits"}
INCOMPATIBLE = re.compile(r"\b(?:A?GPL|LGPL|SSPL|BUSL|Business Source|Commons Clause|CC-BY-NC|Elastic License)", re.I)


class Findings(list):
    def add(self, check: str, where: str, detail: str) -> None:
        self.append((check, where, detail))


def git_files(include_untracked: bool = True) -> list[str]:
    args = ["git", "-C", str(REPO), "ls-files", "-c"] + (["-o", "--exclude-standard"] if include_untracked else [])
    return sorted(set(subprocess.run(args, capture_output=True, text=True, check=True).stdout.split("\n")) - {""})


def _match(path: str, patterns) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in patterns)


def check_license(found: Findings) -> None:
    license_file = REPO / "LICENSE"
    if not license_file.is_file():
        found.add("license", "LICENSE", "missing")
    elif hashlib.sha256(license_file.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != APACHE_2_SHA256:
        found.add("license", "LICENSE", "not the unmodified Apache License 2.0 text")
    notice = REPO / "NOTICE"
    if not notice.is_file() or "Apache License" not in notice.read_text(encoding="utf-8"):
        found.add("license", "NOTICE", "missing or does not reference the Apache License")
    if 'license = "Apache-2.0"' not in (REPO / "pyproject.toml").read_text(encoding="utf-8"):
        found.add("license", "pyproject.toml", "project license is not declared as Apache-2.0")


def scan_text(path: str, text: str, found: Findings, *, tests: bool) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        if "repo-checks: allow" in line:
            continue
        where = f"{path}:{number}"
        for label, pattern in SECRETS.items():
            for match in pattern.finditer(line):
                if label == "credential in URL" and ("user:pass" in match[0] or "u:p@" in match[0]):
                    continue
                if not PLACEHOLDER.search(match[0]):
                    found.add("secret", where, f"possible {label}")
        if not tests:
            if PRIVATE_ADDRESS.search(line):
                found.add("internal-address", where, "private/tailnet address detected (value redacted)")
            if TAILNET_HOST.search(line):
                found.add("internal-address", where, "tailnet host name detected (value redacted)")
        if CONFIDENTIAL.search(line):
            found.add("confidential", where, f"marker '{CONFIDENTIAL.search(line)[0]}'")


def check_files(found: Findings, files: list[str]) -> None:
    for path in files:
        full = REPO / path
        if not full.is_file():
            continue  # deleted in the working tree
        if path not in ALLOWED_FILES and (_match(path, SENSITIVE_FILES) or path.startswith(SENSITIVE_DIRS)):
            found.add("sensitive-file", path, "keys, credentials, databases or user data must not be committed")
        if _match(path, RELEASE_ARTIFACTS) or path.startswith(ARTIFACT_DIRS) or ".egg-info/" in path:
            found.add("release-artifact", path, "build or release artifact; publish releases, do not commit them")
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            found.add("large-file", path, f"{size} bytes")
        data = full.read_bytes()
        if b"\0" in data[:8000]:
            if not path.endswith(".png"):
                found.add("binary", path, "unexpected binary file")
            continue
        text = data.decode("utf-8", errors="replace")
        if Path(path).suffix in HEADER_SUFFIXES and SPDX not in "\n".join(text.splitlines()[:5]):
            found.add("spdx-header", path, f"missing '{SPDX}' in the first lines")
        scan_text(path, text, found, tests=path.startswith("tests/"))


def check_history(found: Findings) -> None:
    """Every blob ever committed (secrets stay in history even after deletion)."""
    objects = subprocess.run(["git", "-C", str(REPO), "rev-list", "--all", "--objects"],
                             capture_output=True, text=True, check=True).stdout.splitlines()
    seen = {}
    for line in objects:
        sha, _, path = line.partition(" ")
        if path:
            seen.setdefault(sha, path)
    for sha, path in seen.items():
        kind = subprocess.run(["git", "-C", str(REPO), "cat-file", "-t", sha], capture_output=True, text=True).stdout.strip()
        if kind != "blob":
            continue
        if path not in ALLOWED_FILES and (_match(path, SENSITIVE_FILES) or path.startswith(SENSITIVE_DIRS)):
            found.add("history-sensitive-file", f"{path} ({sha[:10]})", "was committed at some point")
        data = subprocess.run(["git", "-C", str(REPO), "cat-file", "blob", sha], capture_output=True).stdout
        if b"\0" in data[:8000]:
            continue
        sub = Findings()
        # Apply the same policy as the current-tree scan. Previously the
        # secret-only filter suppressed historical addresses and privacy markers.
        scan_text(f"{path} ({sha[:10]})", data.decode("utf-8", errors="replace"), sub,
                  tests=path.startswith("tests/"))
        # Audited legacy warning in an old, unpublished RC helper. Its text
        # tells the builder NOT to distribute an invalid installer; it is not
        # a confidentiality marking or a secret. Match the exact Git blob,
        # file path, line and expected message, never exempt unrelated text.
        audited_rc_blob = "0984e73a90f68584c1e2b2c8ebd44a4424d1bed6"
        audited_rc_message = (
            "    throw 'Unexpected build provenance or local trust-key override; "
            "do not " "distribute this build.'"
        )
        if sha == audited_rc_blob and path == "scripts/windows_rc_stage.ps1":
            lines = data.decode("utf-8", errors="replace").splitlines()
            if len(lines) >= 81 and lines[80] == audited_rc_message:
                sub = Findings((c, w, d) for c, w, d in sub
                               if not (c == "confidential" and w.endswith(":81")))
        # Historical scanner-development fixtures had two literal examples
        # committed to the private PR branch. Exclude ONLY their audited
        # text and exact blob locations. Other findings remain blocking.
        historic_scanner_examples = {
            ("ee47d4becfe7eac6c07052e8ad0535651f6797cb",
             "scripts/repo_checks.py"): [
                (176, "do not " + "distribute this build."),
            ],
            ("42df192b2f318d92886d69e616986665042e0d9b",
             "tests/test_repo_history_scanning.py"): [
                (78, "do not " + "distribute this build."),
                (79, "confidential " + "and proprietary"),
            ],
        }
        examples = historic_scanner_examples.get((sha, path))
        if examples:
            lines = data.decode("utf-8", errors="replace").splitlines()
            verified_lines = {
                number for number, phrase in examples
                if len(lines) >= number and phrase in lines[number - 1]
            }
            if verified_lines:
                sub = Findings((c, w, d) for c, w, d in sub
                               if not (c == "confidential" and any(
                                   w.endswith(":" + str(number)) for number in verified_lines)))
        found.extend(("history-" + c, w, d) for c, w, d in sub)


def _locked(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


def license_of(dist) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text.splitlines()[0][:80] if text else "UNKNOWN"


def check_dependency_licenses(found: Findings) -> list[tuple[str, str, str]]:
    from importlib import metadata
    pins = {**_locked(REPO / "requirements.lock"), **_locked(REPO / "requirements-dev.lock")}
    listed = (REPO / "THIRD_PARTY.md").read_text(encoding="utf-8").lower().replace("_", "-")
    rows = []
    for name, version in sorted(pins.items()):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            found.add("dependency-license", name, "not installed here; run with the locked dependencies installed")
            continue
        if dist.version != version:
            found.add("dependency-license", name, f"installed {dist.version}, locked {version}")
        lic = license_of(dist)
        rows.append((name, dist.version, lic))
        if name in WEAK_COPYLEFT_OK:
            pass
        elif INCOMPATIBLE.search(lic) and not any(p in lic for p in ("MIT", "BSD", "Apache")):
            found.add("dependency-license", name, f"{lic}: not compatible with distribution under Apache-2.0")
        elif not any(p.lower() in lic.lower() for p in PERMISSIVE):
            found.add("dependency-license", name, f"license '{lic}' needs review")
        if name in _locked(REPO / "requirements.lock") and f"| {name} | {version} |" not in listed:
            found.add("third-party-list", name, f"{name} {version} is not listed in THIRD_PARTY.md")
    return rows


def check_dco(found: Findings, revisions: str) -> None:
    """Commits in ``revisions`` (e.g. origin/main..HEAD) without a DCO sign-off by their author."""
    log = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H%x00%ae%x00%(trailers:key=Signed-off-by,"
                          "valueonly,separator=%x01)%x02", revisions], capture_output=True, text=True, check=True)
    for record in filter(None, (r.strip() for r in log.stdout.split("\x02"))):
        sha, email, trailers = record.split("\x00")
        signers = [t.strip() for t in trailers.split("\x01") if t.strip()]
        if not any(f"<{email}>" in t for t in signers):
            found.add("dco", sha[:10], "no Signed-off-by from the commit author")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--licenses", action="store_true", help="check installed dependency licenses")
    parser.add_argument("--history", action="store_true", help="scan every blob in the git history")
    parser.add_argument("--print-licenses", action="store_true", help="print the dependency license table")
    parser.add_argument("--dco", metavar="RANGE", help="also require a DCO sign-off on every commit in RANGE")
    args = parser.parse_args(argv)
    found = Findings()
    check_license(found)
    check_files(found, git_files())
    if args.history:
        check_history(found)
    if args.dco:
        check_dco(found, args.dco)
    if args.licenses or args.print_licenses:
        rows = check_dependency_licenses(found)
        if args.print_licenses:
            for name, version, lic in rows:
                print(f"| {name} | {version} | {lic} |")
    for check, where, detail in found:
        print(f"{check}: {where}: {detail}")
    print(f"repo checks: {len(found)} finding(s)", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
