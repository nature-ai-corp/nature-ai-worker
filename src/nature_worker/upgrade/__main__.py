# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""``nature-worker-upgrade``: show, verify, install and undo Worker upgrades.

    status      installed version, last upgrade, unfinished upgrade (and, with
                --bundle, the available release, its notes and eligibility)
    check       verify a release bundle and report eligibility; changes nothing
    upgrade     verify, ask for authorization, upgrade (rolls back on failure)
    recover     finish or undo an interrupted upgrade
    rollback    return to the version before the last upgrade
    history     upgrade outcomes recorded for this installation
    install     create a managed Standalone installation from a verified bundle
    uninstall-check   whether the code of this Standalone installation may be removed now
    remove-data       delete this installation's data (and, if asked, its workspace) for good

Exit codes: 0 done, 1 failed (see message), 2 not eligible / not authorized /
nothing changed, 3 usage or installation error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .. import __version__
from ..installation_mode import PLATFORM_CLIENT, read_mode
from ..maintenance import worker_running
from ..migrations import BACKUPS_DIR, backup_data_root, data_schema
from .engine import Outcome, Plan, UpgradeEngine
from .layout import INCOMPLETE, INSTALLED, Installation, Layout, LayoutError, find_prefix, write_json_atomic
from .release import ReleaseError, load_keys, stage_bundle, trusted_keys
from .toolkit import StandaloneToolkit, ToolkitError, toolkit_for

EXIT = {"succeeded": 0, "recovered": 0, "nothing_to_do": 0, "rolled_back_by_user": 0,
        "failed": 1, "rolled_back": 1, "repair_required": 1,
        "ineligible": 2, "declined": 2, "aborted": 2}


def _say(message: str) -> None:
    print(message, flush=True)


def _ask(question: str) -> str | None:
    """The user's typed answer, or None when there is nobody to ask.

    On Windows the NUL device reports isatty(), so end of input is handled too.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _confirm(prompt: str, assume: bool) -> bool:
    _say(prompt)
    if assume:
        return True
    answer = _ask("Type yes to continue: ")
    if answer is None:
        _say("Not confirmed: no terminal to ask on. Re-run with --confirm after reading the above.")
    return (answer or "").lower() == "yes"


def _show_plan(plan: Plan) -> None:
    d = plan.describe()
    _say(f"Installed:  {d['installed']} ({d['edition']})")
    _say(f"Available:  {d['version']} released {d['released_at']} ({d['channel']})")
    _say("Release:    " + ("OFFICIAL NATURE AI release" if d["official"] else
                           "signed by a locally trusted key - NOT an official NATURE AI release"))
    if d["source"]:
        _say(f"Source:     {d['source'].get('repository', '')} {d['source'].get('tag', '')} "
             f"{d['source'].get('commit', '')}".rstrip())
    _say("Checks:")
    for c in d["checks"]:
        _say(f"  [{'ok' if c['ok'] else 'NO'}] {c['check']}: {c['detail']}")
    _say(f"Eligible:   {'yes' if d['eligible'] else 'no'}")
    _say("--- Release notes " + "-" * 40)
    _say(d["notes"].strip())
    _say("-" * 58)


def _authorize(args) -> callable:
    def authorize(plan: Plan) -> bool:
        _show_plan(plan)
        _say(f"The upgrade stops this Worker once its running work is finished, backs up the data "
             f"root, switches to {plan.to_version}, migrates the data and verifies it. If any step "
             f"fails, {plan.from_version} and its data are restored.")
        if args.confirm_version is not None:
            if args.confirm_version == plan.to_version:
                return True
            _say(f"--confirm-version {args.confirm_version} does not match {plan.to_version}: not authorized.")
            return False
        answer = _ask(f"Type {plan.to_version} to upgrade, or press Enter to cancel: ")
        if answer is None:
            _say(f"Not authorized: no terminal to ask on. Re-run with --confirm-version {plan.to_version}.")
        return answer == plan.to_version
    return authorize


def _engine(args) -> UpgradeEngine:
    prefix = find_prefix(args.prefix)
    if prefix is None:
        raise LayoutError("This is not a managed installation (for example a source checkout with "
                          "`pip install .`). Upgrade it with git and pip; data migrations run with a "
                          "backup when the Worker starts. To create a managed installation, see "
                          "`nature-worker-upgrade install --help`.")
    if PLATFORM_CLIENT == Layout(prefix).installation().edition:
        os.umask(0o022)
    layout = Layout(prefix)
    toolkit = toolkit_for(layout, layout.installation(), progress=_say)
    return UpgradeEngine(layout, toolkit, drain_timeout=args.drain_timeout, progress=_say)


def _source(args, engine: UpgradeEngine) -> str:
    source = args.bundle or engine.inst.release_source
    if not source:
        raise LayoutError("No release source: pass --bundle DIR|https://URL. No official release "
                          "channel is configured for this installation.")
    return str(source)


def cmd_status(args) -> int:
    prefix = find_prefix(args.prefix)
    if prefix is None:
        info = {"managed": False, "running_code_version": __version__,
                "note": "Source or pip installation: upgrade with git/pip. Data migrations run, with a "
                        "backup, when the Worker starts."}
        _say(json.dumps(info, indent=2) if args.json else
             f"Nature AI Worker {__version__} (not a managed installation).\n{info['note']}")
        return 0
    engine = _engine(args)
    layout, inst = engine.layout, engine.inst
    current = layout.current()
    record = layout.installed_record(current)
    history = layout.history()
    state = layout.state()
    info = {
        "managed": True, "prefix": str(layout.prefix), "edition": inst.edition,
        "installed_version": current, "installed_from": {k: record.get(k) for k in
                                                         ("signer_scope", "signer_key_id", "source", "installed_at")},
        "installed_versions": layout.installed_versions(), "data_dir": str(inst.data_dir),
        "data_schema": None, "running": engine.toolkit.is_running(),
        "unfinished_upgrade": {k: state.get(k) for k in ("id", "kind", "from", "to", "phase", "error")} if state else None,
        "last_outcome": history[-1] if history else None,
    }
    try:
        info["data_schema"] = engine.toolkit.data_schema(current)
    except ToolkitError as exc:
        info["data_schema"] = f"unreadable: {exc}"
    plan = None
    if args.bundle or inst.release_source:
        try:
            plan = engine.check(_source(args, engine))
            info["available"] = plan.describe()
        except ReleaseError as exc:
            info["available"] = {"error": str(exc)}
    if args.json:
        _say(json.dumps(info, indent=2, default=str))
        return 0
    _say(f"Nature AI Worker {current} ({inst.edition}) at {layout.prefix}")
    scope = {"official": "official NATURE AI release", "local": "release signed by a locally trusted key (not official)",
             "unsigned-local-build": "unsigned local build (not a release)"}.get(record.get("signer_scope"), "unknown origin")
    _say(f"  installed from: {scope}" + (f", key {record.get('signer_key_id')}" if record.get("signer_key_id") else ""))
    _say(f"  data root: {inst.data_dir} (schema {info['data_schema']}); running: {'yes' if info['running'] else 'no'}")
    _say(f"  installed versions: {', '.join(info['installed_versions'])}")
    if state:
        _say(f"  UNFINISHED {state.get('kind', 'upgrade')} {state.get('from')} -> {state.get('to')} "
             f"in phase {state.get('phase')}: run `nature-worker-upgrade recover`")
    if history:
        h = history[-1]
        _say(f"  last: {h.get('at')} {h.get('outcome')} {h.get('from', '')} -> {h.get('to', '')}")
    if plan is not None:
        _show_plan(plan)
    elif "available" in info:
        _say(f"  available release: {info['available']['error']}")
    else:
        _say("  available release: no release source configured (pass --bundle)")
    return 0


def cmd_check(args) -> int:
    engine = _engine(args)
    try:
        plan = engine.check(_source(args, engine))
    except ReleaseError as exc:
        _say(f"Refused: {exc}")
        return 2
    if args.json:
        _say(json.dumps(plan.describe(), indent=2, default=str))
    else:
        _show_plan(plan)
    return 0 if plan.eligible else 2


def _finish(outcome: Outcome) -> int:
    _say(outcome.message)
    return EXIT.get(outcome.outcome, 1)


def cmd_upgrade(args) -> int:
    engine = _engine(args)
    return _finish(engine.upgrade(_source(args, engine), _authorize(args)))


def cmd_recover(args) -> int:
    outcome = _engine(args).recover(lambda prompt: _confirm(prompt, args.confirm))
    # For recovery, returning to the previous version is the successful result.
    if outcome.outcome == "rolled_back":
        _say(outcome.message)
        return 0
    return _finish(outcome)


def cmd_rollback(args) -> int:
    return _finish(_engine(args).rollback(lambda prompt: _confirm(prompt, args.confirm)))


def cmd_history(args) -> int:
    prefix = find_prefix(args.prefix)
    if prefix is None:
        raise LayoutError("Not a managed installation: there is no upgrade history.")
    entries = Layout(prefix).history()
    if args.json:
        _say(json.dumps(entries, indent=2))
        return 0
    for h in entries:
        _say(f"{h.get('at')}  {h.get('outcome'):<20} {h.get('from', ''):>10} -> {h.get('to', ''):<10} "
             f"{h.get('message') or '; '.join(h.get('reasons', [])) or h.get('reason', '')}")
    if not entries:
        _say("No upgrades recorded.")
    return 0


def _default_prefix() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NatureAIWorker"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "nature-ai-worker"


def platform_client_namespace(path: Path) -> Path | None:
    """The Windows Platform Client (S-C) folder that ``path`` lies in, if any. Compared as text
    only: Standalone never opens, reads or lists Platform Client folders. A separate Standalone
    installation may exist next to the Platform Client (its own user, folders and port); what is
    refused is putting Standalone code, data or workspace inside the Platform Client's folders."""
    if sys.platform != "win32":
        return None
    candidate = os.path.normcase(os.path.abspath(path))
    for var, default in (("ProgramFiles", r"C:\Program Files"), ("ProgramData", r"C:\ProgramData")):
        root = os.path.normcase(os.path.abspath(os.path.join(os.environ.get(var, default), "NatureAIWorker")))
        if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
            return Path(root)
    return None


def cmd_install(args) -> int:
    """Create a managed Standalone installation; adopts existing data in place."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise LayoutError("Install Standalone as the user who will run it, not as root.")
    prefix = (args.prefix or _default_prefix()).absolute()
    data_dir = (args.data_dir or prefix / "data").absolute()
    workspace = (args.workspace or prefix / "workspace").absolute()
    for path in (prefix, data_dir, workspace):
        if platform_client_namespace(path) is not None:
            raise LayoutError(f"{path} is inside the Platform Client's folders ({platform_client_namespace(path)}); "
                              "Standalone never uses them. Choose another folder. Nothing was installed.")
    layout = Layout(prefix)
    if (prefix / "installation.json").exists():
        raise LayoutError(f"{prefix} is already an installation; use `nature-worker-upgrade upgrade`.")
    if data_dir.exists() and read_mode(data_dir) == PLATFORM_CLIENT:
        raise LayoutError(f"{data_dir} belongs to a Platform Client installation; it is never used by Standalone.")
    if worker_running(data_dir):
        raise LayoutError(f"A Worker is running on {data_dir}. Stop it first.")
    keys = trusted_keys()
    local = []
    if args.trust_key:
        local = load_keys(args.trust_key, "local")
        if not local:
            raise LayoutError(f"{args.trust_key} contains no keys.")
        keys += local
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    layout.ensure_upgrade_dir()
    staging = layout.upgrade / "staging"
    try:
        release = stage_bundle(str(args.bundle), staging / "install", keys)
    except ReleaseError as exc:
        _say(f"Refused: {exc}")
        return 2
    m, version = release.manifest, str(release.manifest.version)
    inst = Installation("standalone", data_dir, workspace)
    toolkit = StandaloneToolkit(layout, inst, progress=_say)
    host = toolkit.host_target()
    if "standalone" not in m.editions or any(m.target.get(k) != host[k] for k in host):
        _say(f"Refused: the release is for {m.editions} on {m.target}; this computer is {host}.")
        return 2
    _say(f"Nature AI Worker {version} ({'OFFICIAL NATURE AI release' if release.official else 'locally trusted key, NOT an official release'})")
    _say(f"  installation: {prefix}\n  data root:    {data_dir}" +
         (" (existing data is kept and migrated, after a backup)" if (data_dir / "worker.db").exists() else "") +
         f"\n  workspace:    {workspace}")
    if args.confirm_version != version and _ask(f"Type {version} to install, or press Enter to cancel: ") != version:
        _say("Not authorized; nothing was installed.")
        return 2
    # Everything that can fail happens before the installation is recorded, so
    # a failed install leaves no half-registered installation (re-running is safe).
    toolkit.install_version(release)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data_schema(data_dir) is not None:
        backup = data_dir / BACKUPS_DIR / f"install-{time.strftime('%Y%m%d-%H%M%S')}"
        backup_data_root(data_dir, backup)
        _say(f"Backed up the existing data root to {backup}")
        toolkit.migrate(version)
    toolkit.health(version, token="")
    if local:
        write_json_atomic(layout.local_keys, {"schema": 1, "keys": [
            {"key_id": k.key_id, "public_key": _raw_b64(k), "comment": k.comment} for k in local]}, 0o600)
    layout.write_installation(inst)
    layout.set_current(version)
    launchers = layout.write_launchers(inst)
    layout.record({"outcome": "installed", "to": version, "edition": "standalone",
                   "manifest_sha256": m.digest, "signer_key_id": release.signer.key_id,
                   "signer_scope": release.signer.scope})
    shutil.rmtree(staging, ignore_errors=True)
    _say(f"Installed {version}. Start it with: {launchers[0]}")
    return 0


def _standalone(args) -> tuple[Layout, Installation]:
    prefix = find_prefix(args.prefix)
    if prefix is None:
        raise LayoutError("This is not a managed installation.")
    layout = Layout(prefix)
    inst = layout.installation()
    if inst.edition != "standalone":
        raise LayoutError("This command is for Standalone installations only; the Platform Client has "
                          "its own uninstall.")
    return layout, inst


def cmd_uninstall_check(args) -> int:
    """Exit 0 if the installer may remove the code now. Data and workspace are never touched."""
    layout, inst = _standalone(args)
    problems = []
    if worker_running(inst.data_dir):
        problems.append("Nature AI Worker is running. Close its window (or stop it), then try again.")
    if layout.state():
        problems.append("An upgrade did not finish. Run `nature-worker-upgrade recover` first.")
    for p in problems:
        _say(f"Refused: {p}")
    if problems:
        return 2
    _say(f"Ready to remove the program from {layout.prefix}. Kept: data {inst.data_dir}, "
         f"workspace {inst.workspace_dir}.")
    return 0


def cmd_remove_data(args) -> int:
    """Delete the data root (settings, API keys, history, backups) and, only with
    --include-workspace, the workspace. Asks for the exact folder to be typed back."""
    _layout, inst = _standalone(args)
    data = inst.data_dir
    if read_mode(data) == PLATFORM_CLIENT:
        raise LayoutError(f"{data} belongs to a Platform Client installation; refusing.")
    targets = [p for p in [data, inst.workspace_dir if args.include_workspace else None] if p and p.exists()]
    if not targets:
        _say("Nothing to remove: the data folder does not exist.")
        return 0
    _say("This permanently deletes:")
    for p in targets:
        _say(f"  {p}")
    if not args.include_workspace and inst.workspace_dir:
        _say(f"Kept: your workspace {inst.workspace_dir} (add --include-workspace to delete it too).")
    _say("Settings, saved API keys, conversations, approvals and backups cannot be recovered afterwards.")
    if args.confirm_path is not None:
        ok = Path(args.confirm_path) == data
    else:
        ok = _ask(f"Type the data folder exactly ({data}) to delete, or press Enter to cancel: ") == str(data)
    if not ok:
        _say("Not confirmed; nothing was deleted.")
        return 2
    if worker_running(data):
        _say("Refused: Nature AI Worker is running. Close it, then try again. Nothing was deleted.")
        return 2
    for path in targets:
        # Renamed first: on Windows this fails while any process has a file in it open, so a
        # Worker that started meanwhile is never left with half its data deleted.
        aside = path.with_name(f"{path.name}.removing-{os.getpid()}")
        try:
            path.rename(aside)
        except OSError as exc:
            _say(f"Refused: {path} is in use ({exc.strerror}). Nothing more was deleted.")
            return 1
        shutil.rmtree(aside)
        _say(f"Deleted {path}")
    return 0


def _raw_b64(key) -> str:
    import base64
    from cryptography.hazmat.primitives import serialization
    return base64.b64encode(key.public_key.public_bytes(serialization.Encoding.Raw,
                                                        serialization.PublicFormat.Raw)).decode()


def cmd_register_local_build(args) -> int:
    """Used by scripts/platform-client-linux-install.sh after it built versions/<v>/venv.

    Records an UNSIGNED local build (never reported as an official release),
    writes the installation record and launchers, and activates the version.
    """
    layout = Layout(args.prefix)
    inst = Installation(args.edition, args.data_dir, args.workspace, args.account, args.service, args.port)
    existing = None
    try:
        existing = layout.installation()
    except LayoutError:
        pass
    if existing is not None and (existing.edition, existing.data_dir) != (inst.edition, inst.data_dir):
        raise LayoutError("The installation record names a different edition or data root; refusing.")
    target = layout.version_dir(args.version)
    if not (target / "venv").is_dir():
        raise LayoutError(f"{target}/venv does not exist.")
    (target / INSTALLED).write_text(json.dumps({
        "version": args.version, "signer_scope": "unsigned-local-build", "source": {"commit": args.commit},
        "installed_at": time.time()}, indent=2) + "\n", encoding="utf-8")
    (target / INCOMPLETE).unlink(missing_ok=True)
    layout.write_installation(existing or inst)
    layout.write_launchers(existing or inst)
    previous = None
    try:
        previous = layout.current()
    except LayoutError:
        pass
    layout.set_current(args.version)
    layout.record({"outcome": "local_build_activated", "from": previous, "to": args.version,
                   "edition": inst.edition, "signer_scope": "unsigned-local-build"})
    _say(f"Activated local build {args.version}" + (f" (was {previous})" if previous else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nature-worker-upgrade", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--prefix", type=Path, help="managed installation (default: the one running this command)")
    common.add_argument("--drain-timeout", type=float, default=600,
                        help="seconds to wait for running work to finish before giving up (default 600)")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser

    def sub_add_parser(name, **kwargs):
        return add(name, parents=[common], **kwargs)
    sub.add_parser = sub_add_parser
    p = sub.add_parser("status")
    p.add_argument("--bundle")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--bundle")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("upgrade")
    p.add_argument("--bundle", help="release bundle directory or https:// URL")
    p.add_argument("--confirm-version", help="authorize non-interactively: must equal the release version")
    for name in ("recover", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--confirm", action="store_true", help="answer yes to the confirmation shown")
    p = sub.add_parser("history")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("install")
    p.add_argument("--bundle", required=True, type=Path)
    p.add_argument("--data-dir", type=Path, help="existing or new data root (kept in place)")
    p.add_argument("--workspace", type=Path)
    p.add_argument("--trust-key", type=Path, help="key list (JSON) to trust locally; such releases are never official")
    p.add_argument("--confirm-version")
    sub.add_parser("uninstall-check")
    p = sub.add_parser("remove-data")
    p.add_argument("--include-workspace", action="store_true", help="also delete the workspace (your files)")
    p.add_argument("--confirm-path", help="authorize non-interactively: must equal the data folder")
    p = sub.add_parser("register-local-build", help=argparse.SUPPRESS)
    p.add_argument("--version", required=True)
    p.add_argument("--edition", choices=("standalone", "platform_client"), required=True)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--workspace", type=Path)
    p.add_argument("--account")
    p.add_argument("--service")
    p.add_argument("--port", type=int)
    p.add_argument("--commit", default="")
    args = parser.parse_args(argv)
    if args.command == "register-local-build" and args.prefix is None:
        parser.error("--prefix is required")
    handler = {"status": cmd_status, "check": cmd_check, "upgrade": cmd_upgrade, "recover": cmd_recover,
               "rollback": cmd_rollback, "history": cmd_history, "install": cmd_install,
               "uninstall-check": cmd_uninstall_check, "remove-data": cmd_remove_data,
               "register-local-build": cmd_register_local_build}[args.command]
    try:
        return handler(args)
    except (LayoutError, ToolkitError, ReleaseError, OSError) as exc:
        _say(f"Error: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
