"""New Work templates in a real browser (WKR-025), against the real server process."""

import json

from conftest import openai_text, openai_tool_call
from nature_worker.templates import load_templates
from test_work_ui import SALES, app  # noqa: F401 - shared browser + server fixture

T = {t["id"]: t for t in load_templates()}


def _open_new(b):
    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden && document.querySelector('#template-description').textContent")


def _choose(b, name):
    b.click("#template-button")
    b.wait("!document.querySelector('#template-list').hidden")
    b.click("#template-list li", name)
    b.wait(f"document.querySelector('#template-name').textContent === {json.dumps(name)}")


def _field(b):
    return b.eval("document.querySelector('#new-input').value")


def _type(b, text):
    b.eval(f"{{ const i = document.querySelector('#new-input'); i.value = {json.dumps(text)}; i.dispatchEvent(new Event('input')); }}")


def test_default_is_general_work(app):
    b = app["browser"]
    _open_new(b)
    assert b.text("#template-name") == "General Work"
    assert b.text("#template-description") == T["general"]["description"]
    assert _field(b) == "" and b.eval("document.querySelector('#template-notice').hidden") is True
    b.click("#template-button")
    b.wait("!document.querySelector('#template-list').hidden")
    assert b.eval("[...document.querySelectorAll('#template-list [role=option]')].map(x => x.querySelector('.tpl-name').textContent)") == \
        [t["name"] for t in T.values()]
    assert b.eval("document.querySelector('#template-list [aria-selected=true]').id") == "tpl-general"
    assert b.eval("document.querySelector('#template-button').getAttribute('aria-expanded')") == "true"


def test_switching_templates_prepares_editable_instructions(app):
    b = app["browser"]
    _open_new(b)
    _choose(b, "Data Analysis")
    assert _field(b) == T["data-analysis"]["instruction"]
    assert b.text("#template-description") == T["data-analysis"]["description"]
    _choose(b, "Research & Summary")  # untouched template text is simply replaced
    assert _field(b) == T["research-summary"]["instruction"]
    assert b.eval("document.querySelector('#template-notice').hidden") is True
    assert b.eval("document.querySelector('#new-input').readOnly") is False


def test_typed_text_is_never_silently_replaced(app):
    b = app["browser"]
    _open_new(b)
    _choose(b, "Data Analysis")
    edited = T["data-analysis"]["instruction"].replace("File: ", "File: sales.csv")
    _type(b, edited)

    # Keep: the text stays and the selection goes back to the template that matches it.
    _choose(b, "Document & Report")
    assert _field(b) == edited
    assert "Your text was kept" in b.text("#template-notice")
    b.click("#template-notice button", "Keep my text")
    assert _field(b) == edited and b.text("#template-name") == "Data Analysis"
    assert b.eval("document.querySelector('#template-notice').hidden") is True

    # Insert: template text above the user's text.
    _choose(b, "Document & Report")
    b.click("#template-notice button", "Insert")
    assert _field(b) == T["document-report"]["instruction"] + "\n\n" + edited
    assert b.text("#template-name") == "Document & Report"

    # Replace only on an explicit click.
    _type(b, "my own words")
    _choose(b, "File Organization")
    assert _field(b) == "my own words"
    b.click("#template-notice button", "Replace my text")
    assert _field(b) == T["file-organization"]["instruction"]

    # General Work has nothing to insert, so the user's text just stays.
    _type(b, "free text")
    _choose(b, "General Work")
    assert _field(b) == "free text" and b.eval("document.querySelector('#template-notice').hidden") is True

    # Examples follow the same rule.
    b.click("#new-examples .example", "List the files")
    assert _field(b) == "free text" and "Your text was kept" in b.text("#template-notice")


def test_keyboard_selection(app):
    b = app["browser"]
    _open_new(b)
    b.eval("document.querySelector('#template-button').focus()")
    key = lambda k: b.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": k, "code": k,
                                                      "windowsVirtualKeyCode": {"ArrowDown": 40, "Enter": 13, "Escape": 27}[k]})
    key("ArrowDown")
    b.wait("!document.querySelector('#template-list').hidden && document.activeElement.id === 'template-list'")
    key("ArrowDown")
    key("ArrowDown")
    assert b.eval("document.querySelector('#template-list').getAttribute('aria-activedescendant')") == "tpl-data-analysis"
    key("Enter")
    b.wait("document.querySelector('#template-list').hidden")
    assert b.text("#template-name") == "Data Analysis" and b.eval("document.activeElement.id") == "template-button"
    key("ArrowDown")
    b.wait("!document.querySelector('#template-list').hidden")
    key("Escape")
    b.wait("document.querySelector('#template-list').hidden")
    assert b.text("#template-name") == "Data Analysis"


def test_submission_sends_the_template_instructions(app, fake_model):
    b = app["browser"]
    _open_new(b)
    _choose(b, "Data Analysis")
    text = T["data-analysis"]["instruction"].replace("File: ", "File: sales.csv").replace(
        "Question: ", "Question: Which region has the highest revenue?")
    _type(b, text)
    fake_model.queue(openai_tool_call(("table_summary", {"path": "sales.csv"})), openai_text("South is highest."))
    b.click("#new-start")
    b.wait("document.querySelector('#work-status') && document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    sent = fake_model.requests[0]["body"]["messages"]
    assert [m["content"] for m in sent if m["role"] == "user"] == [text]  # exactly what was in the field
    assert b.text("#work-title") == "File: sales.csv"
    assert b.eval("[...document.querySelectorAll('#messages .msg.user .text')].map(x => x.textContent)") == [text]
    # The next New Work starts fresh.
    _open_new(b)
    assert b.text("#template-name") == "General Work" and _field(b) == ""


def test_refresh_keeps_the_new_work_draft_until_new_work_is_clicked(app):
    b = app["browser"]
    _open_new(b)
    _choose(b, "Research & Summary")
    _type(b, T["research-summary"]["instruction"].replace("Topic: ", "Topic: tea sales"))
    b.eval("location.reload()")
    b.wait("document.querySelector('#template-name') && document.querySelector('#template-name').textContent === 'Research & Summary'")
    assert _field(b).startswith("Topic: tea sales")
    # A question left open at refresh restores the template that matches the text.
    _choose(b, "Data Analysis")
    b.eval("location.reload()")
    b.wait("document.querySelector('#template-name') && document.querySelector('#template-name').textContent === 'Research & Summary'")
    assert _field(b).startswith("Topic: tea sales")
    _open_new(b)
    assert b.text("#template-name") == "General Work" and _field(b) == ""


def test_templates_grant_no_authority_approval_and_deny(app, fake_model):
    b, tmp = app["browser"], app["tmp"]
    _open_new(b)
    _choose(b, "File Organization")
    fake_model.queue(openai_tool_call(("delete_file", {"path": "sales.csv"})))
    b.click("#new-start")
    b.wait("document.querySelector('#work-update .approval')", timeout=20)
    assert b.text("#work-status") == "Needs your approval"
    fake_model.queue(openai_text("Kept sales.csv."))
    b.click("#work-update .approval button", "Deny")
    b.wait("document.querySelector('#work-status').textContent === 'Finished'", timeout=20)
    assert (tmp / "workspace" / "sales.csv").read_text() == SALES
    # Existing work history still lists it.
    b.click("#nav-my-work")
    b.wait("document.querySelectorAll('#work-list .work-row').length === 1")
    assert "Folder (leave empty" in b.text("#work-list")


def test_phone_layout_with_open_selector(app):
    b = app["browser"]
    b.viewport(390, 844, mobile=True)
    _open_new(b)
    b.click("#template-button")
    b.wait("!document.querySelector('#template-list').hidden")
    assert b.eval("document.documentElement.scrollWidth <= window.innerWidth")
    assert b.eval("document.querySelector('#template-list').getBoundingClientRect().right <= window.innerWidth")
    b.click("#template-list li", "Document & Report")
    assert _field(b) == T["document-report"]["instruction"]
