"""My Work interface in a real browser (WKR-023), driven against the real server process.

The model is the local fake server with scripted responses, so these tests check
how real records are presented, not what a real model would do.
"""

import json

import httpx
import pytest

from conftest import openai_error, openai_text, openai_tool_call
from nature_worker.db import Database
from test_process import _free_port, _start, _stop
from test_usability import Browser, _find_chrome

SALES = "region,revenue\nNorth,170\nSouth,320\n"


@pytest.fixture
def app(tmp_path, fake_model):
    chrome = _find_chrome()
    if not chrome:
        pytest.skip("Chrome/Chromium not installed")
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "sales.csv").write_text(SALES)
    state = {"port": _free_port(), "tmp": tmp_path, "chrome": chrome}
    state["proc"], state["base"] = _start(tmp_path, state["port"])
    assert httpx.put(state["base"] + "/api/settings", json={
        "provider": "openai_compatible", "model": "fake", "endpoint": fake_model.url + "/v1",
        "api_key": "sk-ui-test-DUMMY-0000"}).json()["configured"]
    state["browser"] = Browser(chrome, state["base"] + "/")
    state["browser"].wait("document.querySelector('#model-label').textContent.includes('fake')")
    yield state
    state["browser"].close()
    _stop(state["proc"])


def _start_work(b, text):
    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden")
    b.eval(f"document.querySelector('#new-input').value = {json.dumps(text)}")
    b.click("#new-start")
    b.wait("!document.querySelector('#view-work').hidden")


def _status(b):
    return b.text("#work-status")


def test_empty_my_work_is_the_entry_point(app):
    b = app["browser"]
    b.wait("!document.querySelector('#view-list').hidden")
    assert b.text("#list-heading") == "My Work"
    assert "No work yet" in b.text("#work-list")
    b.click("#work-list button", "+ New Work")
    b.wait("!document.querySelector('#view-new').hidden && document.querySelector('#new-files-hint').textContent")
    assert "sales.csv" not in b.text("#new-files-hint") and "1 file" in b.text("#new-files-hint")


def test_new_work_report_generation_and_results(app, fake_model):
    b, tmp = app["browser"], app["tmp"]
    report = "# Report\n\nNorth: 170\nSouth: 320\nTotal: 490\n"
    fake_model.delay = 0.7  # long enough to see the run in progress
    fake_model.queue(
        openai_tool_call(("read_file", {"path": "sales.csv"})),
        openai_tool_call(("write_file", {"path": "report.md", "content": report})),
        openai_text("I wrote **report.md**. Total revenue is 490."),
    )
    _start_work(b, "Read sales.csv and write report.md with totals per region.")
    b.wait("document.querySelector('#work-status').textContent === 'Working'")
    assert "Working" in b.text("#work-update")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)

    assert b.text("#work-title") == "Read sales.csv and write report.md with totals per region."
    assert "Total revenue is 490" in b.text("#work-update")
    facts = b.eval("Object.fromEntries([...document.querySelectorAll('#work-facts > div')]"
                   ".map(d => [d.querySelector('dt').textContent, d.querySelector('dd').textContent]))")
    assert facts["Agent steps"] == "3" and facts["Tool calls"] == "2" and facts["Result files"] == "1"
    assert facts["Approvals"] == "None requested"

    # Results: the generated file is a deliverable; the file it read is listed separately as an input.
    assert b.eval("[...document.querySelectorAll('#work-results .deliverables .fname')].map(x => x.textContent)") == ["report.md"]
    assert "Created by this work" in b.text("#work-results .deliverables")
    assert b.eval("[...document.querySelectorAll('#work-results .inputs .fname')].map(x => x.textContent)") == ["sales.csv"]
    b.click("#work-results .deliverables button", "Preview")
    b.wait("!document.querySelector('#preview-content').hidden")
    assert b.text("#preview-content") == report
    b.click("#close-preview")
    href = b.eval("document.querySelector('#work-results .deliverables a.button').getAttribute('href')")
    assert href == "/api/files/download?path=report.md"
    assert httpx.get(app["base"] + href).text == report == (tmp / "workspace" / "report.md").read_text()

    # Activity and communication come from the stored records.
    assert b.eval("[...document.querySelectorAll('#work-activity .tool .name')].map(x => x.textContent)") == ["read_file", "write_file"]
    assert b.eval("[...document.querySelectorAll('#work-activity .tool .state')].map(x => x.textContent)") == ["done", "done"]
    assert b.eval("[...document.querySelectorAll('#messages .msg .who')].map(x => x.textContent)") == ["You", "General Agent"]
    b.click("#nav-my-work")
    b.wait("document.querySelectorAll('#work-list .work-row').length === 1")
    assert "Finished" in b.text("#work-list") and "1 result file" in b.text("#work-list")


def test_approval_and_denial_from_the_work_view(app, fake_model):
    b, tmp = app["browser"], app["tmp"]
    fake_model.queue(openai_tool_call(("delete_file", {"path": "sales.csv"})))
    _start_work(b, "Delete sales.csv")
    b.wait("document.querySelector('#work-update .approval')")
    assert _status(b) == "Needs your approval"
    assert "Your action needed: Delete file 'sales.csv'" in b.text("#work-update")
    assert "waiting for your approval" in b.text("#work-activity")
    fake_model.queue(openai_text("Understood, sales.csv was kept."))
    b.click("#work-update .approval button", "Deny")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    assert (tmp / "workspace" / "sales.csv").read_text() == SALES
    assert b.eval("[...document.querySelectorAll('#work-activity .tool .state')].map(x => x.textContent)") == ["not approved"]
    assert "1 denied" in b.text("#work-facts") and "1 not approved" in b.text("#work-facts")
    assert "No files created by this work yet" in b.text("#work-results")

    fake_model.queue(openai_tool_call(("write_file", {"path": "sales.csv", "content": "replaced\n"})))
    b.eval("document.querySelector('#input').value = 'Replace sales.csv with the word replaced'")
    b.click("#send")
    b.wait("document.querySelector('#work-update .approval')", timeout=20)
    fake_model.queue(openai_text("Replaced."))
    b.click("#work-update .approval button", "Approve")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    assert (tmp / "workspace" / "sales.csv").read_text() == "replaced\n"
    assert "Overwritten by this work" in b.text("#work-results .deliverables")


def test_failed_work_is_shown_clearly(app, fake_model):
    b = app["browser"]
    fake_model.queue(openai_error(401, "Incorrect API key provided."))
    _start_work(b, "Summarize the notes")
    b.wait("document.querySelector('#work-status').textContent === 'Failed'", timeout=20)
    assert "Incorrect API key" in b.text("#work-update .update.error")
    assert "Incorrect API key" in b.text("#work-activity .event.error")
    assert "No files created by this work yet" in b.text("#work-results")


def test_returning_refresh_restart_and_interrupted_work(app, fake_model):
    b, base = app["browser"], app["base"]
    fake_model.queue(openai_tool_call(("write_file", {"path": "a.md", "content": "A"})), openai_text("Wrote a.md."))
    _start_work(b, "Write a.md")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    first_hash = b.eval("location.hash")
    fake_model.queue(openai_text("Hello."))
    _start_work(b, "Say hello")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)

    # Returning to previous work from My Work.
    b.click("#nav-my-work")
    b.click("#work-list .work-row", "Write a.md")
    b.wait("document.querySelector('#work-title').textContent === 'Write a.md'")
    assert b.eval("location.hash") == first_hash

    # Browser refresh keeps the selected work.
    b.eval("location.reload()")
    b.wait("document.querySelector('#work-title') && document.querySelector('#work-title').textContent === 'Write a.md'")
    assert _status(b) == "Finished" and "a.md" in b.text("#work-results .deliverables")

    # Application restart, with a run that was cut off: the record ends with the user's message.
    _stop(app["proc"])
    db = Database(app["tmp"] / "data" / "worker.db")
    conv = db.create_conversation("Cut off work")
    db.add_message(conv["id"], "user", "Cut off work")
    db.close()
    app["proc"], _ = _start(app["tmp"], app["port"])
    b.eval("location.reload()")
    b.wait("document.querySelector('#work-title') && document.querySelector('#work-title').textContent === 'Write a.md'")
    assert "a.md" in b.text("#work-results .deliverables")
    b.click("#work-nav a", "Cut off work")
    b.wait("document.querySelector('#work-status').textContent === 'Interrupted'")
    assert "stopped before replying" in b.text("#work-update") and "continue" in b.text("#work-update")
    assert len(httpx.get(base + "/api/work").json()) == 3


def test_model_and_file_text_is_never_rendered_as_html(app, fake_model):
    b = app["browser"]
    evil = '<img src=x onerror="window.pwned=1">'
    fake_model.queue(openai_tool_call(("write_file", {"path": "x<b>.md", "content": evil})), openai_text(evil))
    _start_work(b, evil)
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    assert b.eval("document.querySelectorAll('#view-work img, #work-nav img').length") == 0
    assert b.eval("window.pwned === undefined") is True
    assert evil in b.text("#work-update") and "x<b>.md" in b.text("#work-results")


def test_accessibility_basics_and_phone_layout(app, fake_model):
    b = app["browser"]
    fake_model.queue(openai_tool_call(("read_file", {"path": "sales.csv"})), openai_text("Read it."))
    _start_work(b, "Read sales.csv")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    work_hash = b.eval("location.hash")
    unnamed = """[...document.querySelectorAll('button, a[href], textarea, input:not([type=hidden]), select')]
        .filter(e => e.offsetParent !== null)
        .filter(e => !(e.getAttribute('aria-label') || e.textContent.trim() || (e.labels && e.labels.length)
                      || e.getAttribute('title')))
        .map(e => e.outerHTML.slice(0, 80))"""
    views = {"#/": "#view-list", "#/new": "#view-new", work_hash: "#view-work"}
    for hash_, view in views.items():
        b.eval(f"location.hash = {json.dumps(hash_)}")
        b.wait(f"!document.querySelector({json.dumps(view)}).hidden")
        assert b.eval(unnamed) == [], hash_  # every control has an accessible name
        assert b.eval("document.querySelectorAll('.view:not([hidden]) h1').length") == 1
        assert b.eval("document.activeElement.tagName") == "H1"  # focus moves to the new view
    # Status is conveyed by text, not only by color.
    assert b.eval("[...document.querySelectorAll('#work-nav .visually-hidden')].map(x => x.textContent)") == [", Finished"]
    assert _status(b) == "Finished"
    # Re-rendering unchanged data does not replace the live region (no repeated announcements).
    assert b.eval("(() => { const n = document.querySelector('#work-update').firstChild; renderWork();"
                  " return n === document.querySelector('#work-update').firstChild; })()") is True

    for width in (390, 768, 1280):
        b.viewport(width, 844, mobile=width < 760)
        for hash_, view in views.items():
            b.eval(f"location.hash = {json.dumps(hash_)}")
            b.wait(f"!document.querySelector({json.dumps(view)}).hidden")
            assert b.eval("document.documentElement.scrollWidth <= window.innerWidth"), (width, hash_)
        menu_visible = b.eval("document.querySelector('#toggle-sidebar').offsetParent !== null")
        assert menu_visible == (width < 760)


def test_my_work_list_follows_running_work(app, fake_model):
    b = app["browser"]
    fake_model.delay = 2.5
    fake_model.queue(openai_text("Done."))
    _start_work(b, "Slow work")
    b.click("#nav-my-work")
    b.wait("document.querySelector('#work-list').textContent.includes('Working')")
    b.wait("document.querySelector('#work-list').textContent.includes('Finished')", timeout=20)
    b.click("#work-list .work-row", "Slow work")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'")
    assert b.text("#work-update").count("Done.") == 1


def test_work_center_views_and_platform_style(app, fake_model):
    """Work Center groups existing work by derived status; selection and colors follow the Platform reference."""
    b, base = app["browser"], app["base"]
    def add(text, *responses):
        conv = httpx.post(base + "/api/conversations").json()["id"]
        fake_model.queue(*responses)
        httpx.post(f"{base}/api/conversations/{conv}/messages", json={"text": text}, timeout=30)
    add("Finished one", openai_text("Done."))
    add("Failed one", openai_error(500, "boom"))
    add("Approval one", openai_tool_call(("delete_file", {"path": "sales.csv"})))
    b.eval("loadWork()")
    b.wait("document.querySelectorAll('#work-nav a').length === 3")

    def view(label):
        b.click("#work-center .nav-item", label)
        b.wait(f"document.querySelector('#list-heading').textContent === {json.dumps(label)}")
        current = b.eval("[...document.querySelectorAll('#sidebar [aria-current=page]')].map(x => x.textContent)")
        assert current == [label]  # exactly one selected item
        return b.eval("[...document.querySelectorAll('#work-list .row-title')].map(x => x.textContent)")

    assert view("My Work") == ["Approval one", "Failed one", "Finished one"]
    assert view("Needs My Attention") == ["Approval one", "Failed one"]
    assert view("Finished Work") == ["Finished one"]
    assert view("Work In Progress") == []
    assert "Nothing is running right now." in b.text("#work-list")
    assert b.eval("location.hash") == "#/in-progress"
    b.eval("location.reload()")  # the selected view survives a refresh
    b.wait("document.querySelector('#list-heading') && document.querySelector('#list-heading').textContent === 'Work In Progress'")

    # Opening work selects it under Recent Work; no Work Center item stays selected.
    b.click("#work-nav a", "Failed one")
    b.wait("document.querySelector('#work-title').textContent === 'Failed one'")
    assert b.eval("[...document.querySelectorAll('#sidebar [aria-current=page]')].map(x => x.textContent)") == ["Failed one, Failed"]

    css = lambda sel, prop: b.eval(f"getComputedStyle(document.querySelector({json.dumps(sel)}))[{json.dumps(prop)}]")
    assert css("#sidebar", "backgroundColor") == "rgb(19, 22, 30)"          # dark navy (#13161e)
    assert css("#work-nav a[aria-current=page]", "backgroundColor") == "rgb(35, 38, 46)"  # selection (#23262e)
    assert css("#new-work", "backgroundColor") == "rgb(26, 28, 41)"         # New Work button (#1a1c29)
    assert css("#main", "backgroundColor") in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)")
    assert css("body", "backgroundColor") == "rgb(255, 255, 255)"           # light working area
    assert b.text("#sidebar .brand-name") == "NATURE AI"
