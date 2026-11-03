# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Real Chrome/Edge UI double-submit regression against an isolated fake Platform.

Never talks to the customer's live Platform or installed Windows service.
"""
import json
import os
from pathlib import Path
import threading
import time

import httpx
import pytest
import uvicorn

from nature_worker.platform_client.server import ClientSettings, create_app
from platform_fake import FakePlatform, USER
from test_process import _free_port
from test_usability import Browser, _find_chrome


def _browser_executable():
    chrome = _find_chrome()
    if chrome:
        return chrome
    if os.name != "nt":
        return None
    for root in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if not root:
            continue
        for sub in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
            candidate = Path(root) / sub
            if candidate.is_file():
                return str(candidate)
    return None


@pytest.fixture
def isolated_browser(tmp_path):
    chrome = _browser_executable()
    if not chrome:
        pytest.skip("Chrome/Edge not installed")
    fake = FakePlatform()
    app = create_app(ClientSettings(data_dir=tmp_path / "pc",
                                    standalone_data_dir=tmp_path / "standalone",
                                    platform_origin="https://platform.example"),
                     transport=fake.transport(), verify_startup=False)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/api/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("isolated Platform Client server did not start")
        browser = Browser(chrome, url + "/")
        try:
            yield fake, app, browser
        finally:
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_real_browser_double_template_submit_creates_only_one_task(isolated_browser):
    fake, app, b = isolated_browser
    b.wait("!document.querySelector('#login-form').hidden")
    b.eval("document.querySelector('#login-email').value = " + json.dumps(USER["email"]))
    b.eval("document.querySelector('#login-password').value = " + json.dumps(fake.password))
    b.click('#login-form button[type="submit"]')
    b.wait("!document.querySelector('#nav').hidden")
    b.eval("location.hash = '#/new'")
    b.wait("document.querySelector('#template-list .card') !== null")
    b.click("#template-list .card")
    b.wait("!document.querySelector('#template-form').hidden")
    b.eval("document.querySelector('#area-1').value = 'ACME'")
    b.eval("document.querySelector('#area-2').value = 'Summary'")
    # Two submissions in the same JS event turn simulate rapid double click/Enter.
    b.eval("""(() => { const form = document.querySelector('#template-form');
        form.requestSubmit(); form.requestSubmit(); return true; })()""")
    b.wait("location.hash.startsWith('#/chat/') || location.hash.startsWith('#/task/')", timeout=15)
    b.wait("document.querySelector('#template-form button[type=submit]').disabled === false", timeout=15)
    # The fake backend gives prompt, deterministic responses, no AI call.
    for _ in range(100):
        if fake.tasks:
            break
        time.sleep(0.05)
    assert len(fake.conversations) == 1
    assert len(app.state.submissions) == 1
    assert len(fake.tasks) == 1
