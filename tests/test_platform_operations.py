import json
import httpx
import pytest
from nature_worker.platform_contract import ContractError, PlatformClientContract
from nature_worker.platform_http import PlatformHttpClient
from nature_worker.platform_operations import PlatformOperations


def test_only_contract_supplied_route_can_execute(tmp_path):
    doc = tmp_path / "routes.json"
    doc.write_text(json.dumps({"schema": 1, "operations": {
        "example_read": {"method": "GET", "path": "/from-platform-contract"}
    }}))
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})
    client = PlatformHttpClient("https://platform.example",
                                transport=httpx.MockTransport(handler))
    ops = PlatformOperations(client, PlatformClientContract.load(doc))
    assert ops.execute("example_read", session="fake") == {"status": "ok"}
    with pytest.raises(ContractError):
        ops.execute("missing_operation")
    assert calls == ["/from-platform-contract"]
    client.close()
