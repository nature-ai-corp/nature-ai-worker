"""New Work templates (WKR-025): local definitions, validation and the read-only API."""

import json

import pytest

from nature_worker.templates import TEMPLATES_FILE, TemplateError, load_templates

EXPECTED = ["general", "research-summary", "data-analysis", "document-report", "file-organization"]


def test_packaged_templates_load_in_order():
    templates = load_templates()
    assert [t["id"] for t in templates] == EXPECTED
    assert [t["name"] for t in templates] == ["General Work", "Research & Summary", "Data Analysis",
                                              "Document & Report", "File Organization"]
    assert templates[0]["instruction"] == ""  # General Work: the user's own words
    for t in templates:
        assert set(t) == {"id", "name", "description", "instruction"}
        assert t["description"].strip()
    assert all(t["instruction"].strip() for t in templates[1:])


def test_api_lists_enabled_templates_with_default(client):
    r = client.get("/api/templates")
    assert r.status_code == 200
    assert r.json()["default"] == "general"
    assert [t["id"] for t in r.json()["templates"]] == EXPECTED


def _write(tmp_path, templates):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"templates": templates}))
    return path


def _t(**over):
    return {"id": "x", "name": "X", "description": "d", "instruction": "i", "enabled": True, **over}


def test_disabled_templates_are_left_out(tmp_path):
    path = _write(tmp_path, [_t(id="a"), _t(id="b", enabled=False), _t(id="c")])
    assert [t["id"] for t in load_templates(path)] == ["a", "c"]


@pytest.mark.parametrize("templates, message", [
    ([_t(id="a"), _t(id="a")], "duplicate id"),
    ([_t(id="Bad Id")], "'id'"),
    ([_t(id="../x")], "'id'"),
    ([{k: v for k, v in _t().items() if k != "instruction"}], "'instruction'"),
    ([_t(name="  ")], "'name' must not be empty"),
    ([_t(enabled="yes")], "'enabled'"),
    ([_t(description="x" * 401)], "'description'"),
    ([_t(enabled=False)], "at least one template"),
    (["not an object"], "must be an object"),
])
def test_invalid_template_files_are_rejected(tmp_path, templates, message):
    with pytest.raises(TemplateError, match=message):
        load_templates(_write(tmp_path, templates))


def test_unreadable_file_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(TemplateError, match="Cannot read"):
        load_templates(bad)
    with pytest.raises(TemplateError, match="must be a list"):
        load_templates(_write(tmp_path, {"a": 1}))


def test_templates_do_not_promise_unavailable_capabilities():
    """Templates are text only. They must not ask for tools Worker does not have."""
    data = json.loads(TEMPLATES_FILE.read_text())
    assert set(data["templates"][0]) == {"id", "name", "description", "instruction", "enabled"}
    text = {t["id"]: (t["description"] + "\n" + t["instruction"]).lower() for t in load_templates()}
    for tid, body in text.items():
        for phrase in ("send an email", "browse the web", "search the internet", "run the code", ".docx", ".pdf",
                       ".xlsx", "schedule", "every day", "department", "module"):
            assert phrase not in body, (tid, phrase)
    # File Organization says plainly what the agent cannot do, and does not ask it to delete anything.
    assert "cannot move or rename" in text["file-organization"]
    assert "do not delete or overwrite" in text["file-organization"]
    assert "workspace files only" in text["research-summary"]
    assert "excel files are not supported" in text["data-analysis"]
