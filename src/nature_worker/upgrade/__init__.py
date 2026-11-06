# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Verified, transactional upgrades of a managed Worker installation.

- ``release``: release identity: versions, trusted keys, signed manifests and
  bundle verification.
- ``layout``: the managed installation on disk (one immutable directory per
  version, an atomic ``current`` pointer, journal and history).
- ``engine``: eligibility, authorization and the upgrade transaction with
  rollback and recovery.
- ``toolkit``: the OS operations the engine uses (pip, processes, services).

Run ``nature-worker-upgrade --help`` (``python -m nature_worker.upgrade``).
See docs/UPGRADES.md.
"""
