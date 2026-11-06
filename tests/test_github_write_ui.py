# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The GitHub comment approval in a real browser.

The real application is served in this process (uvicorn on a thread) so GitHub can be the
counting fake from test_github_write; the model is the local fake server.
"""

import threading
import time

import httpx
import pytest
import uvicorn

from conftest import openai_text, openai_tool_call
from nature_worker.app import create_app
from nature_worker.tools import github_community as gc
from test_github_write import BODY, ISSUE_URL, WRITE_TOKEN, FakeGitHub
from test_process import _free_port
from test_usability import Browser, _find_chrome


@pytest.fixture
def ui(config, fake_model, monkeypatch):
    chrome = _find_chrome()
    if not chrome:
        pytest.skip("Chrome/Chromium not installed")
    github = FakeGitHub()
    monkeypatch.setattr(gc, "_transport", httpx.MockTransport(github.handle))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(base + "/api/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    assert httpx.put(base + "/api/settings", json={
        "provider": "openai_compatible", "model": "fake", "endpoint": fake_model.url + "/v1",
        "api_key": "sk-ui-test-DUMMY-0000", "github_write_enabled": True,
        "github_write_token": WRITE_TOKEN}).json()["github_write_token_source"] == "stored"
    browser = Browser(chrome, base + "/")
    browser.wait("document.querySelector('#model-label').textContent.includes('fake')")
    yield {"browser": browser, "github": github, "base": base}
    browser.close()
    server.should_exit = True
    thread.join(10)


def _start(b, text):
    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden")
    b.eval(f"document.querySelector('#new-input').value = {text!r}")
    b.click("#new-start")
    b.wait("!document.querySelector('#view-work').hidden")


def test_the_card_shows_exactly_what_is_posted_and_approve_posts_once(ui, fake_model):
    b, github = ui["browser"], ui["github"]
    fake_model.queue(openai_tool_call(("github_post_issue_comment", {"issue_number": 99, "body": BODY})),
                     openai_text("GitHub confirmed the comment."))
    _start(b, "Post a reply on issue 99")
    b.wait("document.querySelector('.github-approval')")
    card = b.text(".github-approval")
    for expected in ("nature-ai-corp/nature-ai-worker", "#99 Disposable test issue", ISSUE_URL, "@maintainer",
                     "Approve and post"):
        assert expected in card
    assert b.text("pre.github-comment") == BODY and github.posts == []
    assert WRITE_TOKEN not in b.eval("document.documentElement.outerHTML")
    b.click(".github-approval button.primary")
    b.wait("document.querySelector('.github-operation.s-confirmed')", timeout=20)
    assert "Posted (confirmed by GitHub)" in b.text(".github-operation")
    assert len(github.posts) == 1


def test_deny_in_the_browser_posts_nothing(ui, fake_model):
    b, github = ui["browser"], ui["github"]
    fake_model.queue(openai_tool_call(("github_post_issue_comment", {"issue_number": 99, "body": BODY})),
                     openai_text("Not posted."))
    _start(b, "Post a reply on issue 99")
    b.wait("document.querySelector('.github-approval')")
    b.click(".github-approval button.danger")
    b.wait("document.querySelector('.github-operation.s-denied')", timeout=20)
    assert github.posts == []
