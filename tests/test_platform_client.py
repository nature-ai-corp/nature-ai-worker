"""Platform Client process tests against the contract-shaped fake (not integration evidence)."""
import ast
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nature_worker.platform_client.server as server
from nature_worker.installation_mode import ModeError
from nature_worker.platform_client.server import ClientSettings, compose_template_content, create_app

from platform_fake import TEMPLATE, USER, FakePlatform


@pytest.fixture
def pc(tmp_path):
    fake = FakePlatform()
    settings = ClientSettings(data_dir=tmp_path / "pc", platform_origin="https://platform.example",
                              standalone_data_dir=tmp_path / "sa")
    app = create_app(settings, transport=fake.transport(), verify_startup=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield fake, client, app


def login(client):
    r = client.post("/api/login", json={"email": USER["email"], "password": "correct horse"})
    assert r.status_code == 200, r.text
    return r


def test_api_requires_local_ui_cookie_issued_only_after_platform_sign_in(pc):
    fake, client, _app = pc
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/status").json()["signed_in"] is False
    bad = client.post("/api/login", json={"email": USER["email"], "password": "nope"})
    assert bad.status_code == 401 and "nwpc_ui" not in bad.cookies
    login(client)
    status = client.get("/api/status").json()
    assert status["signed_in"] and status["user"]["email"] == USER["email"]
    assert status["client_interface"] == "1.3"
    # Another local caller without the browser's UI cookie gets nothing.
    other = TestClient(client.app, base_url="http://127.0.0.1")
    assert other.get("/api/tasks").status_code == 401
    assert other.get("/api/status").json()["signed_in"] is False


def test_platform_session_cookies_never_reach_the_browser(pc):
    fake, client, _app = pc
    r = login(client)
    assert "nature_session" not in r.cookies and "nature_csrf" not in r.cookies
    session = next(iter(fake.sessions))
    for path in ("/api/status", "/api/tasks", "/api/templates"):
        assert session not in client.get(path).text


def test_template_task_submission_progress_results_and_logout(pc):
    fake, client, app = pc
    login(client)
    assert client.get("/api/templates").json()[0]["id"] == "tpl-1"
    sub = client.post("/api/submit", json={"template_id": "tpl-1", "values": {"1": "ACME", "2": "Summary"},
                                           "additional": "No external messages."}).json()
    for _ in range(100):
        state = client.get(f"/api/submissions/{sub['submission_id']}").json()
        if state["state"] != "running":
            break
        time.sleep(0.02)
    assert state["state"] == "done" and state["task_number"] == 1
    assert fake.tasks[1]["body"] == {"content": "Customer: ACME\nScope: Summary\n"
                                                "Additional instructions: No external messages.",
                                     "start_task": True, "template_id": "tpl-1"}
    view = client.get("/api/tasks/1").json()
    assert view["progress"]["state"] == "working"
    fake.tasks[1]["work_pending"] = False
    view = client.get("/api/tasks/1").json()
    assert view["progress"]["state"] == "idle"
    assert client.get("/api/tasks/1/results/r-1").json()["content"] == "Full credit summary"
    dl = client.get("/api/tasks/1/files/f-1")
    assert dl.content == b"# hi\n"
    assert dl.headers["content-disposition"] == 'attachment; filename=".._report.md"'
    assert client.post("/api/logout").status_code == 204
    assert fake.sessions == {} and app.state.submissions == {}
    assert client.get("/api/tasks").status_code == 401


def test_platform_ending_the_session_signs_the_worker_out(pc):
    fake, client, app = pc
    login(client)
    fake.sessions.clear()  # e.g. eight hours passed, or an administrator revoked it
    r = client.get("/api/tasks")
    assert r.status_code == 401
    assert app.state.account.session.session is None
    assert client.get("/api/status").json()["signed_in"] is False


def test_unconfirmed_logout_is_reported_not_hidden(pc):
    fake, client, app = pc
    login(client)
    fake.ignore_logout = True
    r = client.post("/api/logout")
    assert r.status_code == 502 and r.json()["revocation_pending"] is True
    assert client.get("/api/tasks").status_code == 401
    fake.ignore_logout = False
    assert client.post("/api/login", json={"email": USER["email"], "password": "correct horse"}).status_code == 200
    assert app.state.account.session.pending_revoke is None


def test_cross_origin_mutation_blocked(pc):
    _fake, client, _app = pc
    r = client.post("/api/login", json={"email": "a@b.c", "password": "x"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_template_composition_enforces_required_areas():
    with pytest.raises(ValueError):
        compose_template_content(TEMPLATE, {"2": "Full"}, "")
    assert compose_template_content(TEMPLATE, {"1": "ACME"}, "") == "Customer: ACME"


def test_startup_blocked_without_platform_client_installation_and_isolation(tmp_path):
    settings = ClientSettings(data_dir=tmp_path / "pc", platform_origin="https://platform.example",
                              standalone_data_dir=tmp_path)
    with pytest.raises(ModeError) as info:
        create_app(settings)
    assert "platform-client-installation-required" in str(info.value)
    assert "os-execution-isolation-unverified" in str(info.value)


def test_platform_client_never_imports_standalone_runtime():
    forbidden = {"agent", "providers", "tools", "app", "credentials", "db", "work"}
    package = Path(server.__file__).parent
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.level >= 2:
                assert node.module.split(".")[0] not in forbidden, (source.name, node.module)
