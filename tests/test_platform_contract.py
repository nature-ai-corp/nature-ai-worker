import json
import pytest
from nature_worker.platform_contract import ContractError, PlatformClientContract


def test_contract_requires_explicit_operation(tmp_path):
    path = tmp_path / "platform-routes.json"
    path.write_text(json.dumps({"schema": 1, "operations": {
        "sample_operation": {"method": "GET", "path": "/contract-supplied"}
    }}))
    loaded = PlatformClientContract.load(path)
    assert loaded.resolve("sample_operation").path == "/contract-supplied"
    with pytest.raises(ContractError):
        loaded.resolve("invented_task_endpoint")


@pytest.mark.parametrize("record", [
    {}, {"schema": 1, "operations": {}},
    {"schema": 1, "operations": {"bad": {"method": "GET", "path": "//evil"}}},
    {"schema": 1, "operations": {"bad": {"method": "GET", "path": "/x", "extra": True}}},
])
def test_invalid_contract_fails_closed(tmp_path, record):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(record))
    with pytest.raises((ContractError, ValueError)):
        PlatformClientContract.load(path)
