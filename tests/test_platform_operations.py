# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import pytest
from nature_worker.platform_contract import ContractError, PlatformClientContract
from nature_worker.platform_http import PlatformHttpClient, PlatformInterfaceError, SessionCookies, SessionExpired
from nature_worker.platform_operations import (
    ClientInterface, CookieSessionTransport, PlatformOperations, new_idempotency_key,
)
from nature_worker.platform_session import PlatformSession, PlatformSessionError

from platform_fake import USER, FakePlatform


@pytest.fixture
def platform():
    fake = FakePlatform()
    client = PlatformHttpClient("https://platform.example", transport=fake.transport())
    ops = PlatformOperations(client, PlatformClientContract.load())
    yield fake, ops
    client.close()


def signed_in(ops):
    transport = CookieSessionTransport(ops)
    session = PlatformSession(transport)
    transport.offer_password(USER["email"], "correct horse")
    session.sign_in()
    return transport, session, SessionCookies.from_token(session.session)


def test_only_contract_operations_can_execute(platform):
    _fake, ops = platform
    with pytest.raises(ContractError):
        ops.execute("createTask")
    with pytest.raises(PlatformInterfaceError):
        ops.execute("getCurrentUser")  # authenticated operation without a session


def test_native_login_validates_then_logout_is_confirmed_on_server(platform):
    fake, ops = platform
    transport, session, cookies = signed_in(ops)
    assert transport._offer is None
    assert ops.execute("getCurrentUser", session=cookies)["email"] == USER["email"]
    session.sign_out()
    assert session.session is None and session.pending_revoke is None
    assert fake.sessions == {}
    with pytest.raises(SessionExpired):
        ops.execute("getCurrentUser", session=cookies)


def test_wrong_password_is_a_sign_in_failure_without_session(platform):
    _fake, ops = platform
    transport = CookieSessionTransport(ops)
    session = PlatformSession(transport)
    transport.offer_password(USER["email"], "wrong")
    with pytest.raises(SessionExpired):
        session.sign_in()
    assert session.session is None and transport._offer is None


def test_unconfirmed_logout_keeps_revocation_pending(platform):
    fake, ops = platform
    transport, session, _ = signed_in(ops)
    fake.ignore_logout = True
    with pytest.raises(PlatformSessionError):
        session.sign_out()
    assert session.pending_revoke is not None


def test_task_flow_uses_only_client_interface_and_csrf(platform):
    fake, ops = platform
    _t, _s, cookies = signed_in(ops)
    api = ClientInterface(ops)
    conv = api.create_conversation(cookies, idempotency_key=new_idempotency_key())
    key = new_idempotency_key()
    out = api.post_message(cookies, conv["id"], content="Customer: ACME", start_task=True,
                           template_id="tpl-1", idempotency_key=key)
    assert out["execution_status"] == "accepted" and out["task"]["task_number"] == 1
    assert fake.tasks[1]["body"] == {"content": "Customer: ACME", "start_task": True, "template_id": "tpl-1"}
    replay = api.post_message(cookies, conv["id"], content="Customer: ACME", start_task=True,
                              template_id="tpl-1", idempotency_key=key)
    assert replay["task"]["task_number"] == 1 and len(fake.tasks) == 1
    assert api.task(cookies, 1)["work_pending"] is True
    fake.tasks[1]["work_pending"] = False
    assert api.results(cookies, 1)[0]["result_id"] == "r-1"
    assert api.result(cookies, 1, "r-1")["content"] == "Full credit summary"
    assert api.download(cookies, 1, "f-1").content == b"# hi\n"
    assert api.complete(cookies, 1)["status"] == "completed"
    assert all(path.startswith("/api/v1/") for _m, path in fake.requests)
