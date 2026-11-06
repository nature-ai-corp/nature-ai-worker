# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""GitHub community settings in a real browser, against the real server process."""

import json

import httpx

from test_work_ui import app  # noqa: F401 - shared browser + server fixture

TOKEN = "fake_readonly_token_for_ui_0123456789"


def _open_settings(b):
    b.click("#open-settings")
    b.wait("document.querySelector('#settings-dialog').open")
    b.eval("document.querySelector('#settings-dialog details').open = true")


def _settings(app):
    return httpx.get(app["base"] + "/api/settings").json()


def test_enable_github_and_store_token_from_settings(app):
    b = app["browser"]
    _open_settings(b)
    assert b.eval("document.querySelector('#s-github').checked") is False
    assert "No token" in b.text("#s-github-token-status")
    assert b.eval("document.querySelector('#s-github-clear-row').hidden") is True
    b.click("#s-github")
    b.eval(f"document.querySelector('#s-github-token').value = {json.dumps(TOKEN)}")
    b.click("#s-save")
    b.wait("!document.querySelector('#settings-dialog').open")
    s = _settings(app)
    assert s["github_enabled"] is True and s["github_token_source"] == "stored"
    assert s["model"] == "fake"  # the model settings were kept

    _open_settings(b)
    assert b.eval("document.querySelector('#s-github').checked") is True
    assert b.eval("document.querySelector('#s-github-token').value") == ""  # never sent back to the page
    assert "A token is stored" in b.text("#s-github-token-status")
    b.click("#s-github-clear")
    b.click("#s-save")
    b.wait("!document.querySelector('#settings-dialog').open")
    assert _settings(app)["github_token_source"] == "none"


def test_invalid_token_is_reported_in_the_dialog(app):
    b = app["browser"]
    _open_settings(b)
    b.eval("document.querySelector('#s-github-token').value = 'not a token'")
    b.click("#s-save")
    b.wait("document.querySelector('#settings-result').textContent.includes('GitHub token')")
    assert b.eval("document.querySelector('#settings-dialog').open") is True
    assert _settings(app)["github_token_source"] == "none"
