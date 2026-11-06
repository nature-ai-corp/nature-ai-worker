# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The upgrade transaction.

Order of an upgrade (each phase is journaled in ``upgrade/state.json``
before it starts, so an interruption at any point can be recovered)::

    verify bundle -> eligibility -> user authorization
    installing   new version built in versions/<new> (the active version is untouched)
    stopping     data root marked "upgrade in progress"; the running Worker drains and exits
    backing_up   copy of the data root in <data>/backups/upgrade-<id>
    switching    `current` -> new version (one atomic replace)
    migrating    the NEW code migrates the data
    verifying    the new version starts and answers; installation mode unchanged
    complete     marker removed; history records success

A failure before ``switching`` changes nothing. A failure from ``switching``
on rolls back in the same run: the pointer returns to the previous version
and, if migrations ran, the data root is restored from this upgrade's own
backup (the data being replaced is moved aside, never deleted). That backup
was taken seconds earlier from the previous version with the Worker stopped,
so it is exactly the data that version expects. If the rollback itself fails,
the installation is left in ``repair_required`` and the Worker refuses to
start until ``recover`` succeeds: nothing is ever silently restored, and old
code never runs on migrated data.
"""
from __future__ import annotations

import secrets
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..migrations import BACKUPS_DIR
from .layout import Layout, LayoutError
from .release import ReleaseError, TrustedKey, VerifiedRelease, Version, stage_bundle, trusted_keys
from .toolkit import BaseToolkit, StopRefused, ToolkitError

DATA_CHANGING = ("migrating", "verifying")
UNFINISHED = ("installing", "stopping", "backing_up", "switching", "migrating", "verifying",
              "rolling_back", "repair_required")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Plan:
    from_version: str
    release: VerifiedRelease
    checks: list[Check]
    edition: str

    @property
    def to_version(self) -> str:
        return str(self.release.manifest.version)

    @property
    def eligible(self) -> bool:
        return all(c.ok for c in self.checks)

    def describe(self) -> dict:
        m = self.release.manifest
        return {
            "edition": self.edition, "installed": self.from_version, "version": self.to_version,
            "released_at": m.released_at, "channel": m.channel, "official": self.release.official,
            "signer": {"key_id": self.release.signer.key_id, "scope": self.release.signer.scope},
            "source": m.source, "eligible": self.eligible,
            "checks": [{"check": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "reasons": [c.detail for c in self.checks if not c.ok],
            "notes": self.release.notes,
        }


@dataclass
class Outcome:
    outcome: str  # succeeded | ineligible | declined | aborted | failed | rolled_back | repair_required | recovered | nothing_to_do
    message: str
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in ("succeeded", "recovered", "nothing_to_do", "rolled_back_by_user")


class UpgradeEngine:
    def __init__(self, layout: Layout, toolkit: BaseToolkit, *, keys: list[TrustedKey] | None = None,
                 drain_timeout: float = 600, progress: Callable[[str], None] = lambda _m: None):
        self.layout, self.toolkit = layout, toolkit
        self.inst = layout.installation()
        self.keys = keys if keys is not None else trusted_keys(layout.local_keys)
        self.drain_timeout = drain_timeout
        self.progress = progress

    # -- eligibility ----------------------------------------------------------------

    def stage(self, source: str) -> VerifiedRelease:
        staging = self.layout.upgrade / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        return stage_bundle(source, staging / uuid.uuid4().hex, self.keys)

    def plan(self, release: VerifiedRelease) -> Plan:
        m = release.manifest
        current = self.layout.current()
        installed = Version.parse(current)
        edition = self.inst.edition
        checks = [Check("authentic", True, f"signature verified with key {release.signer.key_id} "
                        f"({'official NATURE AI release key' if release.official else 'locally trusted key, NOT an official release'})"),
                  Check("integrity", True, f"{len(m.artifacts) + 1 + bool(m.runtime)} files match their SHA-256")]
        checks.append(Check("newer", m.version > installed,
                            f"{m.version} is newer than {current}" if m.version > installed
                            else f"{m.version} is not newer than the installed {current}"))
        checks.append(Check("upgrade_path", installed >= m.min_upgrade_from,
                            f"upgrades from {m.min_upgrade_from} or later" if installed >= m.min_upgrade_from
                            else f"{m.version} cannot upgrade {current} directly (needs {m.min_upgrade_from} or later)"))
        checks.append(Check("edition", edition in m.editions,
                            f"supports {edition}" if edition in m.editions else f"this release is not for {edition}"))
        host = self.toolkit.host_target()
        same = all(m.target.get(k) == host[k] for k in ("os", "arch", "python"))
        checks.append(Check("platform", same, f"built for {m.target['os']}/{m.target['arch']} Python "
                            f"{m.target['python']}; this computer is {host['os']}/{host['arch']} Python {host['python']}"))
        mode = self.toolkit.data_mode()
        checks.append(Check("installation_mode", mode in (edition, None if edition == "standalone" else edition),
                            f"data root mode is {mode or 'standalone (unmarked v0.1)'}; the upgrade never changes it"))
        try:
            data = self.toolkit.data_schema(current)
            target = m.data_schema.get(edition)
            ok = target is not None and (data is None or target >= data)
            checks.append(Check("data_schema", ok, f"data schema {data} -> {target}" if ok else
                                f"release supports data schema {target}, but the data is at {data}"))
        except ToolkitError as exc:
            checks.append(Check("data_schema", False, f"cannot read the data schema: {exc}"))
        if edition == "platform_client":
            try:
                interface = self.toolkit.client_interface(current)
                ok = m.client_interface == interface
                checks.append(Check("client_interface", ok, f"Platform Client Interface {interface}" if ok else
                                    f"release needs Client Interface {m.client_interface}, the Platform contract "
                                    f"in use is {interface}; upgrade only when the Platform supports it"))
            except ToolkitError as exc:
                checks.append(Check("client_interface", False, str(exc)))
        for name, ok, detail in self.toolkit.check_permissions():
            checks.append(Check(name, ok, detail))
        try:
            ok, detail = self.toolkit.backup_space()
        except OSError as exc:
            ok, detail = False, f"cannot measure the data root: {exc}"
        checks.append(Check("disk_space", ok, detail))
        state = self.layout.state()
        unfinished = state is not None and state.get("phase") in UNFINISHED
        checks.append(Check("no_unfinished_upgrade", not unfinished,
                            "no unfinished upgrade" if not unfinished else
                            f"an upgrade stopped in phase {state.get('phase')}: run `nature-worker-upgrade recover`"))
        return Plan(current, release, checks, edition)

    def check(self, source: str) -> Plan:
        """Verify and evaluate a release without changing anything but staging."""
        lock = self.layout.lock()
        try:
            plan = self.plan(self.stage(source))
            self._cache_available(plan)
            return plan
        finally:
            shutil.rmtree(self.layout.upgrade / "staging", ignore_errors=True)
            lock.release()

    def _cache_available(self, plan: Plan) -> None:
        from .layout import write_json_atomic
        info = plan.describe()
        info["checked_at"] = time.time()
        info["notes"] = info["notes"][:4000]
        write_json_atomic(self.layout.upgrade / "available.json", info,
                          0o644 if self.inst.edition == "platform_client" else 0o600)

    # -- the upgrade ----------------------------------------------------------------

    def upgrade(self, source: str, authorize: Callable[[Plan], bool]) -> Outcome:
        lock = self.layout.lock()
        try:
            try:
                release = self.stage(source)
            except ReleaseError as exc:
                self.layout.record({"outcome": "refused", "source_kind": _kind(source), "reason": str(exc)})
                return Outcome("ineligible", f"Refused: {exc}")
            plan = self.plan(release)
            self._cache_available(plan)
            base = {"from": plan.from_version, "to": plan.to_version, "edition": plan.edition,
                    "manifest_sha256": release.manifest.digest, "signer_key_id": release.signer.key_id,
                    "signer_scope": release.signer.scope}
            if not plan.eligible:
                self.layout.record({**base, "outcome": "ineligible", "reasons": plan.describe()["reasons"]})
                return Outcome("ineligible", "Not eligible: " + "; ".join(plan.describe()["reasons"]), plan.describe())
            if authorize(plan) is not True:
                self.layout.record({**base, "outcome": "declined"})
                return Outcome("declined", "The upgrade was not authorized. Nothing was changed.")
            return self._run(plan, base)
        finally:
            shutil.rmtree(self.layout.upgrade / "staging", ignore_errors=True)
            lock.release()

    def _run(self, plan: Plan, base: dict) -> Outcome:
        upgrade_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        token = secrets.token_urlsafe(32)
        state = {**base, "id": upgrade_id, "phase": "installing", "backup": None,
                 "was_running": None, "started_at": time.time()}
        self.layout.save_state(state)
        try:
            self.toolkit.install_version(plan.release)
        except (ToolkitError, LayoutError, ReleaseError, OSError) as exc:
            self.toolkit.remove_incomplete(plan.to_version)
            return self._end(state, "failed", f"Installing {plan.to_version} failed; nothing else was "
                             f"changed and {plan.from_version} stays active. {exc}")

        self._phase(state, "stopping")
        self.toolkit.write_marker({"from_version": plan.from_version, "to_version": plan.to_version,
                                   "upgrade_id": upgrade_id}, token)
        state["was_running"] = self.toolkit.is_running()
        self.layout.save_state(state)
        try:
            self.toolkit.stop(self.drain_timeout)
            self.toolkit.ensure_stopped()
        except (StopRefused, ToolkitError) as exc:
            self.toolkit.remove_marker()
            return self._end(state, "aborted", str(exc))

        mode_before = self.toolkit.mode_fingerprint()
        try:
            self._phase(state, "backing_up")
            backup = self.inst.data_dir / BACKUPS_DIR / f"upgrade-{upgrade_id}"
            state["backup"] = str(backup)
            self.layout.save_state(state)
            self.toolkit.backup(backup)
            self._phase(state, "switching")
            self.layout.set_current(plan.to_version)
            self._phase(state, "migrating")
            self.toolkit.migrate(plan.to_version)
            self._phase(state, "verifying")
            self.toolkit.health(plan.to_version, token)
            if self.toolkit.mode_fingerprint() != mode_before:
                raise ToolkitError("The installation mode changed during the upgrade.")
        except Exception as exc:  # noqa: BLE001 - every failure here must roll back
            return self._roll_back(state, token, f"{type(exc).__name__}: {exc}")
        return self._finish(state)

    def _finish(self, state: dict) -> Outcome:
        self.toolkit.remove_marker()
        message = f"Upgraded from {state['from']} to {state['to']}."
        try:
            # Keep this upgrade's backup (a user rollback may need it) and the newest others.
            removed = self.toolkit.prune_backups((Path(state["backup"]),) if state.get("backup") else ())
            if removed:
                message += f" Removed {len(removed)} older data backup(s); the newest are kept."
        except OSError as exc:
            message += f" Older data backups could not be removed: {exc}"
        try:
            if state.get("was_running"):
                message += " " + self.toolkit.start()
            else:
                self.toolkit.ensure_stopped()
        except ToolkitError as exc:
            message += f" The upgrade is complete, but restarting failed: {exc}"
        return self._end(state, "succeeded", message, backup=state.get("backup"))

    def _roll_back(self, state: dict, token: str, error: str) -> Outcome:
        failed_phase = state["phase"]
        self.progress(f"Upgrade failed during {failed_phase}: {error}. Rolling back.")
        state["error"], state["failed_phase"] = error, failed_phase
        self._phase(state, "rolling_back")
        try:
            self.toolkit.write_marker({"from_version": state["from"], "to_version": state["to"],
                                       "upgrade_id": state["id"]}, token)
            self.toolkit.ensure_stopped()
            self.layout.set_current(state["from"])
            restored = None
            partial = Path(state["backup"]) if state.get("backup") else None
            if failed_phase not in DATA_CHANGING and partial and partial.is_dir() \
                    and not (partial / "BACKUP.json").exists():
                shutil.rmtree(partial)  # an interrupted copy, never usable for a restore
            if failed_phase in DATA_CHANGING:
                restored = self._aside(f"failed-upgrade-{state['id']}")
                self.toolkit.restore(Path(state["backup"]), restored)
            self.toolkit.health(state["from"], token)
            self.toolkit.remove_marker()
        except Exception as exc:  # noqa: BLE001
            state["rollback_error"] = f"{type(exc).__name__}: {exc}"
            state["phase"] = "repair_required"
            self.layout.save_state(state)
            self.layout.record({**_public(state), "outcome": "repair_required"})
            return Outcome("repair_required",
                           f"The upgrade failed ({error}) and the automatic rollback also failed "
                           f"({state['rollback_error']}). Nature AI Worker will not start until "
                           "`nature-worker-upgrade recover` succeeds. Your data backup is "
                           f"{state.get('backup')}.")
        message = (f"The upgrade to {state['to']} failed during {failed_phase} ({error}). "
                   f"{state['from']} is active again"
                   + (" and the data was restored from this upgrade's backup; the data it replaced "
                      f"is kept in {restored}." if restored else "; the data was not changed."))
        try:
            if state.get("was_running"):
                message += " " + self.toolkit.start()
            else:
                self.toolkit.ensure_stopped()
        except ToolkitError as exc:
            message += f" Restarting failed: {exc}"
        return self._end(state, "rolled_back", message)

    def _aside(self, name: str) -> Path:
        """A new directory name for data being replaced (kept, never deleted)."""
        return self.inst.data_dir / BACKUPS_DIR / f"{name}-{uuid.uuid4().hex[:4]}"

    def _phase(self, state: dict, phase: str) -> None:
        state["phase"] = phase
        self.layout.save_state(state)
        self.progress(f"[{phase}]")

    def _end(self, state: dict, outcome: str, message: str, **extra) -> Outcome:
        self.layout.record({**_public(state), **extra, "outcome": outcome, "message": message})
        self.layout.clear_state()
        return Outcome(outcome, message, extra)

    # -- recovery and rollback ------------------------------------------------------------

    def recover(self, confirm: Callable[[str], bool]) -> Outcome:
        """Finish or undo an upgrade that was interrupted (crash, power loss, kill)."""
        lock = self.layout.lock()
        try:
            state = self.layout.state()
            if state is None or state.get("phase") == "complete":
                from ..maintenance import read_upgrade_marker
                if read_upgrade_marker(self.inst.data_dir) is not None:
                    if not confirm("An upgrade marker exists but no upgrade is recorded as running. "
                                   "Remove the marker so the Worker can start?"):
                        return Outcome("declined", "Nothing was changed.")
                    self.toolkit.remove_marker()
                    return Outcome("recovered", "Removed a stale upgrade marker.")
                return Outcome("nothing_to_do", "No interrupted upgrade.")
            phase, token = state["phase"], secrets.token_urlsafe(32)
            if state.get("kind") == "rollback":
                return self._resume_rollback(state, token, confirm)
            if phase == "installing":
                self.toolkit.remove_incomplete(state["to"])
                return self._end(state, "recovered", f"The interrupted installation of {state['to']} was removed. "
                                 f"{state['from']} was never stopped and stays active.")
            if phase == "stopping":
                from ..maintenance import withdraw_stop
                withdraw_stop(self.inst.data_dir)
                self.toolkit.remove_marker()
                return self._end(state, "recovered", f"Nothing was changed; {state['from']} stays active.")
            if phase == "verifying":
                # Data was fully migrated by the new code: check whether it simply works.
                if confirm(f"The upgrade to {state['to']} was interrupted while being verified. "
                           f"Verify {state['to']} again now (it is kept if it works, rolled back if not)?"):
                    try:
                        self.toolkit.ensure_stopped()
                        self.toolkit.write_marker({"from_version": state["from"], "to_version": state["to"],
                                                   "upgrade_id": state["id"]}, token)
                        self.layout.set_current(state["to"])
                        self.toolkit.health(state["to"], token)
                        return self._finish(state)
                    except Exception as exc:  # noqa: BLE001
                        return self._roll_back(state, token, f"verification after interruption: {exc}")
                return Outcome("declined", "Nothing was changed; the Worker stays blocked until recovery.")
            prompt = (f"The upgrade from {state['from']} to {state['to']} stopped in phase '{phase}'. "
                      f"Recovery activates {state['from']} again"
                      + (" and restores the data root from the backup taken at the start of this upgrade "
                         f"({state.get('backup')}). The current data root is moved aside, not deleted."
                         if phase in DATA_CHANGING or phase in ("rolling_back", "repair_required")
                         and state.get("failed_phase") in DATA_CHANGING else ". The data was not changed.")
                      + " Continue?")
            if not confirm(prompt):
                return Outcome("declined", "Nothing was changed; the Worker stays blocked until recovery.")
            if phase in ("rolling_back", "repair_required"):
                phase = state.get("failed_phase", phase)
            state["phase"] = phase
            return self._roll_back(state, token, state.get("error") or f"interrupted during {phase}")
        finally:
            lock.release()

    def rollback(self, confirm: Callable[[str], bool]) -> Outcome:
        """Return to the version before the last successful upgrade, by explicit request."""
        lock = self.layout.lock()
        try:
            if self.layout.state() is not None:
                return Outcome("failed", "An upgrade is unfinished: run `nature-worker-upgrade recover` first.")
            current = self.layout.current()
            last = next((h for h in reversed(self.layout.history())
                         if h.get("outcome") == "succeeded" and h.get("to") == current), None)
            if last is None:
                return Outcome("failed", f"No recorded upgrade to {current} to roll back.")
            previous = last["from"]
            if not self.layout.complete(previous):
                return Outcome("failed", f"{previous} is no longer installed; rolling back is not possible. "
                               "Install a release of that version instead.")
            data, supported = self.toolkit.data_schema(current), self.toolkit.code_schema(previous)
            restore = data is not None and data > supported
            backup = Path(last.get("backup") or "")
            if restore and not (backup / "BACKUP.json").is_file():
                return Outcome("failed", f"The data was migrated to schema {data}, which {previous} cannot "
                               f"read, and the backup from that upgrade ({backup}) is missing. "
                               "Rollback refused: it would need data that does not exist.")
            if restore:
                prompt = (f"Roll back from {current} to {previous}? The data was migrated to schema {data}, "
                          f"which {previous} cannot read, so the data root is restored from the backup taken "
                          f"before the upgrade ({backup}). Work done since then is moved aside to "
                          f"{self.inst.data_dir / BACKUPS_DIR}/rolled-back-..., not deleted, but "
                          f"{previous} will not show it.")
            else:
                prompt = (f"Roll back from {current} to {previous}? The data stays as it is "
                          f"(schema {data}, readable by {previous}).")
            if not confirm(prompt):
                return Outcome("declined", "Nothing was changed.")
            token = secrets.token_urlsafe(32)
            rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            state = {"id": rid, "from": current, "to": previous, "edition": self.inst.edition,
                     "kind": "rollback", "phase": "stopping", "backup": str(backup) if restore else None}
            self.layout.save_state(state)
            self.toolkit.write_marker({"from_version": current, "to_version": previous, "upgrade_id": rid}, token)
            state["was_running"] = self.toolkit.is_running()
            try:
                self.toolkit.stop(self.drain_timeout)
                self.toolkit.ensure_stopped()
            except (StopRefused, ToolkitError) as exc:
                self.toolkit.remove_marker()
                return self._end(state, "aborted", str(exc))
            self.layout.save_state(state)
            return self._complete_rollback(state, token)
        finally:
            lock.release()

    def _complete_rollback(self, state: dict, token: str) -> Outcome:
        """Switch to ``state['to']`` (the older version), restore its backup if needed, verify.

        Every step is safe to repeat, so ``recover`` resumes an interrupted rollback here.
        """
        try:
            self._phase(state, "switching")
            self.toolkit.ensure_stopped()
            self.layout.set_current(state["to"])
            if state.get("backup"):
                self._phase(state, "migrating")  # the data root changes from here on
                self.toolkit.restore(Path(state["backup"]), self._aside(f"rolled-back-{state['id']}"))
            self._phase(state, "verifying")
            self.toolkit.health(state["to"], token)
        except Exception as exc:  # noqa: BLE001
            state["phase"] = "repair_required"
            state["error"] = f"{type(exc).__name__}: {exc}"
            self.layout.save_state(state)
            self.layout.record({**_public(state), "outcome": "repair_required"})
            return Outcome("repair_required", f"Rollback failed: {state['error']}. Nature AI Worker stays "
                           "blocked; run `nature-worker-upgrade recover` to retry.")
        self.toolkit.remove_marker()
        message = f"Rolled back from {state['from']} to {state['to']}."
        try:
            if state.get("was_running"):
                message += " " + self.toolkit.start()
            else:
                self.toolkit.ensure_stopped()
        except ToolkitError as exc:
            message += f" Restarting failed: {exc}"
        return self._end(state, "rolled_back_by_user", message)

    def _resume_rollback(self, state: dict, token: str, confirm: Callable[[str], bool]) -> Outcome:
        if state["phase"] == "stopping":
            from ..maintenance import withdraw_stop
            withdraw_stop(self.inst.data_dir)
            self.toolkit.remove_marker()
            return self._end(state, "recovered", f"The rollback was interrupted before anything changed; "
                             f"{state['from']} stays active.")
        if not confirm(f"A rollback from {state['from']} to {state['to']} was interrupted in phase "
                       f"'{state['phase']}'. Finish it now"
                       + (f" (the data root is restored again from {state['backup']}; the current data root "
                          "is moved aside, not deleted)" if state.get("backup") else "") + "?"):
            return Outcome("declined", "Nothing was changed; the Worker stays blocked until recovery.")
        self.toolkit.write_marker({"from_version": state["from"], "to_version": state["to"],
                                   "upgrade_id": state["id"]}, token)
        return self._complete_rollback(state, token)


def _public(state: dict) -> dict:
    """The journal fields that go into the history (no tokens; tokens are never journaled)."""
    keys = ("id", "kind", "from", "to", "edition", "phase", "failed_phase", "error", "rollback_error",
            "manifest_sha256", "signer_key_id", "signer_scope", "backup", "started_at")
    return {k: state[k] for k in keys if state.get(k) is not None}


def _kind(source: str) -> str:
    return "https" if source.startswith("https://") else "directory"
