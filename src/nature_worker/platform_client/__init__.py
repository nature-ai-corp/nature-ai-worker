# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Nature AI Worker Platform Client (S-C mode): a thin client of the Platform.

Runs as its own process, under its own OS account and data root. It has no
local Agent, model provider, tool or Standalone data access: every Task,
Agent run, authorization and approval is the Platform's.
"""
