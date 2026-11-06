# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""GitHub Community Agent profile in a real browser, against the real server process.

The model is the local fake server with scripted responses: these tests check the Work
Center entry, the starters, the status card and that a starter becomes ordinary Work for
the same General Agent runtime - not what a real model would answer.
"""

import json

import httpx

from conftest import openai_text
from nature_worker.templates import load_templates
from test_work_ui import app  # noqa: F401 - shared browser + server fixture

PROFILE = next(t for t in load_templates() if t["id"] == "github-community")


def _open_community(b):
    b.click("#nav-community")
    b.wait("!document.querySelector('#view-community').hidden && document.querySelector('#community-connection').textContent")


def _settings(app, **changes):
    s = httpx.get(app["base"] + "/api/settings").json()
    body = {"provider": s["provider"], "model": s["model"], "endpoint": s["endpoint"], **changes}
    r = httpx.put(app["base"] + "/api/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_entry_shows_repository_and_honest_status(app):
    b = app["browser"]
    _open_community(b)
    assert b.eval("document.querySelector('#nav-community').getAttribute('aria-current')") == "page"
    assert b.text("#community-repository") == "nature-ai-corp/nature-ai-worker"
    status = b.text("#community-connection")
    assert "GitHub reading is off" in status and "GitHub posting is off" in status
    assert b.eval("document.activeElement.id") == "community-heading"


def test_status_updates_after_saving_settings_from_the_page(app):
    b = app["browser"]
    _open_community(b)
    b.click("#community-settings")
    b.wait("document.querySelector('#settings-dialog').open")
    b.eval("document.querySelector('#settings-dialog details').open = true")
    b.eval("document.querySelector('#s-github-repository').value = 'octo-org/demo'")
    b.click("#s-github")
    b.click("#s-save")
    b.wait("!document.querySelector('#settings-dialog').open")
    b.wait("document.querySelector('#community-repository').textContent === 'octo-org/demo'")
    assert "Public Issues are available" in b.text("#community-connection")


def test_starter_prefills_editable_new_work_and_runs_as_ordinary_work(app, fake_model):
    b = app["browser"]
    _settings(app, github_enabled=True)
    _open_community(b)
    b.click("[data-community-task='issues']")
    b.wait("!document.querySelector('#view-new').hidden")
    text = b.eval("document.querySelector('#new-input').value")
    assert text.startswith("Task: Review the newest open Issues of the configured public GitHub repository.")
    assert "Work as the GitHub Community Agent" in text and "untrusted data" in text
    assert b.text("#template-name") == "GitHub Community Agent"
    assert fake_model.requests == []  # nothing runs until the user starts it
    b.eval("{ const i = document.querySelector('#new-input'); i.value = i.value.replace('newest open Issues', "
           "'newest 5 open Issues'); i.dispatchEvent(new Event('input')); }")
    fake_model.queue(openai_text("Here is the review."))
    b.click("#new-start")
    b.wait("document.querySelector('#work-status') && document.querySelector('#work-status').textContent.includes('Finished')",
           timeout=20)
    (request,) = fake_model.requests
    user = request["body"]["messages"][-1]["content"]
    assert user.startswith("Task: Review the newest 5 open Issues")  # the user's edit was sent
    tools = {t["function"]["name"] for t in request["body"]["tools"]}
    assert {"github_issues", "read_file", "write_file"} <= tools and "github_post_issue_comment" not in tools
    work = httpx.get(app["base"] + "/api/work").json()
    assert work[0]["title"].startswith("Task: Review the newest 5 open Issues")


def test_each_starter_is_distinct_and_untouched_starters_are_replaced_without_asking(app):
    b = app["browser"]
    b.eval("window.__asked = []; window.confirm = (m) => { window.__asked.push(m); return false; }")
    seen = set()
    for task in ("issues", "discussions", "drafts", "report"):
        _open_community(b)
        b.click(f"[data-community-task='{task}']")
        b.wait("!document.querySelector('#view-new').hidden")
        first = b.eval("document.querySelector('#new-input').value").splitlines()[0]
        assert first.startswith("Task: ") and len(first) > 40
        seen.add(first)
    assert len(seen) == 4 and b.eval("window.__asked") == []
    # Text the user wrote is never replaced without asking; declining keeps it.
    b.eval("{ const i = document.querySelector('#new-input'); i.value = 'my own careful notes'; i.dispatchEvent(new Event('input')); }")
    _open_community(b)
    b.click("[data-community-task='issues']")
    b.wait("window.__asked.length === 1")
    b.click("#new-work")  # General Work afterwards: empty field, default template
    b.wait("!document.querySelector('#view-new').hidden")
    assert b.eval("document.querySelector('#new-input').value") == ""
    assert b.text("#template-name") == "General Work"


def test_declined_replacement_keeps_the_users_text(app):
    b = app["browser"]
    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden")
    b.eval("{ const i = document.querySelector('#new-input'); i.value = 'keep me'; i.dispatchEvent(new Event('input')); }")
    b.eval("window.confirm = () => false")
    _open_community(b)
    b.click("[data-community-task='report']")
    b.eval("location.hash = '#/new'")
    b.wait("!document.querySelector('#view-new').hidden")
    assert b.eval("document.querySelector('#new-input').value") == "keep me"


def test_profile_is_also_a_normal_template_with_the_task_line_first(app):
    b = app["browser"]
    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden")
    b.click("#template-button")
    b.wait("!document.querySelector('#template-list').hidden")
    b.click("#template-list li", "GitHub Community Agent")
    value = b.eval("document.querySelector('#new-input').value")
    assert value == PROFILE["instruction"] and value.startswith("Task: \n\n")
    assert json.dumps(PROFILE)  # loaded from the packaged file
