# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Prebuilt GitHub Community Agent: optional profile on the existing Worker runtime."""

from pathlib import Path

from nature_worker.templates import load_templates


ROOT = Path(__file__).resolve().parents[1] / "src" / "nature_worker"


def test_community_profile_is_shipped_and_enabled():
    profiles = load_templates()
    community = next(t for t in profiles if t["id"] == "github-community")
    assert community["name"] == "GitHub Community Agent"
    assert "configured" in community["instruction"]
    assert "approval" in community["instruction"].lower()
    assert "untrusted" in community["instruction"].lower()
    assert "English" in community["instruction"]
    assert profiles[0]["id"] == "general"  # current first-run experience stays General Work


def test_community_starters_use_existing_new_work_and_no_direct_post():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'href="#/community"' in html
    assert 'id="view-community"' in html
    for task in ("issues", "discussions", "drafts", "report"):
        assert f'data-community-task="{task}"' in html
    assert 'go("#/new")' in javascript
    assert 't.id === "github-community"' in javascript
    assert 'api("POST", "/api/github/' not in javascript  # no direct posting from the profile launcher
