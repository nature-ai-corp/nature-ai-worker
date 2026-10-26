# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import copy
import json

import pytest
from nature_worker.platform_contract import BUNDLED, ContractError, PlatformClientContract

WORKER_OPERATIONS = {
    "nativeLogin", "logout", "getCurrentUser",
    "createConversation", "listConversations", "getConversation", "getConversationTask",
    "listConversationMessages", "postConversationMessage",
    "listTaskTemplates", "listMyTaskSummaries", "getTaskByNumber", "getTaskWorkHistory",
    "closeTaskByNumber", "continueTaskInConversation",
    "listTaskResults", "getTaskResult", "listTaskFiles", "downloadTaskFile",
    "listTaskApprovals", "approveTaskApproval", "rejectTaskApproval",
}


def test_bundled_contract_is_client_interface_1_3_with_28_operations():
    contract = PlatformClientContract.load()
    assert len(contract.names()) == 28
    assert WORKER_OPERATIONS <= contract.names()
    login = contract.resolve("nativeLogin")
    assert (login.method, login.path, login.authenticated, login.has_body) == (
        "POST", "/api/v1/auth/native-login", False, True)
    post = contract.resolve("postConversationMessage")
    assert post.mutating and "Idempotency-Key" in post.header_params
    assert contract.resolve("getTaskByNumber").path_params == (("task_number", "integer"),)


def test_contract_has_no_upload_operation():
    contract = PlatformClientContract.load()
    assert not any("upload" in name.lower() for name in contract.names())
    assert not any(contract.resolve(n).method in ("PUT", "POST") and "/files" in contract.resolve(n).path
                   for n in contract.names())


def test_operations_outside_the_client_interface_are_unreachable():
    contract = PlatformClientContract.load()
    for invented in ("createTask", "uploadTaskFile", "list_users_api_v1_users_get"):
        with pytest.raises(ContractError):
            contract.resolve(invented)


def _mutated(tmp_path, change):
    doc = json.loads(BUNDLED.read_text())
    change(doc)
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("change", [
    lambda d: d["components"]["securitySchemes"]["portalSession"].update(name="other"),
    lambda d: d["components"]["securitySchemes"]["csrfToken"].update(name="X-Other"),
    lambda d: d["paths"]["/api/v1/auth/me"]["get"].update({"x-nature-client-interface": "2.0"}),
    lambda d: d.update(openapi="2.0"),
    lambda d: d.update(paths={}),
])
def test_incompatible_contract_fails_closed(tmp_path, change):
    with pytest.raises((ContractError, ValueError)):
        PlatformClientContract.load(_mutated(tmp_path, change))


def test_unmarked_operations_are_ignored(tmp_path):
    def add(d):
        d["paths"]["/api/v1/admin/x"] = {"get": {"operationId": "adminThing"}}
    assert "adminThing" not in PlatformClientContract.load(_mutated(tmp_path, add)).names()


def test_bundled_contract_publishes_no_platform_internals():
    """Free-text descriptions document the Platform's internals; the public copy has none."""
    raw = BUNDLED.read_text()
    document = json.loads(raw)
    security = document["components"].pop("securitySchemes")
    assert all(isinstance(s.get("description"), str) for s in security.values())

    def annotations(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    yield value
                yield from annotations(value)
        elif isinstance(node, list):
            for value in node:
                yield from annotations(value)

    assert list(annotations(document)) == []
    for internal in ("app.services.", "app.domain.", "_handle_callback", "password_credentials", "mission section"):
        assert internal not in raw
